"""Converter Stream State — Anthropic 方向唯一 schema 住所（ADR-0026 D0/D1）。

to_anthropic 两个源状态机（chat→anthropic / responses→anthropic）共享的
类型化 dataclass ``AnthropicStreamState``：10 键裸 dict（``_reset_stream_state``）
的 8 键字段化（剔除死字段 ``tool_id`` / ``tool_name``），两源差异是字段使用
差异（responses 源天然不用 ``tool_call_indices`` / ``_prev_completion_tokens`` /
``pending_finish_reason``），不是 schema 差异——不造两个类（ADR-0026 备选已否决）。

高频不变量转移方法（ADR-0026 D1）：
- ``ensure_started`` — header 幂等（收编 ``_ensure_message_started``）
- ``open_block`` / ``append_delta`` — 坍缩 8 处 ensure open → start → delta 仪式
- ``close_block`` — 收编 ``_close_content_block`` 自由函数，thinking 块先补 signature_delta
- ``queue_stop`` — usage 等待 stash 归一（镜像 ``ResponseStreamState.queue_final`` /
  ``release_pending``，chat 源专属）
- ``finalize`` — EOF 补偿（chat 源专属；responses 源 no-op 由调用方门控）

状态仅存内存、从不序列化，dict → dataclass 无兼容负担。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from converters.parsing_chat import parse_chat_finish_reason
from converters.stream_usage import _chat_usage_output_delta, _chat_usage_to_anthropic_start

# Chat finish_reason → Anthropic stop_reason 单一住所（与 to_anthropic.render_finish_anthropic 同表，
# 此处复制避免循环导入；to_anthropic 侧保留同一映射供非流式路径使用）
_STOP_REASONS_BY_FINISH: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def _render_finish(finish_reason: str | None) -> str:
    return _STOP_REASONS_BY_FINISH.get(finish_reason or "", "end_turn")


@dataclass
class AnthropicStreamState:
    """Anthropic 方向流式会话状态：8 键逐一映射（剔除死字段后）。

    chat / responses 两源共用；responses 源天然不用部分字段（保持默认即可）。
    """

    started: bool = False
    content_block_started: bool = False
    content_block_index: int = 0
    current_content_type: str | None = None
    tool_call_indices: dict[int, int] = field(default_factory=dict)
    _prev_completion_tokens: int = 0
    message_stop_sent: bool = False
    pending_finish_reason: str | None = None

    # --- 不变量方法（ADR-0026 D1） ---

    def ensure_started(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """header 幂等：message_start 仅一次（收编 ``_ensure_message_started``）。

        chat 源：取 chunk.id/model/usage 播种 ``_chat_usage_to_anthropic_start``；
        responses 源：若 chunk 含 ``response`` 则按 responses 语义构造（幂等一致）。
        已 started 返回空列表。
        """
        if self.started:
            return []
        self.started = True
        # responses 创建形态：chunk 为 response.created 携带的 response 对象
        if "response" in chunk or chunk.get("type") == "response.created":
            resp = chunk.get("response") or {}
            # chunk 可能直接就是 response.created 的外层（type 含 response），也可能是裸 response
            # 兼容两种调用：_response_stream_chunk_to_anthropic 传入的 chunk 为外层事件
            if isinstance(resp, dict) and resp:
                rid = resp.get("id", "")
                model = resp.get("model", "")
            else:
                # 兜底：chunk 本身可能就是 response 对象（测试直调时）
                rid = chunk.get("id", "")
                model = chunk.get("model", "")
            return [
                {
                    "type": "message_start",
                    "message": {
                        "id": f"msg_{rid}",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            ]
        # chat 源
        return [
            {
                "type": "message_start",
                "message": {
                    "id": chunk.get("id", "").replace("chatcmpl-", "msg_"),
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": chunk.get("model", ""),
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": _chat_usage_to_anthropic_start(chunk.get("usage")),
                },
            }
        ]

    def open_block(
        self,
        block_type: str,
        *,
        tool_id: str = "",
        tool_name: str = "",
        tool_index: int | None = None,
    ) -> list[dict[str, Any]]:
        """坍缩 8 处仪式：先关旧块（若类型不同）再开新块。

        chat tool_use 需传入 tool_index 做索引映射；responses 侧 tool_index 为 None 时
        按常规 index 自增。已在同类型块中为 no-op（幂等）。
        """
        events: list[dict[str, Any]] = []
        # 同类型已开启，无需重复开块
        if self.content_block_started and self.current_content_type == block_type:
            # 对于 tool_use，虽同类型但可能是不同 tool_index 的新块，需继续处理；
            # 该分支仅对 thinking/text 的重复 open 为 no-op，tool_use 由调用方经 tool_index 区分。
            if block_type != "tool_use":
                return []
            # tool_use 同类型：需检查是否为新 index 的新块；若是，则先关旧块
            # 调用方会在 is_new_tool_call 时才调 open_block，因此此处若已在同类型块中，
            # 说明是已存在的 tool 索引的后续块，无需新 start（由 caller 控制不进此分支）
            # 为稳妥，若 caller 已判定 is_new，则此分支不会命中（因 caller 关后才调）
            # 故此处对 tool_use 同类型直接返回，避免误关
            return []

        # 非同类型且已有块在开：先关旧块（advance_index=True，块切换语义）
        if self.content_block_started:
            events.extend(self.close_block(advance_index=True))

        # 新块参数落盘
        self.content_block_started = True
        self.current_content_type = block_type

        if block_type == "tool_use" and tool_index is not None:
            # chat 源 tool_use 的索引映射（调用方需已判定 is_new）
            self.tool_call_indices[tool_index] = self.content_block_index

        # 发 start 事件
        if block_type == "thinking":
            events.append(
                {
                    "type": "content_block_start",
                    "index": self.content_block_index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                }
            )
        elif block_type == "text":
            events.append(
                {
                    "type": "content_block_start",
                    "index": self.content_block_index,
                    "content_block": {"type": "text", "text": ""},
                }
            )
        elif block_type == "tool_use":
            events.append(
                {
                    "type": "content_block_start",
                    "index": self.content_block_index,
                    "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
                }
            )
        else:
            # 防御：未知类型按 text 处理（不应到达）
            events.append(
                {
                    "type": "content_block_start",
                    "index": self.content_block_index,
                    "content_block": {"type": block_type, "text": ""},
                }
            )
        return events

    def append_delta(self, text: str, *, tool_index: int | None = None) -> list[dict[str, Any]]:
        """按当前块类型追加 delta。

        thinking → thinking_delta，text → text_delta，tool_use → input_json_delta。
        空字符串无产出（与原 8 仪式每处 ``if text:`` 守卫一致）。
        tool_use 的 index 取映射（chat）或当前块 index（responses fallback）。
        """
        if not text:
            return []
        if self.current_content_type == "thinking":
            return [
                {
                    "type": "content_block_delta",
                    "index": self.content_block_index,
                    "delta": {"type": "thinking_delta", "thinking": text},
                }
            ]
        if self.current_content_type == "text":
            return [
                {
                    "type": "content_block_delta",
                    "index": self.content_block_index,
                    "delta": {"type": "text_delta", "text": text},
                }
            ]
        if self.current_content_type == "tool_use":
            idx = self.content_block_index
            if tool_index is not None:
                idx = self.tool_call_indices.get(tool_index, self.content_block_index)
            return [
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": text},
                }
            ]
        # fallback：未设类型（responses function_call_arguments delta 未开块的兜底）
        # 原路径仅 mark started，不设类型，直接发 delta 用当前 index
        # 此时 current_content_type 可能仍为上一块或 None，保持 index 即可
        return [
            {
                "type": "content_block_delta",
                "index": self.content_block_index,
                "delta": {"type": "input_json_delta", "partial_json": text},
            }
        ]

    def ensure_tool_block(self, tc_index: int, tool_id: str, tool_name: str) -> list[dict[str, Any]]:
        """chat tool_calls 专属：按索引幂等开 tool_use 块。

        新索引 → 关旧块并分配新 index；已见索引 → 无产出（后续仅 append delta）。
        返回 content_block_start 事件列表（ may be empty）。
        """
        if tc_index in self.tool_call_indices:
            return []
        events: list[dict[str, Any]] = []
        if self.content_block_started:
            events.extend(self.close_block(advance_index=True))
        self.tool_call_indices[tc_index] = self.content_block_index
        self.current_content_type = "tool_use"
        self.content_block_started = True
        events.append(
            {
                "type": "content_block_start",
                "index": self.content_block_index,
                "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
            }
        )
        return events

    def close_block(self, *, advance_index: bool = True) -> list[dict[str, Any]]:
        """块切换原子操作：关块 + content_block_stop + 状态复位 + index 自增。

        thinking 块先补 signature_delta（空签名）再发 stop，与原 ``_close_content_block``
        逐字一致。前置条件：调用方已确认 ``content_block_started``（本方法内再做空守卫幂等）。
        """
        if not self.content_block_started:
            return []
        index = self.content_block_index
        events: list[dict[str, Any]] = []
        if self.current_content_type == "thinking":
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "signature_delta", "signature": ""},
                }
            )
        events.append({"type": "content_block_stop", "index": index})
        self.content_block_started = False
        if advance_index:
            self.content_block_index = index + 1
        return events

    def _build_message_stop_events(
        self,
        usage: dict[str, Any] | None,
        finish_reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """生成 message_delta + message_stop（幂等门控内）。

        finish_reason 优先显式传入，其次 pending_finish_reason，最后兜底 "stop"。
        usage 增量规则走 ``_chat_usage_output_delta``（累计转增量）。
        调用后置位 ``message_stop_sent`` 并清空 pending。
        仅在未发过 stop 时调用（调用方或本方法门控）。
        """
        if self.message_stop_sent:
            return []
        if finish_reason is None:
            finish_reason = self.pending_finish_reason or "stop"
        stop_reason = _render_finish(parse_chat_finish_reason(finish_reason))
        usage_output, prev = _chat_usage_output_delta(usage, self._prev_completion_tokens)
        self._prev_completion_tokens = prev
        events: list[dict[str, Any]] = [
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": usage_output},
            },
            {"type": "message_stop"},
        ]
        self.message_stop_sent = True
        self.pending_finish_reason = None
        return events

    def queue_stop(
        self,
        usage: dict[str, Any] | None,
        finish_reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """usage 等待 stash 归一（镜像 queue_final/release_pending）。

        finish 到而 usage 未到：扣留 message_stop（仅存 pending）；usage 到位（usage 非空
        或显式 finish 均可触发）则关块（若有）并补发 message_stop。幂等：已发 stop 则空。
        与原 ``_chat_build_message_stop_events`` + pending 逻辑逐字一致。
        """
        if self.message_stop_sent:
            return []
        # usage 已到：立即收尾
        if usage is not None:
            events: list[dict[str, Any]] = []
            if self.content_block_started:
                events.extend(self.close_block(advance_index=False))
            events.extend(self._build_message_stop_events(usage=usage, finish_reason=finish_reason))
            return events
        # usage 未到但有 finish：stash 等后续 usage chunk 或 finalize
        if finish_reason is not None:
            self.pending_finish_reason = finish_reason
            return []
        # 无 usage 无 finish（不应到达）：空
        return []

    def handle_usage_chunk(self, usage: dict[str, Any] | None) -> list[dict[str, Any]]:
        """usage-only chunk 到达时的补发路径（responses 无此分支，仅 chat）。

        若有 pending_finish_reason：带该 reason 补发；
        否则：罕见的 usage 在 finish 前到，关块后按 "stop" 兜底补发。
        已发 stop 或未 started 则空。
        """
        if not usage or not self.started:
            return []
        if self.message_stop_sent:
            return []
        if self.pending_finish_reason is not None:
            return self._build_message_stop_events(usage=usage)
        # 罕见：usage 在 finish 之前
        events: list[dict[str, Any]] = []
        if self.content_block_started:
            events.extend(self.close_block(advance_index=False))
        events.extend(self._build_message_stop_events(usage=usage))
        return events

    def finalize(self) -> list[dict[str, Any]]:
        """流末（[DONE]）补出 pending finish_reason 对应的 message_stop（chat 源专属）。

        finalize 幂等：已发 stop 或未 started 则空。responses 源 no-op 由调用方门控，
        本方法本身对 responses 语义亦为关块+补 stop（若误调仍保持安全），但对外承诺
        responses 源不调此方法（converter finalizes 门控）。
        与原 ``finalize_stream``（:969-994）逐字一致。
        """
        if self.message_stop_sent:
            return []
        if not self.started:
            return []
        events: list[dict[str, Any]] = []
        if self.content_block_started:
            events.extend(self.close_block(advance_index=False))
        events.extend(self._build_message_stop_events(usage=None))
        return events
