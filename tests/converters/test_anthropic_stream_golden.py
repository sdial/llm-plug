"""to_anthropic golden 事件序列基线 — ADR-0026 D2

先行录制：在 AnthropicStreamState 搬家前钉死 to_anthropic 两个源状态机的
对外行为，06 票重构后逐字节不变。

覆盖（ADR-0026 D1 行号清单，8 处仪式 + 5 不变量）：

- 仪式位点（ensure open → start → delta）：
  1) chat reasoning_content  → thinking 块  (to_anthropic.py:592-623)
  2) chat content            → text 块      (625-649)
  3) chat tool_calls         → tool_use 块  (651-695)
  4) responses function_call added → tool_use 块 (817-833)
  5) responses reasoning added     → thinking 块 (834-854)
  6) responses reasoning delta     → thinking delta fallback (860-887)
  7) responses output_text delta   → text delta (889-906)
  8) responses function_call_arguments delta → input_json delta (908-919)
  注：行号为 ADR 冻结时 994 行版本，当前实现同构位移仍一一对应。

- 5 不变量：
  a) header 幂等：message_start 仅一次 (ensure_started)
  b) 块开闭配对：content_block_start/stop 配对，index 递增，thinking 关块先补 signature_delta
  c) usage stash：finish_reason 到达而 usage 未到时扣留 message_stop，usage chunk/ finalize 到位再补发
  d) EOF 补偿：上游截断（无 finish_reason、仅 [DONE]）时 finalize_stream 补关块 + 补 stop
  e) finalize 幂等：重复 finalize 无二度产出；responses 源 finalize 为 no-op

断言形态：完整事件序列逐字节——既断言 dict 列表精确相等，也断言
``event:`` / ``data:`` wire 拼接（``stream_events.yield_anthropic_event``）逐字节相等。
纯测试增量，不碰生产代码；先行基线供 06 票“事件逐字节不变”断言。
"""

from __future__ import annotations

import json

from converters.stream_events import yield_anthropic_event
from converters.to_anthropic import ToAnthropicConverter

# ── helpers ──


def _feed_chat(converter: ToAnthropicConverter, chunks: list[dict]) -> list[dict]:
    """拍 1：逐 chunk 经 openai-chat-completions 喂入，收集全部事件（不含拍 2 finalize）。"""
    out: list[dict] = []
    for ch in chunks:
        out.extend(converter.convert_stream_chunk(ch, "openai-chat-completions"))
    return out


def _feed_responses(converter: ToAnthropicConverter, chunks: list[dict]) -> list[dict]:
    """拍 1：逐 chunk 经 openai-response 喂入，收集全部事件。"""
    out: list[dict] = []
    for ch in chunks:
        out.extend(converter.convert_stream_chunk(ch, "openai-response"))
    return out


def _wire(events: list[dict]) -> list[str]:
    """dict 事件 → Anthropic SSE wire（event: + data:）逐字节形态。"""
    return [yield_anthropic_event(e["type"], e) for e in events]


def _assert_golden(actual: list[dict], expected: list[dict]) -> None:
    """双重逐字节：dict 序列 + wire 序列同时相等。"""
    assert actual == expected, (
        f"dict golden mismatch:\nactual={json.dumps(actual, ensure_ascii=False, indent=2)}\n"
        f"expected={json.dumps(expected, ensure_ascii=False, indent=2)}"
    )
    assert _wire(actual) == _wire(expected), "wire golden mismatch (event:/data: 形态不一致)"


# ── chat 源 golden ──


class TestChatSourceGolden:
    """chat → anthropic 6 场景，覆盖仪式 1-3 与不变量 a-e。"""

    def test_reasoning_thinking_block_switch_golden(self):
        """仪式 1→2：reasoning (thinking) → text 切换；含 signature_delta 关块与 index 推进。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-1", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-1", "model": "gpt-4o", "choices": [{"delta": {"reasoning_content": "Think step"}}]},
            {"id": "chatcmpl-1", "model": "gpt-4o", "choices": [{"delta": {"content": "Answer"}}]},
            {"id": "chatcmpl-1", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize

        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Think step"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": ""}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Answer"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected)

    def test_text_block_golden(self):
        """仪式 2：纯 text 块（无 thinking 前置），含同块内多 delta 归一 index。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-2", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-2", "model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}}]},
            {"id": "chatcmpl-2", "model": "gpt-4o", "choices": [{"delta": {"content": " world"}}]},
            {"id": "chatcmpl-2", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize

        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_2",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected)

    def test_text_with_usage_in_finish_chunk_golden(self):
        """仪式 2 携带同 chunk usage：finish 与 usage 同帧立即收尾（不经 stash）。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-2u", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-2u", "model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}}]},
            {
                "id": "chatcmpl-2u",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
            },
        ]
        events = _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize

        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_2u",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected)
        assert finalize == [], "同帧 usage 已收尾，finalize 应为空（幂等不变量 e）"

    def test_tool_call_single_golden(self):
        """仪式 3：单 tool_use 块，ensure open → start → delta 单次。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-3s", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {
                "id": "chatcmpl-3s",
                "model": "gpt-4o",
                "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q":"x"}'}}]}}],
            },
            {"id": "chatcmpl-3s", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize

        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_3s",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "search", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected)

    def test_tool_call_index_advancement_golden(self):
        """仪式 3：多 tool_call index 推进；每新 index 关旧块、index 自增、配对校验。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-3", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search", "arguments": "{"}}]}}],
            },
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"q":"x"'}}]}}],
            },
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "calc", "arguments": "{}"}}]}}],
            },
            {"id": "chatcmpl-3", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize

        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_3",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "search", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"q":"x"'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call_2", "name": "calc", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected)
        # 块配对不变量 b：start/stop 索引一一对应且递增
        starts = [e["index"] for e in all_events if e["type"] == "content_block_start"]
        stops = [e["index"] for e in all_events if e["type"] == "content_block_stop"]
        assert starts == [0, 1]
        assert stops == [0, 1]

    def test_finish_stash_then_usage_recovery_golden(self):
        """不变量 c：finish 无 usage 时 stash，usage-only chunk 到达后补发 message_delta+message_stop。"""
        c = ToAnthropicConverter()
        pre_chunks = [
            {"id": "chatcmpl-4", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-4", "model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}}]},
            {"id": "chatcmpl-4", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        pre_events = _feed_chat(c, pre_chunks)
        # 尚未收到 usage：应仅有关块，无 message_delta/message_stop
        assert [e["type"] for e in pre_events] == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
        ]
        # stash 状态：pending 扣留，尚未发 stop
        assert c._stream_state.pending_finish_reason == "stop"  # type: ignore[union-attr]
        assert c._stream_state.message_stop_sent is False  # type: ignore[union-attr]
        # 若此时直接 finalize（EOF 兜底），应补发 end_turn + 0 tokens
        # 需用独立实例验证，避免污染 stash→usage 补发主路径
        c_eof = ToAnthropicConverter()
        for ch in pre_chunks:
            c_eof.convert_stream_chunk(ch, "openai-chat-completions")
        eof_finalize = c_eof.finalize_stream("openai-chat-completions")
        _assert_golden(
            eof_finalize,
            [
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
                {"type": "message_stop"},
            ],
        )

        # 主路径：stash 后由 usage-only chunk 补发（含增量 output_tokens）
        c2 = ToAnthropicConverter()
        for ch in pre_chunks:
            c2.convert_stream_chunk(ch, "openai-chat-completions")
        assert c2._stream_state.pending_finish_reason == "stop"  # type: ignore[union-attr]
        assert c2._stream_state.message_stop_sent is False  # type: ignore[union-attr]

        usage_events = c2.convert_stream_chunk({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}, "openai-chat-completions")
        finalize_after = c2.finalize_stream("openai-chat-completions")

        expected_usage = [
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        _assert_golden(usage_events, expected_usage)
        assert finalize_after == [], "usage 已补发，finalize 幂等应为空"
        # wire 逐字节
        assert _wire(usage_events) == _wire(expected_usage)

    def test_eof_truncation_finalize_compensation_golden(self):
        """不变量 d：上游截断（仅 content，无 finish）时 finalize 补关块 + 补 stop（end_turn 兜底）。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-5", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-5", "model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}}]},
        ]
        pre = _feed_chat(c, chunks)
        assert [e["type"] for e in pre] == ["message_start", "content_block_start", "content_block_delta"]
        # 此时块仍开启，未关
        assert c._stream_state.content_block_started is True  # type: ignore[union-attr]

        finalize = c.finalize_stream("openai-chat-completions")
        expected_finalize = [
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(finalize, expected_finalize)
        all_events = pre + finalize
        expected_all = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_5",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected_all)

    def test_eof_truncation_thinking_finalize_includes_signature_delta_golden(self):
        """不变量 b+d：thinking 块截断时 finalize 关块先补 signature_delta。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-5t", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-5t", "model": "gpt-4o", "choices": [{"delta": {"reasoning_content": "Think"}}]},
        ]
        _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        expected_finalize = [
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": ""}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(finalize, expected_finalize)
        # 紧接着的二度 finalize 幂等
        assert c.finalize_stream("openai-chat-completions") == []

    def test_finalize_idempotency_golden(self):
        """不变量 e：finalize 幂等；responses 源 no-op 亦幂等。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-6", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-6", "model": "gpt-4o", "choices": [{"delta": {"content": "Hi"}}]},
        ]
        _feed_chat(c, chunks)
        first = c.finalize_stream("openai-chat-completions")
        second = c.finalize_stream("openai-chat-completions")
        third = c.finalize_stream("openai-chat-completions")
        assert first == [
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        assert second == []
        assert third == []
        # wire 幂等
        assert _wire(second) == []
        # responses 源 finalize 恒为 no-op
        c2 = ToAnthropicConverter()
        c2.convert_stream_chunk({"type": "response.created", "response": {"id": "resp_1", "model": "gpt-4o"}}, "openai-response")
        assert c2.finalize_stream("openai-response") == []
        assert c2.finalize_stream("openai-response") == []

    def test_header_idempotency_golden(self):
        """不变量 a：多次 role:assistant 头仅发一次 message_start。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-7", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-7", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-7", "model": "gpt-4o", "choices": [{"delta": {"content": "Hi"}}]},
        ]
        events = _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize
        # 仅一次 message_start
        assert [e["type"] for e in all_events].count("message_start") == 1
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_7",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected)

    def test_message_start_with_prompt_tokens_golden(self):
        """header 携带 prompt_tokens 时 message_start.usage.input_tokens 精确。"""
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-8", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}], "usage": {"prompt_tokens": 42}},
            {"id": "chatcmpl-8", "model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}}]},
            {"id": "chatcmpl-8", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = _feed_chat(c, chunks)
        # message_start 已在首拍发出，携带 input_tokens 42
        assert events[0]["type"] == "message_start"
        assert events[0]["message"]["usage"]["input_tokens"] == 42
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_8",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 42, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
        _assert_golden(all_events, expected)


# ── responses 源 golden ──


class TestResponsesSourceGolden:
    """responses → anthropic 5 场景，覆盖仪式 4-8 与 response.completed 原子语义。"""

    def test_reasoning_item_open_start_delta_done_golden(self):
        """仪式 5+6+done：reasoning item 的 open→start→delta→done 完整生命周期。"""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_10", "model": "o1"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.reasoning_text.delta", "delta": "thinking step..."},
            {"type": "response.output_item.done", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.output_item.added", "output_index": 1, "item": {"type": "message", "id": "msg_1"}},
            {"type": "response.output_text.delta", "delta": "the answer"},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5}}},
        ]
        events = _feed_responses(c, chunks)
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_10",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "o1",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "thinking step..."}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": ""}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "the answer"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)
        # finalize no-op
        assert c.finalize_stream("openai-response") == []

    def test_reasoning_delta_fallback_without_added_golden(self):
        """仪式 6：未收到 output_item.added 时 reasoning delta 兜底开 thinking 块。"""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_11", "model": "o1"}},
            {"type": "response.reasoning_summary_text.delta", "delta": "fallback thinking"},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 2}}},
        ]
        events = _feed_responses(c, chunks)
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_11",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "o1",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "fallback thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": ""}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)

    def test_message_item_text_delta_done_golden(self):
        """仪式 7：message item 经 output_text.delta 开 text 块 → delta → done."""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_12", "model": "gpt-4o"}},
            {"type": "response.output_item.added", "output_index": 1, "item": {"type": "message", "id": "msg_1"}},
            {"type": "response.output_text.delta", "delta": "hello"},
            {"type": "response.output_item.done", "output_index": 1, "item": {"type": "message", "id": "msg_1"}},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 3}}},
        ]
        events = _feed_responses(c, chunks)
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_12",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)

    def test_text_delta_fallback_without_added_golden(self):
        """仪式 7 fallback：无 message added 时 text delta 仍兜底开块。"""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_13", "model": "gpt-4o"}},
            {"type": "response.output_text.delta", "delta": "fallback hello"},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 1}}},
        ]
        events = _feed_responses(c, chunks)
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_13",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fallback hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)

    def test_function_call_item_open_start_delta_done_golden(self):
        """仪式 4+8+done：function_call item 的 open→start→delta→done。"""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_14", "model": "gpt-4o"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search", "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "delta": '{"q":"x"}'},
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search"},
            },
            {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 5}}},
        ]
        events = _feed_responses(c, chunks)
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_14",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "search", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)

    def test_function_call_delta_fallback_without_added_golden(self):
        """仪式 8 fallback：无 function_call added 时 delta 兜底（仅 mark started，不新建块头）。"""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_15", "model": "gpt-4o"}},
            {"type": "response.function_call_arguments.delta", "delta": '{"fallback":true}'},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 1}}},
        ]
        events = _feed_responses(c, chunks)
        # 该路径不 emit content_block_start（state 仅 mark started），直接 delta
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_15",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"fallback":true}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)

    def test_response_completed_atomic_message_delta_stop_golden(self):
        """response.completed 原子产出 message_delta+message_stop；usage 为终值。"""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_16", "model": "gpt-4o"}},
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 7}}},
        ]
        events = _feed_responses(c, chunks)
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_16",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4o",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)
        # 原子性：message_delta 紧邻 message_stop
        assert [e["type"] for e in events[-2:]] == ["message_delta", "message_stop"]
        # usage 终值精确
        assert events[-2]["usage"] == {"output_tokens": 7}

    def test_response_finalize_noop_golden(self):
        """responses 源 finalize_stream 恒为 no-op（即使已开块、未 completed 亦不补发）。"""
        c = ToAnthropicConverter()
        c.convert_stream_chunk({"type": "response.created", "response": {"id": "resp_17", "model": "gpt-4o"}}, "openai-response")
        c.convert_stream_chunk({"type": "response.output_text.delta", "delta": "hi"}, "openai-response")
        # 未发 completed，块仍开启
        assert c._stream_state.content_block_started is True  # type: ignore[union-attr]
        assert c.finalize_stream("openai-response") == []
        assert c.finalize_stream("openai-response") == []
        # 即使未 started
        c2 = ToAnthropicConverter()
        assert c2.finalize_stream("openai-response") == []
        # chat 源 finalize 仍正常，responses 源不受影响
        assert c2.finalize_stream("openai-chat-completions") == []

    def test_reasoning_to_message_switch_golden(self):
        """reasoning → message 切换：关 thinking（补 signature_delta）再开 text。"""
        c = ToAnthropicConverter()
        chunks = [
            {"type": "response.created", "response": {"id": "resp_18", "model": "o1"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.reasoning_text.delta", "delta": "think"},
            {"type": "response.output_item.done", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.output_item.added", "output_index": 1, "item": {"type": "message", "id": "msg_1"}},
            {"type": "response.output_text.delta", "delta": "answer"},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"output_tokens": 4}}},
        ]
        events = _feed_responses(c, chunks)
        expected = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_resp_18",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "o1",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "think"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": ""}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "answer"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 4}},
            {"type": "message_stop"},
        ]
        _assert_golden(events, expected)


# ── 5 不变量显式回归（与 golden 交叉覆盖，便于定位漂移） ──


class TestFiveInvariants:
    """5 不变量的极简直测（与 golden 互为冗余，漂移时快速定位）。"""

    def test_header_idempotent(self):
        c = ToAnthropicConverter()
        c.convert_stream_chunk(
            {"id": "chatcmpl-x", "model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]}, "openai-chat-completions"
        )
        c.convert_stream_chunk(
            {"id": "chatcmpl-x", "model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]}, "openai-chat-completions"
        )
        c.convert_stream_chunk({"id": "chatcmpl-x", "model": "m", "choices": [{"delta": {"content": "hi"}}]}, "openai-chat-completions")
        finalize = c.finalize_stream("openai-chat-completions")
        assert finalize is not None
        # 重新收集以验幂等
        c2 = ToAnthropicConverter()
        ev = _feed_chat(
            c2,
            [
                {"id": "chatcmpl-x", "model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
                {"id": "chatcmpl-x", "model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            ],
        )
        assert [e["type"] for e in ev].count("message_start") == 1
        # finalize 仍补 stop
        assert finalize is not None

    def test_block_pairing_and_index_monotonic(self):
        c = ToAnthropicConverter()
        chunks = [
            {"id": "chatcmpl-b", "model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"id": "chatcmpl-b", "model": "m", "choices": [{"delta": {"reasoning_content": "r1"}}]},
            {"id": "chatcmpl-b", "model": "m", "choices": [{"delta": {"content": "t1"}}]},
            {
                "id": "chatcmpl-b",
                "model": "m",
                "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}],
            },
            {
                "id": "chatcmpl-b",
                "model": "m",
                "choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "g", "arguments": "{}"}}]}}],
            },
            {"id": "chatcmpl-b", "model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        events = _feed_chat(c, chunks)
        finalize = c.finalize_stream("openai-chat-completions")
        all_events = events + finalize
        starts = [e for e in all_events if e["type"] == "content_block_start"]
        stops = [e for e in all_events if e["type"] == "content_block_stop"]
        assert len(starts) == len(stops) == 4
        assert [e["index"] for e in starts] == [0, 1, 2, 3]
        assert [e["index"] for e in stops] == [0, 1, 2, 3]
        # thinking 关块必含 signature_delta
        sig_deltas = [e for e in all_events if e.get("delta", {}).get("type") == "signature_delta"]
        assert len(sig_deltas) == 1
        assert sig_deltas[0]["index"] == 0

    def test_usage_stash_and_eof_and_finalize_idempotent(self):
        # 已在 chat golden 中逐字节覆盖，此处仅作不变量门控快速断言
        c = ToAnthropicConverter()
        c.convert_stream_chunk({"id": "c", "model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]}, "openai-chat-completions")
        c.convert_stream_chunk({"id": "c", "model": "m", "choices": [{"delta": {"content": "hi"}}]}, "openai-chat-completions")
        c.convert_stream_chunk({"id": "c", "model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]}, "openai-chat-completions")
        assert c._stream_state.pending_finish_reason == "stop"  # type: ignore[union-attr]
        assert c._stream_state.message_stop_sent is False  # type: ignore[union-attr]
        # usage 到达补发
        beat = c.convert_stream_chunk({"choices": [], "usage": {"completion_tokens": 5}}, "openai-chat-completions")
        assert [e["type"] for e in beat] == ["message_delta", "message_stop"]
        assert c.finalize_stream("openai-chat-completions") == []
        assert c.finalize_stream("openai-chat-completions") == []

        # EOF 补偿
        c2 = ToAnthropicConverter()
        c2.convert_stream_chunk({"id": "c", "model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]}, "openai-chat-completions")
        c2.convert_stream_chunk({"id": "c", "model": "m", "choices": [{"delta": {"content": "hi"}}]}, "openai-chat-completions")
        fin = c2.finalize_stream("openai-chat-completions")
        assert [e["type"] for e in fin] == ["content_block_stop", "message_delta", "message_stop"]
        assert c2.finalize_stream("openai-chat-completions") == []
