"""ADR-0016 D1 事件工厂直测：Chat chunk 信封 / Responses done 序列。

纯函数 wire 断言（含 key 顺序，dict == 不比顺序故另断 list(keys)），
spec「Implementation Decisions D1」为唯一事实源。不手写信封 dict 于被测工厂之外、
不探任何转换器实例状态。块切换原子操作（原工厂 2）已归位 to_anthropic 私有
``_close_content_block``（ADR-0023 D1），其直测随迁 tests/converters/。
"""

import pytest

from converters.stream_events import (
    _build_chat_completion_chunk,
    _build_response_item_done_events,
)

# ─── 工厂 1：chat.completion.chunk 信封 ───


class TestBuildChatCompletionChunk:
    def test_minimal_envelope_five_fields(self):
        chunk = _build_chat_completion_chunk("c1", "gpt-4o")
        assert chunk == {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        }
        # wire 上 key 顺序钉死：id → object → created → model → choices
        assert list(chunk.keys()) == ["id", "object", "created", "model", "choices"]

    def test_delta_and_finish_reason(self):
        chunk = _build_chat_completion_chunk("c1", "gpt-4o", delta={"content": "hi"}, finish_reason="stop", index=2)
        assert chunk["choices"] == [{"index": 2, "delta": {"content": "hi"}, "finish_reason": "stop"}]

    def test_usage_chunk_choices_empty_usage_last(self):
        chunk = _build_chat_completion_chunk("c1", "gpt-4o", choices=[], usage={"prompt_tokens": 3})
        assert chunk["choices"] == []
        assert chunk["usage"] == {"prompt_tokens": 3}
        # usage 在 choices 之后（与既有 wire 顺序一致）
        assert list(chunk.keys()) == ["id", "object", "created", "model", "choices", "usage"]

    def test_created_override(self):
        chunk = _build_chat_completion_chunk("c1", "gpt-4o", created=1700000000)
        assert chunk["created"] == 1700000000

    def test_explicit_choices_beats_delta(self):
        choice = {"index": 0, "delta": {}, "finish_reason": "stop", "x_stop_sequence": "END"}
        chunk = _build_chat_completion_chunk("c1", "gpt-4o", choices=[choice])
        assert chunk["choices"] == [choice]


# ─── 工厂 3：Responses *.done 收尾序列 ───


class TestBuildResponseItemDoneEvents:
    def test_message_with_text_full_sequence(self):
        events = _build_response_item_done_events("message", item_id="msg_1", output_index=0, text="Hello")
        assert events == [
            {
                "type": "response.output_text.done",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "text": "Hello",
            },
            {
                "type": "response.content_part.done",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "Hello"},
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello"}],
                },
            },
        ]

    def test_message_empty_text_only_item_done(self):
        events = _build_response_item_done_events("message", item_id="msg_1", output_index=1, text="")
        assert len(events) == 1
        assert events[0]["type"] == "response.output_item.done"
        assert events[0]["item"]["content"] == [{"type": "output_text", "text": ""}]

    def test_function_call_sequence(self):
        events = _build_response_item_done_events(
            "function_call",
            item_id="fc_1",
            output_index=2,
            call_id="call_1",
            name="get_weather",
            arguments='{"city":"NYC"}',
        )
        assert events == [
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_1",
                "output_index": 2,
                "name": "get_weather",
                "arguments": '{"city":"NYC"}',
            },
            {
                "type": "response.output_item.done",
                "output_index": 2,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": '{"city":"NYC"}',
                    "status": "completed",
                },
            },
        ]

    def test_reasoning_sequence(self):
        events = _build_response_item_done_events("reasoning", item_id="rs_1", output_index=0, content_index=0, text="Think")
        assert [e["type"] for e in events] == [
            "response.reasoning_text.done",
            "response.content_part.done",
            "response.output_item.done",
        ]
        assert events[0]["text"] == "Think"
        assert events[1]["part"] == {"type": "reasoning_text", "text": "Think"}
        assert events[2]["item"] == {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "Think"}],
        }

    def test_item_override_replaces_synthesized_item(self):
        """调用方已构建的 completed item（含归一化 id）优先于工厂合成形态。"""
        override = {}
        events = _build_response_item_done_events("message", item_id="msg_1", output_index=0, text="x", item=override)
        assert events[-1]["item"] is override

    def test_unknown_item_type_raises(self):
        with pytest.raises(ValueError, match="unsupported item_type"):
            _build_response_item_done_events("web_search_call", item_id="x", output_index=0)
