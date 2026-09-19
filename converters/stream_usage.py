"""流式 usage 方言提取（ADR-0015 D1）：三协议 usage 增量语义的单一住所。

Anthropic（message_start 初值 + message_delta 终值覆写）、OpenAI Chat（chunk 顶层
usage 覆写）、OpenAI Responses（response.usage 嵌套）三种方言与各自 finish_reason
提取（message_delta.stop_reason / choices[0].finish_reason / response.status）收敛
于此。两个入口：

- ``_StreamUsageAccumulator``：per-request 累积器，流式骨架逐块 ``feed(chunk)``，
  增量提取 token 用量与 finish_reason（避免 finally 中二次遍历）；source_type 传
  ``_OPENAI_FAMILY_AUTO`` 时为流重建方言（Chat 顶层 / Responses 嵌套统一提取，
  供 ``stream_reconstruct`` 逐 chunk usage 聚合委托）；
- ``_normalize_full_response_usage``：一次性归一，non-SSE 兜底整块 / 非流式整响应
  （``non_stream_executor`` 同走此入口）；
- ``_collect_anthropic_stream_usage``：流重建的 Anthropic usage 原样聚合
  （message_start / message_delta 的原始 usage dict 覆写合并，不做归一——重建体
  需原样存档上游字段）；
- converter 侧流式 usage 规则纯函数（ADR-0016 D3）：``_chat_usage_to_anthropic_start`` /
  ``_chat_usage_output_delta`` / ``_responses_usage_output_final`` /
  ``_anthropic_usage_raw_merge`` / ``_anthropic_usage_start_accumulate`` /
  ``_anthropic_usage_delta_overwrite`` / ``_chat_usage_to_response_acc``——
  (chunk usage 形态 + 当前累计) → 新累计；3 个转换器的流式状态机只持累计状态、
  调用这些规则，不再内联规则本体（何时累计 / 增量 vs 终值 / usage-only chunk
  在 finish 之后的口径单一住所）。finish 门控（message_stop_sent /
  waiting_for_usage_after_finish 等流控）属状态机职责，不在此处。

必须逐字保留的方言语义（表驱动测试钉死：tests/streaming/test_stream_usage.py）：
- 字段名优先序按协议分叉：Anthropic 先 ``input_tokens`` 后 ``prompt_tokens``；
  Chat 先 ``prompt_tokens`` 后 ``input_tokens``；Responses 嵌套取 ``response.usage``；
- Anthropic ``message_delta`` 的 ``output_tokens`` 为终值覆写；缓存字段 start/delta
  双报防重复计数（仅当此前未并入过缓存时补加）；
- 全缓存命中：start/delta 的 ``input_tokens`` 均 0 → 总输入 = cache_read + cache_creation；
- DeepSeek 型网关：缓存字段仅在 ``message_delta``，需并入总输入，避免
  ``cache_read > input_tokens`` 倒挂；
- 一次性归一：``prompt_tokens`` 缺失且无 ``input_tokens_details`` 时视为纯 Anthropic
  语义，input_tokens 补加缓存两项；
- usage 为 null / 缺失时全部保持 0（或回退现值），不抛错。

私有接缝（ADR-0015 D0 约定）：不进 ``__all__``，测试可直接 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from converters.usage import cache_token_details
from models.api_types import APIType

# 流重建方言（stream_reconstruct.build_openai_stream_response 专用 source_type）：
# 上游可能是 Chat 也可能是 Responses，逐 chunk 统一提取——顶层 usage（Chat 形态）
# 优先，缺失时回退嵌套 response.usage（Responses 形态）。
_OPENAI_FAMILY_AUTO = "openai-family-auto"


@dataclass
class _StreamUsageSummary:
    """统一 usage 摘要：三协议方言（流式增量 / 整块归一）的最终产出。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    finish_reason: str | None = None


class _StreamUsageAccumulator:
    """per-request usage 累积器（ADR-0015 D1）：流式骨架逐块 feed，方言单一住所。

    source_type 决定方言分支；chunk 形态与方言不匹配时为 no-op，usage 为
    null / 缺失时保持现值，均不抛错。
    """

    def __init__(self, source_type: str):
        self.source_type = source_type
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0
        self.finish_reason: str | None = None
        # 透传字段（Chat 方言 / 流重建方言需要）：上游 total_tokens 与
        # prompt/completion_tokens_details 原样保留，缺省 None
        self.total_tokens: int | None = None
        self.prompt_tokens_details: dict[str, Any] | None = None
        self.completion_tokens_details: dict[str, Any] | None = None

    def feed(self, chunk: Any) -> None:
        """喂入一个上游 chunk，按协议方言增量更新摘要。"""
        if not isinstance(chunk, dict):
            return
        if self.source_type == APIType.ANTHROPIC.value:
            self._feed_anthropic(chunk)
        elif self.source_type == APIType.OPENAI_RESPONSE.value:
            self._feed_responses(chunk)
            self._feed_choices_finish_reason(chunk)
        elif self.source_type == _OPENAI_FAMILY_AUTO:
            self._feed_openai_family_auto(chunk)
            self._feed_choices_finish_reason(chunk)
        else:
            self._feed_chat(chunk)
            self._feed_choices_finish_reason(chunk)

    def _feed_anthropic(self, chunk: dict[str, Any]) -> None:
        if chunk.get("type") == "message_start":
            start_usage = chunk.get("message", {}).get("usage", {})
            self.input_tokens = start_usage.get("input_tokens", 0)
            if self.input_tokens == 0:
                self.input_tokens = start_usage.get("prompt_tokens", 0)
            else:
                self.input_tokens += start_usage.get("cache_creation_input_tokens", 0) + start_usage.get("cache_read_input_tokens", 0)
            token_details = cache_token_details(start_usage)
            self.cache_read_input_tokens = token_details["cache_read_input_tokens"]
            self.cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
        elif chunk.get("type") == "message_delta":
            delta_usage = chunk.get("usage", {})
            # output_tokens 终值覆写（非累加）
            self.output_tokens = delta_usage.get("output_tokens", self.output_tokens)
            if self.input_tokens == 0:
                self.input_tokens = delta_usage.get("input_tokens", 0)
                if self.input_tokens == 0:
                    self.input_tokens = delta_usage.get("prompt_tokens", 0)
                if self.input_tokens == 0:
                    # Anthropic 全缓存命中：message_start 与 message_delta 的
                    # input_tokens 均为 0，总输入 = 缓存 token 之和，
                    # 与非流式路径（usage 归一同）保持一致。
                    self.input_tokens = self.cache_read_input_tokens + self.cache_creation_input_tokens
                else:
                    self.input_tokens += self.cache_read_input_tokens + self.cache_creation_input_tokens
            token_details = cache_token_details(delta_usage)
            if token_details["cache_read_input_tokens"] or token_details["cache_creation_input_tokens"]:
                # 部分 Anthropic 兼容网关（如 DeepSeek /anthropic 端点）只把缓存字段
                # 放在 message_delta 的 usage 里，message_start 仅含 input_tokens。
                # 此时总输入尚未包含缓存，需并入，否则会记录出 cache_read > input_tokens
                # 的倒挂。仅当之前未并入过缓存时补加（防 start/delta 双报重复计数）。
                if self.cache_read_input_tokens == 0 and self.cache_creation_input_tokens == 0:
                    self.input_tokens += token_details["cache_read_input_tokens"] + token_details["cache_creation_input_tokens"]
                self.cache_read_input_tokens = token_details["cache_read_input_tokens"]
                self.cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
            fr = chunk.get("delta", {}).get("stop_reason")
            if fr:
                self.finish_reason = fr

    def _feed_responses(self, chunk: dict[str, Any]) -> None:
        # OpenAI Responses 上游：usage 嵌套在 response.completed / response.failed
        # 事件的 response.usage 里，与 Chat Completions 的顶层 usage 字段位置不同，
        # 必须单独处理，否则 token 全为 0。
        resp_obj = chunk.get("response")
        if isinstance(resp_obj, dict):
            usage = resp_obj.get("usage")
            if usage:
                logger.info(f"[STREAM USAGE] upstream returned usage: {usage}")
                self.input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", self.input_tokens))
                self.output_tokens = usage.get("output_tokens", usage.get("completion_tokens", self.output_tokens))
                token_details = cache_token_details(usage)
                self.cache_read_input_tokens = token_details["cache_read_input_tokens"]
                self.cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
            # response.completed 事件携带最终 status，作为 finish_reason
            status = resp_obj.get("status")
            if status:
                self.finish_reason = status

    def _feed_chat(self, chunk: dict[str, Any]) -> None:
        # OpenAI Chat Completions 上游：usage 在 chunk 顶层
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._apply_chat_usage(usage)

    def _feed_openai_family_auto(self, chunk: dict[str, Any]) -> None:
        # 流重建方言（build_openai_stream_response）：上游可能是 Chat 也可能是
        # Responses，顶层 usage（Chat 形态）优先，缺失时回退嵌套 response.usage
        # （Responses 形态）；字段名优先序与 Chat 一致（prompt_tokens 优先），
        # 与迁移前 stream_reconstruct 的统一提取逐字一致。
        usage = chunk.get("usage")
        if not isinstance(usage, dict):
            resp_obj = chunk.get("response")
            usage = resp_obj.get("usage") if isinstance(resp_obj, dict) else None
        if isinstance(usage, dict):
            self._apply_chat_usage(usage)

    def _apply_chat_usage(self, usage: dict[str, Any]) -> None:
        """Chat 形态 usage 应用（_feed_chat 与流重建方言共享的单点提取）。"""
        logger.info(f"[STREAM USAGE] upstream returned usage: {usage}")
        self.input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", self.input_tokens))
        self.output_tokens = usage.get("completion_tokens", usage.get("output_tokens", self.output_tokens))
        token_details = cache_token_details(usage)
        self.cache_read_input_tokens = token_details["cache_read_input_tokens"]
        self.cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
        # 优先使用上游的 total_tokens
        if usage.get("total_tokens") is not None:
            self.total_tokens = usage["total_tokens"]
        # 透传 prompt_tokens_details / completion_tokens_details
        pd = usage.get("prompt_tokens_details")
        if isinstance(pd, dict):
            self.prompt_tokens_details = pd
        cd = usage.get("completion_tokens_details")
        if isinstance(cd, dict):
            self.completion_tokens_details = cd

    def _feed_choices_finish_reason(self, chunk: dict[str, Any]) -> None:
        choices = chunk.get("choices", [])
        if choices and isinstance(choices[0], dict):
            fr = choices[0].get("finish_reason")
            if fr:
                self.finish_reason = fr


def _normalize_full_response_usage(
    full_response: dict[str, Any],
    *,
    cur_input_tokens: int = 0,
    cur_output_tokens: int = 0,
    cur_finish_reason: str | None = None,
) -> _StreamUsageSummary:
    """一次性归一：non-SSE 兜底整块 / 非流式整响应 → 统一摘要（单次调用）。

    字段名优先序与 Anthropic 缓存补加语义见模块 docstring；usage 为 null /
    缺失时回退调用方现值（默认 0），不抛错。finish_reason 优先序：
    choices[0].finish_reason → 顶层 stop_reason → 调用方现值。
    """
    full_usage = full_response.get("usage") or {}
    summary = _StreamUsageSummary(
        input_tokens=full_usage.get("prompt_tokens", full_usage.get("input_tokens", cur_input_tokens)),
        output_tokens=full_usage.get("completion_tokens", full_usage.get("output_tokens", cur_output_tokens)),
        finish_reason=cur_finish_reason,
    )
    token_details = cache_token_details(full_usage)
    # 非 SSE JSON / 非流式的 Anthropic 响应：input_tokens 不含 cache，归一化为总输入
    if "prompt_tokens" not in full_usage and "input_tokens_details" not in full_usage:
        summary.input_tokens += token_details["cache_creation_input_tokens"] + token_details["cache_read_input_tokens"]
    summary.cache_read_input_tokens = token_details["cache_read_input_tokens"]
    summary.cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
    choices = full_response.get("choices", [])
    if choices and isinstance(choices[0], dict):
        fr = choices[0].get("finish_reason")
        if fr:
            summary.finish_reason = fr
    stop_reason = full_response.get("stop_reason")
    if stop_reason:
        summary.finish_reason = stop_reason
    return summary


def _collect_anthropic_stream_usage(chunks: list[Any]) -> dict[str, Any]:
    """流重建方言：Anthropic 逐 chunk usage 原样聚合（build_anthropic_stream_response 委托）。

    与累积器的归一语义不同：重建体需原样存档上游 usage 字段，因此不做缓存补加 /
    字段名归一——message_start 的 ``message.usage`` 先入，message_delta 的 ``usage``
    覆写合并（后到字段覆盖先到），其余 chunk 为 no-op。
    """
    usage: dict[str, Any] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("type") == "message_start":
            msg_usage = chunk.get("message", {}).get("usage")
            if isinstance(msg_usage, dict):
                usage.update(msg_usage)
        elif chunk.get("type") == "message_delta":
            if isinstance(chunk.get("usage"), dict):
                usage.update(chunk["usage"])
    return usage


# ─── converter 侧流式 usage 规则（ADR-0016 D3）───
#
# 纯函数：(chunk usage 形态 + 当前累计) → 新累计。3 个转换器（to_chat /
# to_anthropic / to_response）的流式状态机只持累计状态、调用这些规则；新增上游
# usage 方言时规则改一处，转换器与 executor 两侧口径不再漂移。
# 全部 null / 缺失容错：usage 非 dict / 缺失时保持现值，不抛错。

# to_response 侧 Anthropic 累计白名单：Responses 入口只消费这四个键
_ANTHROPIC_ACCUMULATED_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def _chat_usage_to_anthropic_start(usage: Any) -> dict[str, int]:
    """Chat chunk usage → Anthropic message_start.usage（to_anthropic 流开拍播种）。

    只取 prompt_tokens 作为 input_tokens；output 与缓存字段在流开拍恒为 0
    （Chat 的流式 usage 在收尾 chunk 才出现，message_start 阶段无从得知）。
    """
    usage = usage if isinstance(usage, dict) else {}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _chat_usage_output_delta(usage: Any, prev_cumulative: int) -> tuple[int, int]:
    """Chat 累计 completion_tokens → Anthropic message_delta 增量 output_tokens（增量规则）。

    Chat 上游的 completion_tokens 是累计终值，Anthropic 的 message_delta.usage
    是增量：delta = cumulative - prev，并返回新 prev 供下拍相减。usage 缺失时
    delta 为 0 且 prev 不动；usage 缺 completion_tokens 时 cumulative 记 0
    （delta 为负、prev 归零——与迁移前逐字一致，行为零变化）。
    """
    if not isinstance(usage, dict):
        return 0, prev_cumulative
    cumulative = usage.get("completion_tokens", 0)
    return cumulative - prev_cumulative, cumulative


def _responses_usage_output_final(usage: Any) -> int:
    """Responses response.usage.output_tokens 终值提取（终值规则，无累计）。"""
    if not isinstance(usage, dict):
        return 0
    return usage.get("output_tokens", 0)


def _anthropic_usage_raw_merge(current: dict[str, Any], incoming: Any) -> dict[str, Any]:
    """Anthropic 原始 usage 覆写合并（to_chat：message_start 播种 + message_delta 覆写）。

    全字段原样保留（含 server_tool_use 等非数值键），后到字段覆盖先到；
    返回新 dict，不原地改 current。
    """
    merged = dict(current)
    if isinstance(incoming, dict):
        merged.update(incoming)
    return merged


def _anthropic_usage_start_accumulate(current: dict[str, Any], msg_usage: Any) -> dict[str, Any]:
    """Anthropic message_start usage 加法播种（to_response：白名单键增量累计）。

    与 raw merge 的分界：Responses 入口只消费四键白名单，且 message_start
    阶段做加法累计（existing + incoming）而非覆写。
    """
    merged = dict(current)
    if not isinstance(msg_usage, dict):
        return merged
    for key in _ANTHROPIC_ACCUMULATED_USAGE_KEYS:
        if key in msg_usage:
            merged[key] = merged.get(key, 0) + msg_usage[key]
    return merged


def _anthropic_usage_delta_overwrite(current: dict[str, Any], delta_usage: Any) -> dict[str, Any]:
    """Anthropic message_delta usage 终值覆写（to_response：白名单键）。

    message_delta 的 usage 是累计终值而非增量：直接覆盖，不与 message_start
    播种值相加，否则初始值被重复累加。
    """
    merged = dict(current)
    if not isinstance(delta_usage, dict):
        return merged
    for key in _ANTHROPIC_ACCUMULATED_USAGE_KEYS:
        if key in delta_usage:
            merged[key] = delta_usage[key]
    return merged


def _chat_usage_to_response_acc(usage: Any, acc: dict[str, Any]) -> dict[str, Any]:
    """Chat usage → Responses 流式累计（to_response：终值覆写 + 现值回退 + details 重建）。

    prompt/completion/total 三键终值覆写、缺失回退 acc 现值（usage-only chunk
    在 finish 之后到达时部分字段缺失，保持已累计值）；details 仅在 usage 携带
    dict 形态时重建（cached_tokens / reasoning_tokens，内层缺失回退 0）。
    返回增量 dict（不含未变化的键），调用方 update 进状态；usage 无效 /
    为空时返回空 dict（no-op，与迁移前 ``if usage:`` 守卫逐字一致）。入参不被
    原地修改。
    """
    if not usage or not isinstance(usage, dict):
        return {}
    new: dict[str, Any] = {
        "input_tokens": usage.get("prompt_tokens", acc.get("input_tokens", 0)),
        "output_tokens": usage.get("completion_tokens", acc.get("output_tokens", 0)),
        "total_tokens": usage.get("total_tokens", acc.get("total_tokens", 0)),
    }
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        new["input_tokens_details"] = {"cached_tokens": prompt_details.get("cached_tokens", 0)}
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        new["output_tokens_details"] = {"reasoning_tokens": completion_details.get("reasoning_tokens", 0)}
    return new
