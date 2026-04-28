from converters.to_anthropic import ToAnthropicConverter
from converters.to_response import ToResponseConverter


def feed_anthropic_events(converter, events):
    """辅助函数：逐 chunk 输入并收集全部输出（Chat→Anthropic 方向）"""
    outputs = []
    for evt in events:
        result = converter.convert_stream_chunk(evt, "openai-chat-completions")
        if result is not None:
            et = converter.get_stream_event_type(evt, "openai-chat-completions")
            outputs.append((et, result))
            extra = converter.get_extra_events(result or {})
            for extra_evt in extra:
                if isinstance(extra_evt, tuple) and len(extra_evt) == 2:
                    outputs.append(extra_evt)
                elif isinstance(extra_evt, dict):
                    outputs.append((extra_evt.get("type", ""), extra_evt))
    return outputs


class TestChatToAnthropicStream:
    def test_thinking_stream_with_signature_delta(self):
        """OpenAI reasoning_content 流应生成 thinking_delta + signature_delta"""
        converter = ToAnthropicConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "Let me think..."}}]},
            {"choices": [{"delta": {"content": "The answer is 42"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = feed_anthropic_events(converter, events)
        delta_types = [d.get("delta", {}).get("type") for _, d in outputs]
        assert "thinking_delta" in delta_types
        assert "signature_delta" in delta_types

    def test_message_start_with_usage(self):
        """message_start 应包含 input_tokens"""
        converter = ToAnthropicConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}], "usage": {"prompt_tokens": 42}},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = feed_anthropic_events(converter, events)
        msg_start = [d for et, d in outputs if et == "message_start"][0]
        assert msg_start["message"]["usage"]["input_tokens"] == 42


class TestChatToResponseStream:
    def test_multiple_tool_calls_output_index(self):
        """多个 tool_call 应有递增的 output_index"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "calc"}}]}}]},
            {"choices": [{"finish_reason": "tool_calls"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        added_events = [o for o in outputs if isinstance(o, dict) and o.get("type") == "response.output_item.added"]
        assert len(added_events) == 2
        assert added_events[0]["output_index"] == 0
        assert added_events[1]["output_index"] == 1

    def test_reasoning_output_index_not_zero_when_preceded_by_tool(self):
        """reasoning 在 tool_call 之后应有递增的 output_index"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search"}}]}}]},
            {"choices": [{"delta": {"reasoning_content": "Thinking..."}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        added_events = [o for o in outputs if isinstance(o, dict) and o.get("type") == "response.output_item.added"]
        reasoning_events = [o for o in added_events if o.get("item", {}).get("type") == "reasoning"]
        assert len(reasoning_events) == 1
        assert reasoning_events[0]["output_index"] == 1
