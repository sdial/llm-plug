import json

import pytest

from converters.to_chat import ToChatCompletionsConverter
from models.api_types import APIType


class TestAnthropicToChat:
    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_thinking_enabled_to_reasoning_effort(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 16000},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "reasoning_effort" in result
        assert result["reasoning_effort"] == "high"
        assert result["enable_thinking"] is True

    def test_thinking_adaptive_to_reasoning_effort_medium(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "thinking": {"type": "adaptive"},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "reasoning_effort" in result
        assert result["reasoning_effort"] == "medium"

    def test_no_thinking_no_reasoning_effort(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "reasoning_effort" not in result

    def test_tool_choice_any_to_required(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": {"type": "any"},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["tool_choice"] == "required"

    def test_tool_choice_none(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": {"type": "none"},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["tool_choice"] == "none"

    def test_thinking_response_to_reasoning_content(self):
        """Anthropic thinking block -> OpenAI reasoning_content"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me analyze..."},
                {"type": "text", "text": "The answer is 42"},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        assert result["choices"][0]["message"]["reasoning_content"] == "Let me analyze..."
        assert result["choices"][0]["message"]["content"] == "The answer is 42"

    def test_tool_use_response(self):
        """Anthropic tool_use -> OpenAI tool_calls"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_001",
                    "name": "search",
                    "input": {"q": "test"},
                },
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "search"
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"q": "test"}

    def test_metadata_user_id_to_user(self):
        """Anthropic metadata.user_id -> OpenAI user"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "metadata": {"user_id": "user_12345"},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["user"] == "user_12345"

    def test_metadata_dict_dropped_except_user_id(self):
        """M3: metadata 除 user_id 外不透传（Claude Code 含布尔/嵌套值，官方 OpenAI 拒绝非字符串值）"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "metadata": {
                "user_id": "user_12345",
                "session_id": "sess_1",
                "is_cmd_loop": False,
                "nested": {"key": "val"},
            },
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["user"] == "user_12345"
        assert "metadata" not in result

    def test_metadata_without_user_id_not_forwarded(self):
        """M3: metadata 无 user_id 时整个丢弃，不残留任何字段"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "metadata": {"session_id": "sess_1", "is_cmd_loop": True},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "metadata" not in result
        assert "user" not in result

    def test_tools_strict_passthrough(self):
        """Anthropic tools strict 字段应透传到 OpenAI"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [
                {
                    "name": "test",
                    "description": "A test function",
                    "input_schema": {"type": "object"},
                    "strict": True,
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["tools"][0]["function"]["strict"] is True

    def test_nonportable_document_content_is_rejected(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "content",
                                "content": "PDF content here",
                            },
                        },
                    ],
                }
            ],
        }
        with pytest.raises(ValueError, match="cannot be represented"):
            self.converter.convert_request(request, APIType.ANTHROPIC)

    def test_redacted_thinking_response_skipped(self):
        """Anthropic redacted_thinking response block 应被跳过"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "[REDACTED]"},
                {"type": "text", "text": "The answer is 42"},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        # redacted_thinking 不应出现在 reasoning_content 中
        assert result["choices"][0]["message"]["content"] == "The answer is 42"
        assert result["choices"][0]["message"].get("reasoning_content") is None

    def test_search_result_conversion(self):
        """Anthropic search_result -> OpenAI text"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "search_result", "content": "Found relevant info"},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        assert "Found relevant info" in result["choices"][0]["message"]["content"]

    def test_tool_role_tool_result_converts_to_tool_message(self):
        """非常规 tool role 中的 tool_result 不应被 fallback 分支吞掉"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_001",
                            "content": [{"type": "text", "text": "42"}],
                        }
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["messages"] == [{"role": "tool", "tool_call_id": "toolu_001", "content": "42"}]

    def test_tool_use_input_list_is_json_serialized(self):
        """非 dict tool_use input 也应输出为 JSON 字符串"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_001",
                            "name": "search",
                            "input": ["weather", "today"],
                        }
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        tool_call = result["messages"][0]["tool_calls"][0]
        arguments = tool_call["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == ["weather", "today"]

    def test_assistant_thinking_only_uses_empty_string_content(self):
        """thinking-only assistant 消息不应输出 content: None"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "plan"}],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["messages"][0]["content"] == ""

    def test_refusal_stop_reason_maps_to_content_filter(self):
        """预留 Anthropic refusal stop_reason 的 OpenAI 映射"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "I can't help with that."}],
            "model": "claude-opus-4-7",
            "stop_reason": "refusal",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        assert result["choices"][0]["finish_reason"] == "content_filter"

    def test_assistant_image_block_to_image_url(self):
        """Anthropic assistant 消息中的 image 块应转为 OpenAI image_url，不再静默丢失。"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Here is the chart:"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        msg = result["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == [
            {"type": "text", "text": "Here is the chart:"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,iVBORw0KGgo=",
                },
            },
        ]

    def test_assistant_image_only_to_image_url_array(self):
        """纯图片的 assistant 消息应保留为 image_url 数组。"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/chart.png",
                            },
                        },
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        msg = result["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == [
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/chart.png"},
            },
        ]


class TestAnthropicToChatReviewFixes:
    """边界条件的回归测试。"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    # #5 input_json_delta 缺少前置 content_block_start 时 fallback 到最后已知 tc_idx
    def test_input_json_delta_fallback_uses_last_tool_call_index(self):
        # 公共协议模拟两次 tool_use start（block_index 2、3 → tc_idx 0、1），
        # 随后一个 input_json_delta 的 block_index 与已知映射不匹配 ——
        # fallback 应当回到 tool_call_index-1 而不是 block_index 原值
        for block_index, tool_id in ((2, "toolu_a"), (3, "toolu_b")):
            self.converter.convert_stream_chunk(
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "tool_use", "id": tool_id, "name": "f", "input": {}},
                },
                APIType.ANTHROPIC.value,
            )

        chunk = {
            "type": "content_block_delta",
            "index": 99,  # 未知 block_index
            "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'},
        }
        (out,) = self.converter.convert_stream_chunk(chunk, APIType.ANTHROPIC.value)
        tool_call = out["choices"][0]["delta"]["tool_calls"][0]
        # tool_call_index - 1 = 1，不应是 99
        assert tool_call["index"] == 1
        assert tool_call["index"] != 99

    # #8 tools 内的 cache_control 也应被识别为不支持参数
    def test_cache_control_on_tools_detected(self, caplog):
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "go"}],
            "tools": [
                {
                    "name": "search",
                    "input_schema": {"type": "object", "properties": {}},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        # 转换应成功，且产物中不应保留 cache_control
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "cache_control" not in json.dumps(result)
        # 工具定义应被正确转换
        assert result["tools"][0]["type"] == "function"
        assert result["tools"][0]["function"]["name"] == "search"
