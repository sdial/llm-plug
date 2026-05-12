import json

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
        assert (
            result["choices"][0]["message"]["reasoning_content"] == "Let me analyze..."
        )
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

    def test_document_content_block_conversion(self):
        """Anthropic document content block -> OpenAI text"""
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
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        user_msg = result["messages"][0]
        assert user_msg["role"] == "user"
        # document content 应被转为文本
        content = user_msg.get("content")
        if isinstance(content, list):
            assert any(
                "PDF content here" in c.get("text", "")
                for c in content
                if c.get("type") == "text"
            )
        else:
            assert "PDF content here" in content

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
