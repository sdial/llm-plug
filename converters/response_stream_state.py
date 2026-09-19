"""转换器流状态（Converter Stream State）唯一 schema 住所（ADR-0022 D0/D1）。

to_response 方向流式转换的 30 键会话状态升格为类型化 dataclass
（``ResponseStreamState``），chat→response 与 anthropic→response 两个方向状态机
（``stream_chat_to_response`` / ``stream_anthropic_to_response``）共享：

- 高频不变量转移方法（ADR-0022 D1）：``emit_created``（两拍协议唯一住所，幂等）/
  ``queue_final`` → ``release_pending``（usage 等待状态机）/ ``finalize``（空流
  伪造 ID 补偿）/ ``emit_text_done``（文本→工具切换防护）/ ``next_seq``
  （sequence_number 单调）；
- 聚合截断防护（``MAX_STREAM_AGGREGATE_TEXT_CHARS`` + ``append_aggregate_text`` /
  ``append_tool_arguments``）——改截断阈值只改本模块；
- per-item 文本累计、事件信封、completed output 装配等纯函数工具（以 state 为首参）。

状态仅存内存、从不序列化。方向文件函数签名收 ``state: ResponseStreamState``
（ADR-0022 D2，conv 参数消亡）：chunk 解析直接读写类型化字段，不变量转移走方法。
"""

import secrets
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from converters.response_ids import make_function_call_id, make_message_id
from converters.stream_events import build_response_item_done_events

# 流式聚合文本/工具参数的单项上限；超出即截断并打 aggregate_truncated 标记
MAX_STREAM_AGGREGATE_TEXT_CHARS = 1_000_000


@dataclass
class ResponseStreamState:
    """to_response 方向流式会话状态：30 键逐一映射 + 两拍协议标志 need_in_progress。"""

    response_id: str = ""
    model: str = ""
    created_at: int = 0
    reasoning_started: bool = False
    reasoning_id: str = ""
    message_id: str = ""
    output_index: int = 0
    response_created_sent: bool = False
    completed_sent: bool = False
    accumulated_text: str = ""
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)  # call_id -> {name, arguments, output_index}
    tool_call_index_to_id: dict[int, str] = field(default_factory=dict)
    reasoning_content: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    sequence_number: int = 0
    # 按输出项维护状态
    output_items: list[dict[str, Any]] = field(default_factory=list)  # {type, output_index, item_id}
    active_text_item_id: str | None = None
    active_text_output_index: int | None = None
    content_part_added_sent: bool = False
    # 每个文本 output item 各自的累计文本；completed output 里多个 message
    # 各带自己的文本，而不是全都塞全局 accumulated_text（防文本重复）
    item_texts: dict[int, str] = field(default_factory=dict)
    pending_finish_reason: str | None = None
    pending_final_events: list[dict[str, Any]] = field(default_factory=list)
    waiting_for_usage_after_finish: bool = False
    # Anthropic usage accumulation for cache-aware mapping
    anthropic_usage: dict[str, Any] = field(default_factory=dict)
    anthropic_content_blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    aggregate_truncated: bool = False
    # usage details 仅在 chunk usage 携带 dict 形态时由 _chat_usage_to_response_acc 重建写入
    input_tokens_details: dict[str, Any] | None = None
    output_tokens_details: dict[str, Any] | None = None
    # 两拍协议标志：response.created 同拍待注入的 response.in_progress
    # （原为 converter 实例上的游离属性 _need_in_progress，ADR-0022 D0 字段化）
    need_in_progress: bool = False

    # --- 高频不变量转移方法（ADR-0022 D1）---

    def pop_in_progress(self) -> dict[str, Any] | None:
        """response.created 同拍紧随的 response.in_progress 事件（严格协议要求）。

        无待注入时返回 None。
        """
        if not self.need_in_progress:
            return None
        self.need_in_progress = False
        return {
            "type": "response.in_progress",
            "response": {
                "id": self.response_id,
                "object": "response",
                "status": "in_progress",
                "model": self.model,
                "output": [],
            },
        }

    def emit_created(self) -> list[dict[str, Any]]:
        """两拍协议唯一住所：response.created 同拍紧随 response.in_progress。

        幂等（response_created_sent 门控，created 只发一次）；message_id 缺失时
        回填 response_id。已发过返回空列表。
        """
        if self.response_created_sent:
            return []
        self.response_created_sent = True
        if not self.message_id:
            self.message_id = self.response_id
        created = make_response_event(
            "response.created",
            response={
                "id": self.response_id,
                "object": "response",
                "status": "in_progress",
                "model": self.model,
                "output": [],
            },
        )
        self.need_in_progress = True
        in_progress = self.pop_in_progress()
        return [created, in_progress] if in_progress else [created]

    def emit_text_done(self) -> list[dict[str, Any]]:
        """关闭当前活跃的 text output；返回 output_text.done + content_part.done + output_item.done 序列。

        文本→function_call 切换前必须先关文本 output，否则严格模式客户端拒收；
        同时复位活跃文本状态，避免 finalize 重复发送相同的 done 事件。
        """
        item_id = self.active_text_item_id
        if item_id is None:
            return []
        text_output_index = self.active_text_output_index
        if text_output_index is None:
            text_output_index = 0
        text = item_text(self, text_output_index)
        # done 事件序列由 stream_sse 工厂单份合成（ADR-0016 D1 工厂 3）
        events = build_response_item_done_events("message", item_id=item_id, output_index=text_output_index, text=text)
        self.active_text_item_id = None
        self.active_text_output_index = None
        self.content_part_added_sent = False
        return events

    def build_final_events(self, finish_reason: str, mark_completed: bool = True) -> list[dict[str, Any]]:
        """组装 response.completed + 各 output item 的 *.done 收尾序列。"""
        if self.completed_sent:
            return []

        output = build_output_items(self)
        status = "completed" if finish_reason != "length" else "incomplete"
        usage_data = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.input_tokens_details:
            usage_data["input_tokens_details"] = self.input_tokens_details
        if self.output_tokens_details:
            usage_data["output_tokens_details"] = self.output_tokens_details
        completed_event = {
            "type": "response.completed",
            "response": {
                "id": self.response_id,
                "object": "response",
                "created_at": self.created_at,
                "model": self.model,
                "status": status,
                "output": output,
                "output_text": self.accumulated_text,
                "usage": usage_data,
            },
        }
        if finish_reason == "length":
            completed_event["response"]["incomplete_details"] = {"reason": "max_output_tokens"}

        done_events = []
        for entry in sorted(
            self.output_items,
            key=lambda item: item.get("output_index", 0),
        ):
            item_type = entry.get("type")
            output_index = entry.get("output_index", 0)
            item = build_completed_output_item(self, entry)
            if item_type == "message":
                item_id = entry.get("item_id")
                if output_index != self.active_text_output_index:
                    continue
                done_events.extend(
                    build_response_item_done_events(
                        "message",
                        item_id=item_id,
                        output_index=output_index,
                        text=item_text(self, output_index),
                        item=item or {},
                    )
                )
            elif item_type == "reasoning":
                item_id = entry.get("id") or self.reasoning_id
                done_events.extend(
                    build_response_item_done_events(
                        "reasoning",
                        item_id=item_id,
                        output_index=output_index,
                        text=self.reasoning_content,
                        item=item or {},
                    )
                )
            elif item_type == "function_call":
                call_id = entry.get("call_id", "")
                tc_data = self.tool_calls.get(call_id, {})
                done_events.extend(
                    build_response_item_done_events(
                        "function_call",
                        item_id=make_function_call_id(call_id),
                        output_index=output_index,
                        call_id=call_id,
                        name=tc_data.get("name", ""),
                        arguments=tc_data.get("arguments", ""),
                        item=item or {},
                    )
                )
        done_events.append(completed_event)
        if mark_completed:
            self.completed_sent = True
        return done_events

    def queue_final(self, finish_reason: str) -> list[dict[str, Any]]:
        """finish 后扣留 completed 等 usage：终事件序列入 pending，仅先发 done 序列。"""
        final_events = self.build_final_events(finish_reason=finish_reason, mark_completed=False)
        self.pending_finish_reason = finish_reason
        self.pending_final_events = final_events
        self.waiting_for_usage_after_finish = True
        if not final_events:
            return []
        return final_events[:-1]

    def release_pending(self) -> list[dict[str, Any]]:
        """usage 到位后补发扣留的 response.completed 并回填最新 usage。"""
        pending = self.pending_final_events or []
        if not pending:
            return []
        completed = pending[-1]
        if completed.get("type") == "response.completed":
            usage = completed["response"].get("usage", {})
            usage["input_tokens"] = self.input_tokens
            usage["output_tokens"] = self.output_tokens
            usage["total_tokens"] = self.total_tokens
            if self.input_tokens_details:
                usage["input_tokens_details"] = self.input_tokens_details
            if self.output_tokens_details:
                usage["output_tokens_details"] = self.output_tokens_details
            completed["response"]["usage"] = usage
        self.pending_final_events = []
        self.waiting_for_usage_after_finish = False
        self.completed_sent = True
        return [completed]

    def finalize(self) -> list[dict[str, Any]]:
        """chat 方向流收尾补偿（ADR-0022 D1，原 to_response.finalize_stream 手工段）。

        pending 扣留中先排空；已 completed 跳过；空流（无 chunk 到达）伪造
        response_id / message_id 后组装终事件。
        """
        if self.pending_final_events:
            return self.release_pending()
        if self.completed_sent:
            logger.debug("[FINALIZE] already completed, skipping")
            return []
        # 如果 response_id 为空，生成一个默认的
        if not self.response_id:
            self.response_id = f"resp_{secrets.token_hex(12)}"
            logger.debug(f"[FINALIZE] generated response_id={self.response_id}")
        if not self.message_id:
            self.message_id = self.response_id
        if self.message_id and not self.message_id.startswith("msg_"):
            self.message_id = make_message_id(
                self.response_id,
                self.message_id,
            )
        logger.debug(f"[FINALIZE] accumulated_text={repr(self.accumulated_text[:100])} response_created_sent={self.response_created_sent}")
        return self.build_final_events(finish_reason="stop")

    def next_seq(self) -> int:
        """sequence_number 单调递增。"""
        self.sequence_number += 1
        return self.sequence_number


def new_response_stream_state() -> ResponseStreamState:
    """初始流状态模板（唯一调用点：ToResponseConverter._reset_stream_state）。"""
    return ResponseStreamState()


def append_aggregate_text(state: ResponseStreamState, key: str, text: str) -> None:
    if not text:
        return
    current = getattr(state, key)
    remaining = MAX_STREAM_AGGREGATE_TEXT_CHARS - len(current)
    if remaining <= 0:
        state.aggregate_truncated = True
        return
    setattr(state, key, current + text[:remaining])
    if len(text) > remaining:
        state.aggregate_truncated = True


def append_tool_arguments(state: ResponseStreamState, call_id: str, text: str) -> None:
    if not text:
        return
    tool_call = state.tool_calls.get(call_id)
    if not tool_call:
        return
    current = tool_call.get("arguments", "")
    remaining = MAX_STREAM_AGGREGATE_TEXT_CHARS - len(current)
    if remaining <= 0:
        state.aggregate_truncated = True
        return
    tool_call["arguments"] = current + text[:remaining]
    if len(text) > remaining:
        state.aggregate_truncated = True


def append_item_text(state: ResponseStreamState, output_index: int | None, text: str) -> None:
    """把文本增量累计到指定 output item 名下（per-item，防多 message 文本重复）。

    以 output_index 为 key：item_id 在 Chat 方向会被多个 message item 复用
    （同一 message_id），output_index 才是输出项的唯一标识。
    """
    if not text or output_index is None:
        return
    state.item_texts[output_index] = state.item_texts.get(output_index, "") + text


def item_text(state: ResponseStreamState, output_index: int | None) -> str:
    """读取指定 output item 的累计文本；无记录时回退全局 accumulated_text。"""
    if output_index is not None:
        text = state.item_texts.get(output_index)
        if text is not None:
            return text
    return state.accumulated_text


def make_response_event(event_type: str, **payload) -> dict[str, Any]:
    return {"type": event_type, **payload}


def build_output_items(state: ResponseStreamState) -> list[dict[str, Any]]:
    output = []
    if state.output_items:
        for entry in sorted(
            state.output_items,
            key=lambda item: item.get("output_index", 0),
        ):
            item = build_completed_output_item(state, entry)
            if item is not None:
                output.append(item)
    elif state.reasoning_content:
        output.append(
            {
                "type": "reasoning",
                "id": state.reasoning_id,
                "summary": [],
                "content": [
                    {
                        "type": "reasoning_text",
                        "text": state.reasoning_content,
                    }
                ],
            }
        )
    if not output:
        item_id = state.message_id
        if not item_id.startswith("msg_"):
            item_id = make_message_id(state.response_id or "resp_stream", item_id)
        output.append(
            {
                "type": "message",
                "id": item_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            }
        )
    return output


def build_completed_output_item(state: ResponseStreamState, entry: dict[str, Any]) -> dict[str, Any] | None:
    item_type = entry.get("type")
    if item_type == "reasoning":
        if not state.reasoning_content:
            return None
        return {
            "type": "reasoning",
            "id": entry.get("id") or state.reasoning_id,
            "summary": [],
            "content": [
                {
                    "type": "reasoning_text",
                    "text": state.reasoning_content,
                }
            ],
        }
    if item_type == "message":
        item_id = entry.get("item_id") or state.message_id
        if not item_id.startswith("msg_"):
            item_id = make_message_id(state.response_id or "resp_stream", item_id)
        return {
            "type": "message",
            "id": item_id,
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": item_text(state, entry.get("output_index")),
                }
            ],
        }
    if item_type == "function_call":
        call_id = entry.get("call_id", "")
        tc_data = state.tool_calls.get(call_id)
        if not tc_data:
            return None
        return {
            "type": "function_call",
            "id": make_function_call_id(call_id),
            "call_id": call_id,
            "name": tc_data["name"],
            "arguments": tc_data["arguments"],
            "status": "completed",
        }
    return None
