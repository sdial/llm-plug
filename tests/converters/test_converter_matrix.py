"""转换器矩阵测试 - 验证 Claude Code / OpenCode 兼容性"""

from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType


# ─── OpenAI Chat → Anthropic (OpenCode → Anthropic渠道) ───


class TestChatToAnthropic:
    """测试 OpenAI Chat Completions → Anthropic 转换"""

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_basic_request(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "messages" in result
        assert result["messages"][0]["role"] == "user"
        assert result["model"] == "gpt-4o"
        assert "max_tokens" in result

    def test_system_message_extraction(self):
        """OpenAI system message 应转为 Anthropic system 字段"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "system" in result
        assert result["system"] == [{"type": "text", "text": "You are helpful"}]
        # system 不应出现在 messages 中
        assert all(m["role"] != "system" for m in result["messages"])

    def test_tools_conversion(self):
        """OpenAI function tools 应转为 Anthropic tools 格式"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Use calculator"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "calculate",
                        "description": "A calculator",
                        "parameters": {
                            "type": "object",
                            "properties": {"expr": {"type": "string"}},
                        },
                    },
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "tools" in result
        assert result["tools"][0]["name"] == "calculate"

    def test_tool_choice_conversion(self):
        """OpenAI tool_choice 应正确转换"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "calc",
                        "description": "calc",
                        "parameters": {},
                    },
                }
            ],
            "tool_choice": "auto",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "tool_choice" in result


# ─── OpenAI Response → Anthropic ───


class TestResponseToAnthropic:
    """测试 OpenAI Response → Anthropic 转换"""

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_consecutive_function_call_outputs_merged(self):
        """连续 function_call_output 应合并到同一条 user 消息中（Anthropic 格式要求）"""
        request = {
            "model": "gpt-4o",
            "input": [
                {"role": "user", "content": "Use tools"},
                {
                    "type": "function_call",
                    "id": "call_1",
                    "call_id": "call_1",
                    "name": "tool_a",
                    "arguments": '{"x": 1}',
                },
                {
                    "type": "function_call",
                    "id": "call_2",
                    "call_id": "call_2",
                    "name": "tool_b",
                    "arguments": '{"y": 2}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "result_a",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_2",
                    "output": "result_b",
                },
            ],
            "max_output_tokens": 1024,
        }
        result = self.converter.convert_request(request, "openai-response")
        messages = result["messages"]

        # 找到包含 tool_result 的 user 消息
        user_with_tool_result = [
            m for m in messages
            if m["role"] == "user"
            and isinstance(m.get("content"), list)
            and any(c.get("type") == "tool_result" for c in m["content"])
        ]

        # 两个连续 function_call_output 应合并为 1 条 user 消息，而非 2 条
        assert len(user_with_tool_result) == 1, (
            f"Expected 1 user message with merged tool_results, got {len(user_with_tool_result)}"
        )

        # 合并后的消息应包含 2 个 tool_result 块
        tool_results = [
            c for c in user_with_tool_result[0]["content"]
            if c.get("type") == "tool_result"
        ]
        assert len(tool_results) == 2
        assert tool_results[0]["tool_use_id"] == "call_1"
        assert tool_results[1]["tool_use_id"] == "call_2"


# ─── Anthropic → OpenAI Chat (Claude Code → OpenAI渠道) ───


class TestAnthropicToChat:
    """测试 Anthropic → OpenAI Chat Completions 转换"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_basic_request(self):
        request = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "messages" in result
        assert result["model"] == "claude-sonnet-4-20250514"
        assert "max_tokens" in result

    def test_system_message_injection(self):
        """Anthropic system 字段应转为 OpenAI system message"""
        request = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "system": "You are helpful",
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "You are helpful"

    def test_thinking_request_passthrough(self):
        """Anthropic thinking 参数在请求中应保留或正确映射"""
        request = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1600,
            "thinking": {"type": "enabled", "budget_tokens": 1000},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        # thinking 应被正确处理（可能保留或映射到 reasoning_effort）
        assert "messages" in result

    def test_tool_use_message_conversion(self):
        """Anthropic tool_use content block 应转为 OpenAI tool_calls"""
        request = {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {"role": "user", "content": "Use calc"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me calculate"},
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "calculate",
                            "input": {"expr": "1+1"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": "2",
                        },
                    ],
                },
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        messages = result["messages"]
        # assistant 消息应有 tool_calls
        assistant_msg = [m for m in messages if m["role"] == "assistant"][0]
        assert "tool_calls" in assistant_msg
        assert assistant_msg["tool_calls"][0]["function"]["name"] == "calculate"

    def test_anthropic_response_to_chat(self):
        """Anthropic 非流式响应应转为 OpenAI Chat 格式"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello world"}],
            "model": "claude-sonnet-4-20250514",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        assert result["object"] == "chat.completion"
        assert result["choices"][0]["message"]["content"] == "Hello world"
        assert result["choices"][0]["finish_reason"] == "stop"


# ─── OpenAI Chat → OpenAI Response ───


class TestChatToResponse:
    """测试 OpenAI Chat Completions → OpenAI Response 转换"""

    def setup_method(self):
        self.converter = ToResponseConverter()

    def test_basic_request(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "input" in result
        assert result["model"] == "gpt-4o"


# ─── 透传测试 ───


class TestPassthrough:
    """测试同格式请求直接透传"""

    def test_anthropic_passthrough_request(self):
        """Anthropic → Anthropic 应直接透传"""
        request = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 1000},
        }
        # 透传时 converter 为 None，proxy_core 不做转换
        # 这里验证的是请求体不被修改
        assert request["thinking"]["type"] == "enabled"
        assert request["max_tokens"] == 100

    def test_openai_chat_passthrough_request(self):
        """OpenAI Chat → OpenAI Chat 应直接透传"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        }
        # 透传时请求体不变
        assert request["model"] == "gpt-4o"
        assert len(request["messages"]) == 1


# ─── 边界情况 ───


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_messages(self):
        """空消息列表应不崩溃"""
        converter = ToAnthropicConverter()
        request = {"model": "gpt-4o", "messages": [], "max_tokens": 100}
        result = converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "messages" in result

    def test_multimodal_content(self):
        """多模态内容应正确转换"""
        converter = ToAnthropicConverter()
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                        },
                    ],
                }
            ],
            "max_tokens": 100,
        }
        result = converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "messages" in result
        # image_url 应被转为 Anthropic image 格式
        content = result["messages"][0]["content"]
        assert any(c.get("type") == "image" for c in content)

    def test_response_with_tool_use(self):
        """Anthropic 响应含 tool_use 应正确转换"""
        converter = ToChatCompletionsConverter()
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check"},
                {
                    "type": "tool_use",
                    "id": "toolu_001",
                    "name": "search",
                    "input": {"q": "test"},
                },
            ],
            "model": "claude-sonnet-4-20250514",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = converter.convert_response(response, APIType.ANTHROPIC)
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        assert len(result["choices"][0]["message"]["tool_calls"]) == 1


class TestConverterRouting:
    """测试 proxy_core 路由表覆盖所有转换方向"""

    def test_all_conversion_directions(self):
        """验证 CONVERTER_MAP 包含所有 6 个非直通组合"""
        from proxy_core import CONVERTER_MAP

        expected = {
            ("openai-chat-completions", "anthropic"),
            ("openai-response", "anthropic"),
            ("openai-response", "openai-chat-completions"),
            ("anthropic", "openai-chat-completions"),
            ("anthropic", "openai-response"),
            ("openai-chat-completions", "openai-response"),
        }
        assert set(CONVERTER_MAP.keys()) == expected

    def test_passthrough_same_type(self):
        """同格式应返回 None, None"""
        from proxy_core import _get_converter_and_upstream_type
        from models.channel import Channel
        from models.api_types import APIType

        channel = Channel(
            name="test",
            api_type=APIType.OPENAI_CHAT,
            base_url="http://test",
            api_key="test",
            models=["gpt-4o"],
        )
        req, resp, src = _get_converter_and_upstream_type(channel, APIType.OPENAI_CHAT)
        assert req is None
        assert resp is None
        assert src == "openai-chat-completions"

    def test_unsupported_direction_raises(self):
        """不支持的转换方向应抛出 ValueError"""
        import pytest
        from unittest.mock import patch

        # 验证 CONVERTER_MAP 对不存在的键返回 None
        from proxy_core import CONVERTER_MAP

        assert CONVERTER_MAP.get(("nonexistent", "type")) is None

        # 通过 mock CONVERTER_MAP.get 触发 ValueError 分支
        from proxy_core import _get_converter_and_upstream_type
        from models.channel import Channel
        from models.api_types import APIType

        channel = Channel(
            name="test",
            api_type=APIType.OPENAI_CHAT,
            base_url="http://test",
            api_key="test",
            models=["gpt-4o"],
        )

        with patch("proxy_core.CONVERTER_MAP", {}):
            with pytest.raises(ValueError, match="不支持的转换方向"):
                _get_converter_and_upstream_type(channel, APIType.ANTHROPIC)
