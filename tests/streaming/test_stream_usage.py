"""流式 usage 方言模块（ADR-0015 D1）表驱动测试：协议 × 形态 → 期望 token 值。

三个入口都覆盖：
- 累积器 ``_StreamUsageAccumulator``（流式骨架逐块 feed，多次调用）；
- 累积器流重建方言（``_OPENAI_FAMILY_AUTO`` source_type：Chat 顶层 / Responses
  嵌套统一提取，``stream_reconstruct.build_openai_stream_response`` 委托入口）；
- 一次性归一 ``_normalize_full_response_usage``（non-SSE 兜底整块 / 非流式整响应，
  ``non_stream_executor`` 委托入口）；
- Anthropic 原样聚合 ``_collect_anthropic_stream_usage``（``build_anthropic_stream_response``
  委托入口：重建体原样存档上游 usage 字段，不做归一）。

矩阵行 = 一个上游形态的完整期望，spec D1 列出的方言语义逐条钉死：
message_start / message_delta / 顶层 usage / 嵌套 response.usage / 全缓存命中 /
网关仅 delta 缓存 / usage null / non-SSE 整块。直接 import 私有接缝（不进
``__all__``），先例 tests/streaming/test_error_builders.py。
"""

import pytest

from converters.stream_usage import (
    _OPENAI_FAMILY_AUTO,
    _anthropic_usage_delta_overwrite,
    _anthropic_usage_raw_merge,
    _anthropic_usage_start_accumulate,
    _chat_usage_output_delta,
    _chat_usage_to_anthropic_start,
    _chat_usage_to_response_acc,
    _collect_anthropic_stream_usage,
    _normalize_full_response_usage,
    _responses_usage_output_final,
    _StreamUsageAccumulator,
)

# ─── 累积器矩阵：协议 × 形态 → 期望 token 值（多次 feed）───

ACCUMULATOR_CASES = [
    # ── Anthropic：message_start 初值 / message_delta 终值覆写 ──
    pytest.param(
        "anthropic",
        [{"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}],
        {"input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-message_start-basic",
    ),
    pytest.param(
        "anthropic",
        # 字段名优先序：先 input_tokens 后 prompt_tokens
        [{"type": "message_start", "message": {"usage": {"prompt_tokens": 7}}}],
        {"input_tokens": 7, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-message_start-prompt_tokens-fallback",
    ),
    pytest.param(
        "anthropic",
        # message_start 缓存字段并入总输入（input_tokens 不含 cache）
        [{"type": "message_start", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}}}],
        {"input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2, "finish_reason": None},
        id="anthropic-message_start-with-cache",
    ),
    pytest.param(
        "anthropic",
        # usage null / 缺失容错：全 0，不抛错
        [{"type": "message_start", "message": {}}, {"type": "message_delta", "usage": {}}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-usage-null-tolerant",
    ),
    pytest.param(
        "anthropic",
        # 非 message_start/message_delta 形态为 no-op
        [{"type": "message_start", "message": {"usage": {"input_tokens": 4}}}, {"type": "content_block_delta", "index": 0, "delta": {}}],
        {"input_tokens": 4, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-other-chunk-noop",
    ),
    pytest.param(
        "anthropic",
        # message_delta 的 output_tokens 为终值覆写（非累加，多次 feed 最后一次生效）
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "message_delta", "usage": {"output_tokens": 42}},
            {"type": "message_delta", "usage": {"output_tokens": 50}},
        ],
        {"input_tokens": 10, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-message_delta-output-terminal-overwrite",
    ),
    pytest.param(
        "anthropic",
        # message_delta 的 stop_reason → finish_reason
        [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "end_turn"},
        id="anthropic-message_delta-stop_reason",
    ),
    pytest.param(
        "anthropic",
        # 全缓存命中：start/delta 的 input_tokens 均 0 → 总输入 = cache_read + cache_creation
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 0, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 1}}},
            {"type": "message_delta", "usage": {"output_tokens": 9}},
        ],
        {"input_tokens": 5, "output_tokens": 9, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 1, "finish_reason": None},
        id="anthropic-full-cache-hit-merge",
    ),
    pytest.param(
        "anthropic",
        # DeepSeek 型网关：缓存字段仅在 message_delta，需并入总输入（防 cache_read > input_tokens 倒挂）
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 6}}},
            {"type": "message_delta", "usage": {"output_tokens": 2, "cache_read_input_tokens": 3}},
        ],
        {"input_tokens": 9, "output_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-gateway-delta-only-cache",
    ),
    pytest.param(
        "anthropic",
        # start/delta 双报防重复计数：start 已并入缓存 → delta 再报不重复加
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 2}}},
            {"type": "message_delta", "usage": {"output_tokens": 1, "cache_read_input_tokens": 2}},
        ],
        {"input_tokens": 7, "output_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-start-delta-double-report-prevention",
    ),
    pytest.param(
        "anthropic",
        # message_delta 先 input_tokens 后 prompt_tokens；非零时并入 start 已并入的缓存
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 0, "cache_read_input_tokens": 2}}},
            {"type": "message_delta", "usage": {"input_tokens": 3, "output_tokens": 1}},
        ],
        {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-delta-input-nonzero-merges-start-cache",
    ),
    pytest.param(
        "anthropic",
        # message_delta 先 input_tokens 后 prompt_tokens 兜底
        [
            {"type": "message_start", "message": {"usage": {}}},
            {"type": "message_delta", "usage": {"prompt_tokens": 8, "output_tokens": 1}},
        ],
        {"input_tokens": 8, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="anthropic-delta-prompt_tokens-fallback",
    ),
    # ── OpenAI Chat Completions：chunk 顶层 usage ──
    pytest.param(
        "openai-chat-completions",
        [{"usage": {"prompt_tokens": 8, "completion_tokens": 3}}],
        {"input_tokens": 8, "output_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-usage-top-level",
    ),
    pytest.param(
        "openai-chat-completions",
        # 字段名优先序：先 prompt_tokens 后 input_tokens
        [{"usage": {"completion_tokens": 2, "input_tokens": 6}}],
        {"input_tokens": 6, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-input_tokens-fallback",
    ),
    pytest.param(
        "openai-chat-completions",
        # prompt_tokens_details.cached_tokens → cache_read
        [{"usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}}],
        {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-prompt_tokens_details-cached",
    ),
    pytest.param(
        "openai-chat-completions",
        # usage null（如 NVIDIA z-ai/glm-5.2）容错：跳过且后续 chunk 仍生效
        [{"id": "x", "usage": None}, {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}],
        {"input_tokens": 3, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-usage-null-tolerant",
    ),
    pytest.param(
        "openai-chat-completions",
        [{"id": "x"}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-usage-missing-noop",
    ),
    pytest.param(
        "openai-chat-completions",
        # usage 非 dict（如 list）被 isinstance 守卫忽略，不抛错
        [{"usage": ["bad"]}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-usage-non-dict-ignored",
    ),
    pytest.param(
        "openai-chat-completions",
        # choices[0].finish_reason → finish_reason
        [{"choices": [{"finish_reason": "stop"}]}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "stop"},
        id="chat-choices-finish_reason",
    ),
    pytest.param(
        "openai-chat-completions",
        # 终值覆写 + 字段缺省回退现值：后到 usage 缺 prompt_tokens 时保留此前 input
        [{"usage": {"prompt_tokens": 5, "completion_tokens": 1}}, {"usage": {"completion_tokens": 7}}],
        {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-terminal-overwrite-keeps-previous-input",
    ),
    pytest.param(
        "openai-chat-completions",
        # 非 dict chunk 为 no-op
        ["not-a-dict", {"usage": {"prompt_tokens": 2, "completion_tokens": 1}}],
        {"input_tokens": 2, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="chat-non-dict-chunk-noop",
    ),
    # ── OpenAI Responses：嵌套 response.usage ──
    pytest.param(
        "openai-response",
        # usage 嵌套在 response.completed / response.failed 事件的 response.usage 里
        [{"response": {"usage": {"input_tokens": 12, "output_tokens": 4}, "status": "completed"}}],
        {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "completed"},
        id="responses-nested-usage-and-status",
    ),
    pytest.param(
        "openai-response",
        # 嵌套方言同样先 input_tokens/completion 优先序回退
        [{"response": {"usage": {"prompt_tokens": 6, "completion_tokens": 2}}}],
        {"input_tokens": 6, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="responses-nested-fallback-field-names",
    ),
    pytest.param(
        "openai-response",
        # input_tokens_details.cached_tokens → cache_read
        [{"response": {"usage": {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 5}}}}],
        {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="responses-nested-cached-tokens",
    ),
    pytest.param(
        "openai-response",
        # usage 缺失时 token 全 0，但 status 仍作为 finish_reason 捕获
        [{"response": {"status": "completed"}}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "completed"},
        id="responses-usage-missing-status-captured",
    ),
    pytest.param(
        "openai-response",
        [{"response": {"usage": None, "status": "failed"}}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "failed"},
        id="responses-usage-null-tolerant",
    ),
    pytest.param(
        "openai-response",
        # choices[0].finish_reason 与 response.status 同表（Chat 共享路径）
        [{"response": {}, "choices": [{"finish_reason": "stop"}]}],
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "stop"},
        id="responses-choices-finish_reason-shared",
    ),
    pytest.param(
        "openai-response",
        # 终值覆写 + 字段缺省回退现值（嵌套形态）
        [
            {"response": {"usage": {"input_tokens": 3, "output_tokens": 1}}},
            {"response": {"usage": {"output_tokens": 9}}},
        ],
        {"input_tokens": 3, "output_tokens": 9, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="responses-terminal-overwrite-keeps-previous-input",
    ),
    # ── 流重建方言（_OPENAI_FAMILY_AUTO）：Chat 顶层 / Responses 嵌套统一提取 ──
    pytest.param(
        _OPENAI_FAMILY_AUTO,
        # Chat 上游：chunk 顶层 usage
        [{"usage": {"prompt_tokens": 8, "completion_tokens": 3}}],
        {"input_tokens": 8, "output_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="reconstruct-chat-top-level-usage",
    ),
    pytest.param(
        _OPENAI_FAMILY_AUTO,
        # Responses 上游：usage 嵌套在 response.completed / response.failed 事件的 response.usage
        [{"response": {"usage": {"input_tokens": 12, "output_tokens": 4}}}],
        {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="reconstruct-responses-nested-usage",
    ),
    pytest.param(
        _OPENAI_FAMILY_AUTO,
        # 流重建方言字段名优先序与 Chat 一致（prompt_tokens 优先，与迁移前
        # stream_reconstruct 的统一提取逐字一致）
        [{"response": {"usage": {"prompt_tokens": 6, "input_tokens": 99}}}],
        {"input_tokens": 6, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="reconstruct-nested-chat-first-field-priority",
    ),
    pytest.param(
        _OPENAI_FAMILY_AUTO,
        # 顶层 usage 优先于嵌套 response.usage（同 chunk 两处并存时不双取）
        [{"usage": {"prompt_tokens": 5, "completion_tokens": 1}, "response": {"usage": {"input_tokens": 99, "output_tokens": 99}}}],
        {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="reconstruct-top-level-usage-preferred",
    ),
    pytest.param(
        _OPENAI_FAMILY_AUTO,
        # 顶层 usage null 时回退嵌套 response.usage（Chat→Responses 混合形态容错）
        [{"usage": None, "response": {"usage": {"input_tokens": 7, "output_tokens": 2}}}],
        {"input_tokens": 7, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="reconstruct-null-top-level-falls-back-to-nested",
    ),
    pytest.param(
        _OPENAI_FAMILY_AUTO,
        # prompt_tokens_details.cached_tokens → cache_read（与 Chat 方言同表）
        [{"usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}}],
        {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="reconstruct-prompt_tokens_details-cached",
    ),
    pytest.param(
        _OPENAI_FAMILY_AUTO,
        # 非 usage chunk（choices delta）为 no-op；多 chunk 终值覆写
        [
            {"choices": [{"delta": {"content": "hi"}}]},
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            {"usage": {"prompt_tokens": 1, "completion_tokens": 6}},
        ],
        {"input_tokens": 1, "output_tokens": 6, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="reconstruct-multi-chunk-terminal-overwrite",
    ),
]


@pytest.mark.parametrize(("source_type", "feeds", "expected"), ACCUMULATOR_CASES)
def test_accumulator_matrix(source_type, feeds, expected):
    """协议 × 形态 → 期望 token 值：累积器多次 feed 入口。"""
    acc = _StreamUsageAccumulator(source_type)
    for chunk in feeds:
        acc.feed(chunk)
    actual = {
        "input_tokens": acc.input_tokens,
        "output_tokens": acc.output_tokens,
        "cache_read_input_tokens": acc.cache_read_input_tokens,
        "cache_creation_input_tokens": acc.cache_creation_input_tokens,
        "finish_reason": acc.finish_reason,
    }
    assert actual == expected


# ─── 流重建透传字段：total_tokens / prompt·completion_tokens_details（Chat 方言共享）───

PASSTHROUGH_CASES = [
    pytest.param(
        [
            {
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "prompt_tokens_details": {"cached_tokens": 900},
                    "completion_tokens_details": {"reasoning_tokens": 30},
                }
            }
        ],
        {"total_tokens": 1050, "prompt_tokens_details": {"cached_tokens": 900}, "completion_tokens_details": {"reasoning_tokens": 30}},
        id="upstream-total-and-details-preserved",
    ),
    pytest.param(
        [{"usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 50}}}],
        # 无上游 total_tokens → 保持 None（重建体回退 prompt + completion 自加）
        {"total_tokens": None, "prompt_tokens_details": {"cached_tokens": 50}, "completion_tokens_details": None},
        id="missing-total-falls-back-none",
    ),
    pytest.param(
        [{"usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}}],
        # 无 details → 保持 None（重建体不输出 details 字段）
        {"total_tokens": 60, "prompt_tokens_details": None, "completion_tokens_details": None},
        id="missing-details-none",
    ),
    pytest.param(
        [{"usage": {"prompt_tokens": 5, "completion_tokens": 1, "prompt_tokens_details": "bad", "total_tokens": "bad"}}],
        # details 非 dict / total 非 int 容错：details 不采纳、total 原样采纳（与迁移前
        # stream_reconstruct 一致：total 仅判 is not None，details 需 isinstance dict）
        {"total_tokens": "bad", "prompt_tokens_details": None, "completion_tokens_details": None},
        id="malformed-details-ignored",
    ),
]


@pytest.mark.parametrize(("feeds", "expected"), PASSTHROUGH_CASES)
def test_accumulator_openai_passthrough_fields(feeds, expected):
    """流重建透传字段：上游 total_tokens 优先，details 仅 dict 采纳。"""
    acc = _StreamUsageAccumulator(_OPENAI_FAMILY_AUTO)
    for chunk in feeds:
        acc.feed(chunk)
    actual = {
        "total_tokens": acc.total_tokens,
        "prompt_tokens_details": acc.prompt_tokens_details,
        "completion_tokens_details": acc.completion_tokens_details,
    }
    assert actual == expected


# ─── Anthropic 原样聚合（build_anthropic_stream_response 委托入口）───

ANTHROPIC_COLLECT_CASES = [
    pytest.param(
        [
            {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 10, "cache_read_input_tokens": 3}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
        ],
        # 原样存档：start 先入、delta 覆写合并，缓存字段不做归一（input_tokens 保持 10）
        {"input_tokens": 10, "cache_read_input_tokens": 3, "output_tokens": 7},
        id="start-then-delta-raw-merge",
    ),
    pytest.param(
        [{"type": "message_start", "message": {"id": "m"}}, {"type": "message_delta", "delta": {}}],
        # usage 缺失 / null 容错：空 dict
        {},
        id="usage-missing-empty",
    ),
    pytest.param(
        ["not-a-dict", {"type": "message_delta", "usage": ["bad"]}, {"type": "message_start", "message": {"usage": {"input_tokens": 2}}}],
        # 非 dict chunk / 非 dict usage 跳过
        {"input_tokens": 2},
        id="non-dict-skipped",
    ),
    pytest.param([], {}, id="empty-chunks"),
]


@pytest.mark.parametrize(("chunks", "expected"), ANTHROPIC_COLLECT_CASES)
def test_collect_anthropic_stream_usage_matrix(chunks, expected):
    """Anthropic 原样聚合：message_start 先入、message_delta 覆写，不做归一。"""
    assert _collect_anthropic_stream_usage(chunks) == expected


# ─── 调用方委托端到端：stream_reconstruct 经流重建方言累积器取 usage ───


def test_build_openai_stream_response_delegates_nested_usage():
    """Responses 上游嵌套 response.usage 经流重建方言进入重建体（迁移前由
    stream_reconstruct 本地嵌套特判处理，现委托 stream_usage 单一住所）。"""
    from proxy.stream_reconstruct import build_openai_stream_response

    chunks = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {
            "type": "response.completed",
            "response": {"id": "resp_1", "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}, "status": "completed"},
        },
    ]

    result = build_openai_stream_response(chunks, "gpt-4o")

    assert result is not None
    assert result["usage"]["prompt_tokens"] == 12
    assert result["usage"]["completion_tokens"] == 4
    assert result["usage"]["total_tokens"] == 16


# ─── 一次性归一矩阵：non-SSE 整块 / 非流式整响应 → 期望 token 值（单次调用）───

NORMALIZE_CASES = [
    pytest.param(
        {
            "id": "chatcmpl-nonsse",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            "choices": [{"finish_reason": "stop"}],
        },
        {},
        {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "stop"},
        id="chat-whole-block",
    ),
    pytest.param(
        {
            "type": "message",
            "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        },
        {},
        # prompt_tokens 缺失且无 input_tokens_details → 纯 Anthropic 语义：input 补加缓存两项
        {"input_tokens": 6, "output_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1, "finish_reason": "end_turn"},
        id="anthropic-whole-block-cache-merge",
    ),
    pytest.param(
        {
            "object": "response",
            "usage": {"input_tokens": 4, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 2}},
        },
        {},
        # input_tokens_details 存在 → OpenAI 语义（prompt 含缓存），不补加
        {"input_tokens": 4, "output_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="openai-whole-block-input_tokens_details-not-merged",
    ),
    pytest.param(
        {
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}},
            "choices": [{"finish_reason": "stop"}],
        },
        {},
        {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 0, "finish_reason": "stop"},
        id="chat-whole-block-prompt_tokens_details-cached",
    ),
    pytest.param(
        {"usage": {"prompt_tokens": 0, "completion_tokens": 0}},
        {},
        # prompt_tokens 存在（即使为 0）→ 不触发 Anthropic 缓存补加
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": None},
        id="prompt_tokens-present-zero-no-anthropic-merge",
    ),
    pytest.param(
        {"usage": None},
        {"cur_input_tokens": 99, "cur_output_tokens": 88, "cur_finish_reason": "x"},
        # usage null 容错：回退调用方现值
        {"input_tokens": 99, "output_tokens": 88, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "x"},
        id="usage-null-falls-back-to-current",
    ),
    pytest.param(
        {},
        {"cur_input_tokens": 7, "cur_output_tokens": 8, "cur_finish_reason": "y"},
        {"input_tokens": 7, "output_tokens": 8, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "y"},
        id="usage-missing-falls-back-to-current",
    ),
    pytest.param(
        {"choices": [{"finish_reason": "stop"}], "stop_reason": "end_turn"},
        {},
        # finish_reason 优先序：choices[0].finish_reason → 顶层 stop_reason 覆写
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "finish_reason": "end_turn"},
        id="stop-reason-overrides-choices-finish_reason",
    ),
]


@pytest.mark.parametrize(("full_response", "cur", "expected"), NORMALIZE_CASES)
def test_normalize_full_response_usage_matrix(full_response, cur, expected):
    """协议 × 形态 → 期望 token 值：一次性归一单次调用入口（non-SSE 整块 / 非流式整响应）。"""
    summary = _normalize_full_response_usage(full_response, **cur)
    actual = {
        "input_tokens": summary.input_tokens,
        "output_tokens": summary.output_tokens,
        "cache_read_input_tokens": summary.cache_read_input_tokens,
        "cache_creation_input_tokens": summary.cache_creation_input_tokens,
        "finish_reason": summary.finish_reason,
    }
    assert actual == expected


def test_seam_is_private_importable():
    """私有接缝可直接 import（ADR-0015 D0 约定：不进 __all__，模块无公共导出面）。"""
    import converters.stream_usage as su

    assert callable(su._normalize_full_response_usage)
    assert su._StreamUsageAccumulator is not None
    assert not hasattr(su, "__all__")


# ─── converter 侧流式 usage 规则（ADR-0016 D3）：纯函数，(chunk usage 形态 + 当前累计) → 新累计 ───
#
# 覆盖形态：增量 vs 终值、usage-only chunk（无 choices）在 finish 之后的累计、
# null / 缺失容错、字段名优先序。finish 门控（message_stop_sent /
# waiting_for_usage_after_finish 等）属状态机流控，留在转换器；此处钉死的是规则本身。

CHAT_TO_ANTHROPIC_START_CASES = [
    pytest.param(
        {"prompt_tokens": 7},
        {"input_tokens": 7, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        id="prompt_tokens-extracted",
    ),
    pytest.param(
        {"prompt_tokens": 7, "completion_tokens": 3, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
        # 流开拍只带 prompt_tokens：output 与缓存不从 chunk usage 带入
        {"input_tokens": 7, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        id="output-and-cache-not-carried-at-start",
    ),
    pytest.param(
        {}, {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, id="empty-usage-all-zero"
    ),
    pytest.param(
        None, {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, id="usage-null-tolerant"
    ),
    pytest.param(
        "garbage",
        {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        id="usage-non-dict-tolerant",
    ),
]


@pytest.mark.parametrize(("usage", "expected"), CHAT_TO_ANTHROPIC_START_CASES)
def test_chat_usage_to_anthropic_start_matrix(usage, expected):
    """Chat chunk usage → Anthropic message_start.usage（流开拍播种规则）。"""
    assert _chat_usage_to_anthropic_start(usage) == expected


CHAT_USAGE_OUTPUT_DELTA_CASES = [
    pytest.param(None, 0, (0, 0), id="usage-null-zero-delta-prev-kept"),
    pytest.param(None, 12, (0, 12), id="usage-null-prev-unchanged"),
    pytest.param({"completion_tokens": 10}, 0, (10, 10), id="first-cumulative-full-delta"),
    pytest.param({"completion_tokens": 15}, 10, (5, 15), id="cumulative-minus-prev-increment"),
    pytest.param({"completion_tokens": 20}, 15, (5, 20), id="usage-only-chunk-after-finish-increment"),
    pytest.param({"prompt_tokens": 5}, 0, (0, 0), id="missing-completion_tokens-zero"),
    # 终值回退（上游 usage 缺 completion_tokens 时 cumulative 记 0）：
    # 与迁移前逐字一致——delta 为负、prev 归零，行为零变化
    pytest.param({"prompt_tokens": 5}, 10, (-10, 0), id="missing-completion_tokens-resets-prev"),
]


@pytest.mark.parametrize(("usage", "prev", "expected"), CHAT_USAGE_OUTPUT_DELTA_CASES)
def test_chat_usage_output_delta_matrix(usage, prev, expected):
    """Chat 累计 completion_tokens → Anthropic message_delta 增量 output_tokens（增量规则）。"""
    assert _chat_usage_output_delta(usage, prev) == expected


RESPONSES_USAGE_OUTPUT_FINAL_CASES = [
    pytest.param({"output_tokens": 7}, 7, id="final-value-extracted"),
    pytest.param({"output_tokens": 0}, 0, id="explicit-zero-kept"),
    pytest.param({"input_tokens": 5}, 0, id="missing-output_tokens-zero"),
    pytest.param({}, 0, id="empty-usage-zero"),
    pytest.param(None, 0, id="usage-null-tolerant"),
    pytest.param("garbage", 0, id="usage-non-dict-tolerant"),
]


@pytest.mark.parametrize(("usage", "expected"), RESPONSES_USAGE_OUTPUT_FINAL_CASES)
def test_responses_usage_output_final_matrix(usage, expected):
    """Responses response.usage.output_tokens 终值提取（终值规则，无累计）。"""
    assert _responses_usage_output_final(usage) == expected


ANTHROPIC_RAW_MERGE_CASES = [
    pytest.param({}, {"input_tokens": 10, "output_tokens": 1}, {"input_tokens": 10, "output_tokens": 1}, id="message_start-seeds-empty"),
    pytest.param(
        {"input_tokens": 10, "output_tokens": 1},
        {"output_tokens": 9},
        # message_delta 覆写：后到字段覆盖先到，未提及字段保留
        {"input_tokens": 10, "output_tokens": 9},
        id="message_delta-overwrites-keeps-rest",
    ),
    pytest.param(
        {"input_tokens": 10},
        {"input_tokens": 10, "output_tokens": 9, "server_tool_use": {"web_search_requests": 1}},
        # 非数值字段原样保留（重建/透传口径，不做白名单过滤）
        {"input_tokens": 10, "output_tokens": 9, "server_tool_use": {"web_search_requests": 1}},
        id="extra-fields-preserved",
    ),
    pytest.param({"input_tokens": 10}, None, {"input_tokens": 10}, id="usage-null-current-kept"),
    pytest.param({"input_tokens": 10}, "garbage", {"input_tokens": 10}, id="usage-non-dict-current-kept"),
]


@pytest.mark.parametrize(("current", "incoming", "expected"), ANTHROPIC_RAW_MERGE_CASES)
def test_anthropic_usage_raw_merge_matrix(current, incoming, expected):
    """Anthropic 原始 usage 覆写合并（to_chat：start 播种 + delta 覆写，字段原样）。"""
    current_copy = dict(current)
    assert _anthropic_usage_raw_merge(current, incoming) == expected
    # 纯函数：入参 current 不被原地修改
    assert current == current_copy


ANTHROPIC_START_ACCUMULATE_CASES = [
    pytest.param(
        {},
        {"input_tokens": 10, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2, "output_tokens": 1},
        {"input_tokens": 10, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2, "output_tokens": 1},
        id="message_start-seeds-whitelist",
    ),
    pytest.param(
        {"input_tokens": 10, "output_tokens": 1},
        {"input_tokens": 5},
        # message_start 加法播种：白名单键增量累计（非覆写）
        {"input_tokens": 15, "output_tokens": 1},
        id="additive-accumulate",
    ),
    pytest.param(
        {"input_tokens": 10},
        {"server_tool_use": {"web_search_requests": 2}, "foo": 1},
        # 白名单外键丢弃（Responses 侧只消费四键）
        {"input_tokens": 10},
        id="non-whitelisted-keys-dropped",
    ),
    pytest.param({"input_tokens": 10}, None, {"input_tokens": 10}, id="usage-null-current-kept"),
    pytest.param({"input_tokens": 10}, "garbage", {"input_tokens": 10}, id="usage-non-dict-current-kept"),
]


@pytest.mark.parametrize(("current", "msg_usage", "expected"), ANTHROPIC_START_ACCUMULATE_CASES)
def test_anthropic_usage_start_accumulate_matrix(current, msg_usage, expected):
    """Anthropic message_start usage 加法播种（to_response：四键白名单增量累计）。"""
    current_copy = dict(current)
    assert _anthropic_usage_start_accumulate(current, msg_usage) == expected
    assert current == current_copy


ANTHROPIC_DELTA_OVERWRITE_CASES = [
    pytest.param(
        {"input_tokens": 15, "output_tokens": 1},
        {"input_tokens": 12, "output_tokens": 42},
        # message_delta 终值覆写：不与 start 播种值相加（增量 vs 终值的分界）
        {"input_tokens": 12, "output_tokens": 42},
        id="delta-final-value-not-summed",
    ),
    pytest.param(
        {"input_tokens": 15, "output_tokens": 1}, {"output_tokens": 42}, {"input_tokens": 15, "output_tokens": 42}, id="absent-keys-keep-current"
    ),
    pytest.param({"input_tokens": 15}, {"server_tool_use": {"x": 1}}, {"input_tokens": 15}, id="non-whitelisted-keys-dropped"),
    pytest.param({"input_tokens": 15}, None, {"input_tokens": 15}, id="usage-null-current-kept"),
]


@pytest.mark.parametrize(("current", "delta_usage", "expected"), ANTHROPIC_DELTA_OVERWRITE_CASES)
def test_anthropic_usage_delta_overwrite_matrix(current, delta_usage, expected):
    """Anthropic message_delta usage 终值覆写（to_response：四键白名单）。"""
    current_copy = dict(current)
    assert _anthropic_usage_delta_overwrite(current, delta_usage) == expected
    assert current == current_copy


def test_anthropic_start_additive_then_delta_overwrite():
    """组合形态：message_start 加法播种后 message_delta 终值覆写（两规则口径不同）。"""
    acc = _anthropic_usage_start_accumulate({}, {"input_tokens": 10, "output_tokens": 1})
    acc = _anthropic_usage_start_accumulate(acc, {"input_tokens": 10, "output_tokens": 1})
    assert acc == {"input_tokens": 20, "output_tokens": 2}
    acc = _anthropic_usage_delta_overwrite(acc, {"input_tokens": 12, "output_tokens": 42})
    assert acc == {"input_tokens": 12, "output_tokens": 42}


CHAT_TO_RESPONSE_ACC_CASES = [
    pytest.param(
        {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        {},
        {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        id="full-usage-mapped",
    ),
    pytest.param(
        {"completion_tokens": 6},
        {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        # 字段缺失回退现值（usage-only chunk / 后到 chunk 部分字段时的终值覆写）
        {"input_tokens": 10, "output_tokens": 6, "total_tokens": 12},
        id="missing-fields-fall-back-to-current",
    ),
    pytest.param(
        {"prompt_tokens": 1},
        {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        {"input_tokens": 1, "output_tokens": 2, "total_tokens": 12},
        id="usage-only-chunk-after-finish-overrides",
    ),
    pytest.param(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 900}},
        {},
        {"input_tokens": 1000, "output_tokens": 0, "total_tokens": 0, "input_tokens_details": {"cached_tokens": 900}},
        id="prompt-details-rebuilt",
    ),
    pytest.param(
        {"completion_tokens": 5, "completion_tokens_details": {}},
        {},
        # details 内层缺失回退 0
        {"input_tokens": 0, "output_tokens": 5, "total_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0}},
        id="completion-details-rebuilt-inner-default",
    ),
    pytest.param(
        {"prompt_tokens": 1, "prompt_tokens_details": "garbage"},
        {"input_tokens": 3, "output_tokens": 2, "total_tokens": 3},
        # details 非 dict 不重建（原样跳过，现值保留）
        {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        id="non-dict-details-skipped",
    ),
    pytest.param(
        {"prompt_tokens": 1},
        {"input_tokens": 3, "input_tokens_details": {"cached_tokens": 1}},
        {"input_tokens": 1, "output_tokens": 0, "total_tokens": 0},
        id="details-only-set-when-present",
    ),
    pytest.param(None, {"input_tokens": 3}, {}, id="usage-null-no-update"),
    pytest.param({}, {"input_tokens": 3}, {}, id="usage-empty-no-update"),
    pytest.param("garbage", {"input_tokens": 3}, {}, id="usage-non-dict-no-update"),
]


@pytest.mark.parametrize(("usage", "acc", "expected"), CHAT_TO_RESPONSE_ACC_CASES)
def test_chat_usage_to_response_acc_matrix(usage, acc, expected):
    """Chat usage → Responses 流式累计（终值覆写 + 现值回退 + details 透传重建）。"""
    acc_copy = dict(acc)
    assert _chat_usage_to_response_acc(usage, acc) == expected
    assert acc == acc_copy
