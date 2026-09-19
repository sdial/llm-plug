"""转换器流式两拍协议（ADR-0016 D0）的表驱动公共缝测试。

协议（写在 converters/base.py 的接口 docstring 里，这里只走公共协议）：
- 拍 1 —— 每个上游 chunk 调一次 ``convert_stream_chunk(chunk, source_type)``，
  返回该拍产生的**全部**事件（``list[dict]``，空列表 = 本拍无产出）；
- 拍 2 —— 上游流结束时调一次 ``finalize_stream(source_type)``，返回收尾事件。

本文件不手写信封 dict（源 chunk 除外）、不探入 ``_stream_state`` 等实例状态。
事件统一 dict 形态：Anthropic / Responses 目标事件的协议类型内嵌于 ``type`` 字段。
"""

import pytest

from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter

CHAT_TEXT_CHUNKS = [
    {"id": "c1", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
    {"id": "c1", "model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}}]},
    {"id": "c1", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}]},
]


def beats(converter, chunks, source_type):
    """拍 1：逐 chunk 喂入，收集每拍的事件列表（空列表拍原样保留）。"""
    return [converter.convert_stream_chunk(chunk, source_type) for chunk in chunks]


def event_types(beat_events):
    return [e.get("type") for e in beat_events if isinstance(e, dict)]


def flatten(beats_list):
    return [evt for beat in beats_list for evt in beat]


# ─── ToAnthropicConverter：一拍多事件 / 空拍 / finalize 补发 ───


class TestToAnthropicTwoBeatProtocol:
    def test_role_beat_is_single_message_start_event(self):
        converter = ToAnthropicConverter()
        beat = converter.convert_stream_chunk(CHAT_TEXT_CHUNKS[0], "openai-chat-completions")
        assert isinstance(beat, list)
        assert event_types(beat) == ["message_start"]

    def test_content_beat_produces_block_start_and_delta_in_one_beat(self):
        converter = ToAnthropicConverter()
        converter.convert_stream_chunk(CHAT_TEXT_CHUNKS[0], "openai-chat-completions")
        beat = converter.convert_stream_chunk(CHAT_TEXT_CHUNKS[1], "openai-chat-completions")
        assert event_types(beat) == ["content_block_start", "content_block_delta"]
        # 事件统一 dict 形态，协议类型内嵌于 type 字段
        assert all(evt["type"] == evt.get("type") for evt in beat)

    def test_finish_without_usage_is_pending_finalize_supplements(self):
        """finish chunk 不带 usage 时不收尾；拍 2 finalize 补出 message_delta + message_stop。"""
        converter = ToAnthropicConverter()
        for chunk in CHAT_TEXT_CHUNKS:
            beat = converter.convert_stream_chunk(chunk, "openai-chat-completions")
        assert all(t != "message_stop" for t in event_types(beat))

        final = converter.finalize_stream("openai-chat-completions")
        assert event_types(final) == ["message_delta", "message_stop"]

    def test_usage_only_chunk_after_finish_is_multi_event_beat_and_finalize_empty(self):
        """usage-only chunk 一拍产出 message_delta + message_stop；finalize 已收尾返回空列表。"""
        converter = ToAnthropicConverter()
        for chunk in CHAT_TEXT_CHUNKS:
            converter.convert_stream_chunk(chunk, "openai-chat-completions")
        beat = converter.convert_stream_chunk(
            {"id": "c1", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
            "openai-chat-completions",
        )
        assert event_types(beat) == ["message_delta", "message_stop"]
        assert converter.finalize_stream("openai-chat-completions") == []

    def test_cross_block_switch_beat_closes_previous_block_first(self):
        """thinking → text 跨块切换拍：先补 signature_delta 关旧块，再开新块。"""
        converter = ToAnthropicConverter()
        converter.convert_stream_chunk(CHAT_TEXT_CHUNKS[0], "openai-chat-completions")
        converter.convert_stream_chunk(
            {"id": "c1", "choices": [{"delta": {"reasoning_content": "think"}}]},
            "openai-chat-completions",
        )
        beat = converter.convert_stream_chunk(
            {"id": "c1", "choices": [{"delta": {"content": "answer"}}]},
            "openai-chat-completions",
        )
        assert event_types(beat) == [
            "content_block_delta",  # signature_delta（关 thinking 块前补空签名）
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
        ]

    def test_response_thinking_block_close_emits_signature_delta_first(self):
        """response→anthropic 关 thinking 块同样先补 signature_delta（ADR-0016 D1 授权收敛点）。

        收敛前该方向关块不发 signature_delta；两套状态机统一走 stream_sse
        块切换原子操作后，thinking 块关块前一律先补空签名。
        """
        converter = ToAnthropicConverter()
        converter.convert_stream_chunk(
            {"type": "response.created", "response": {"id": "r1", "model": "gpt-4o"}},
            "openai-response",
        )
        converter.convert_stream_chunk(
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
            "openai-response",
        )
        converter.convert_stream_chunk(
            {"type": "response.reasoning_text.delta", "output_index": 0, "delta": "think"},
            "openai-response",
        )
        beat = converter.convert_stream_chunk(
            {"type": "response.output_item.added", "output_index": 1, "item": {"type": "message", "id": "msg_1"}},
            "openai-response",
        )
        assert event_types(beat) == ["content_block_delta", "content_block_stop"]
        assert beat[0]["delta"] == {"type": "signature_delta", "signature": ""}

    def test_finalize_without_any_chunk_returns_empty(self):
        assert ToAnthropicConverter().finalize_stream("openai-chat-completions") == []


# ─── ToChatCompletionsConverter：空拍语义 / usage chunk 并入同拍 ───


class TestToChatTwoBeatProtocol:
    def test_silent_events_yield_empty_list_beat(self):
        converter = ToChatCompletionsConverter()
        assert converter.convert_stream_chunk({"type": "ping"}, "anthropic") == []
        assert converter.convert_stream_chunk({"type": "content_block_stop", "index": 0}, "anthropic") == []

    def test_message_start_beat_is_single_chunk(self):
        converter = ToChatCompletionsConverter()
        beat = converter.convert_stream_chunk(
            {"type": "message_start", "message": {"id": "msg_1", "model": "claude"}},
            "anthropic",
        )
        assert len(beat) == 1
        assert beat[0]["object"] == "chat.completion.chunk"

    def test_usage_chunk_joins_finish_beat_when_include_usage(self):
        """include_usage 时 response.completed 拍 = finish chunk + usage chunk 两个事件。"""
        converter = ToChatCompletionsConverter()
        converter.set_stream_include_usage(True)
        converter.convert_stream_chunk(
            {"type": "response.created", "response": {"id": "resp_u", "model": "gpt-4o"}},
            "openai-response",
        )
        beat = converter.convert_stream_chunk(
            {"type": "response.completed", "response": {"id": "resp_u", "status": "completed", "output": []}},
            "openai-response",
        )
        assert len(beat) == 2
        assert beat[0]["choices"][0]["finish_reason"] == "stop"
        assert beat[1]["choices"] == []
        assert beat[1]["usage"]["prompt_tokens"] == 0

    def test_finalize_default_returns_empty(self):
        assert ToChatCompletionsConverter().finalize_stream("anthropic") == []

    def test_usage_only_message_delta_does_not_emit_a_second_finish(self):
        converter = ToChatCompletionsConverter()
        converter.convert_stream_chunk({"type": "message_start", "message": {"id": "msg_1", "model": "claude"}}, "anthropic")
        first = converter.convert_stream_chunk({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, "anthropic")
        second = converter.convert_stream_chunk({"type": "message_delta", "usage": {"output_tokens": 3}}, "anthropic")

        assert first[0]["choices"][0]["finish_reason"] == "tool_calls"
        assert second == []


# ─── ToResponseConverter：created+in_progress 同拍 / finalize 补发 ───


class TestToResponseTwoBeatProtocol:
    def test_created_beat_carries_in_progress_in_same_beat(self):
        converter = ToResponseConverter()
        beat = converter.convert_stream_chunk(
            {"id": "c1", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            "openai-chat-completions",
        )
        assert event_types(beat) == ["response.created", "response.in_progress"]

    def test_empty_delta_beat_yields_empty_list(self):
        converter = ToResponseConverter()
        converter.convert_stream_chunk(
            {"id": "c1", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
            "openai-chat-completions",
        )
        assert (
            converter.convert_stream_chunk(
                {"id": "c1", "choices": [], "usage": None},
                "openai-chat-completions",
            )
            == []
        )

    def test_finish_beat_emits_done_sequence_finalize_supplements_completed(self):
        """finish 拍产出 *.done 序列（不含 completed）；拍 2 finalize 补发 response.completed。"""
        converter = ToResponseConverter()
        for chunk in CHAT_TEXT_CHUNKS:
            beat = converter.convert_stream_chunk(chunk, "openai-chat-completions")
        assert event_types(beat) == [
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
        ]

        final = converter.finalize_stream("openai-chat-completions")
        assert event_types(final) == ["response.completed"]

    def test_anthropic_message_start_beat_carries_in_progress(self):
        converter = ToResponseConverter()
        beat = converter.convert_stream_chunk(
            {"type": "message_start", "message": {"id": "msg_a", "model": "claude"}},
            "anthropic",
        )
        assert event_types(beat) == ["response.created", "response.in_progress"]

    def test_each_response_event_has_monotonic_sequence_number(self):
        converter = ToResponseConverter()
        events = flatten(beats(converter, CHAT_TEXT_CHUNKS, "openai-chat-completions"))
        events.extend(converter.finalize_stream("openai-chat-completions"))

        assert [event["sequence_number"] for event in events] == list(range(1, len(events) + 1))


# ─── 协议形态不变式 + 分发表 ───


class TestProtocolInvariants:
    def test_anthropic_target_events_embed_type(self):
        converter = ToAnthropicConverter()
        events = flatten(beats(converter, CHAT_TEXT_CHUNKS, "openai-chat-completions"))
        events.extend(converter.finalize_stream("openai-chat-completions"))
        assert events and all(evt.get("type") for evt in events)

    def test_response_target_events_embed_type(self):
        converter = ToResponseConverter()
        events = flatten(beats(converter, CHAT_TEXT_CHUNKS, "openai-chat-completions"))
        events.extend(converter.finalize_stream("openai-chat-completions"))
        assert events and all(evt.get("type") for evt in events)

    @pytest.mark.parametrize(
        "converter",
        [ToAnthropicConverter(), ToChatCompletionsConverter(), ToResponseConverter()],
        ids=["to_anthropic", "to_chat", "to_response"],
    )
    def test_unknown_source_type_raises_unified_value_error(self, converter):
        for call in (
            lambda: converter.convert_request({}, "no-such-source"),
            lambda: converter.convert_response({}, "no-such-source"),
            lambda: converter.convert_stream_chunk({}, "no-such-source"),
        ):
            with pytest.raises(ValueError, match=r"不支持 source_type='no-such-source'"):
                call()
