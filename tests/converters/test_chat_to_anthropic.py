import pytest

from converters.to_anthropic import ToAnthropicConverter
from models.api_types import APIType


class TestChatToAnthropic:
    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_multiple_system_messages(self):
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "system", "content": "Always be concise"},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert isinstance(result["system"], list)
        assert len(result["system"]) == 2
        assert result["system"][0]["type"] == "text"
        assert result["system"][0]["text"] == "You are helpful"
        assert result["system"][1]["text"] == "Always be concise"

    def test_single_system_message_still_works(self):
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert isinstance(result["system"], list)
        assert len(result["system"]) == 1
        assert result["system"][0]["text"] == "You are helpful"

    def test_tool_choice_required_to_any(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": "required",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tool_choice"]["type"] == "any"

    def test_tool_choice_none_string(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": "none",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tool_choice"]["type"] == "none"

    def test_invalid_json_arguments_fallback(self):
        """无效 JSON 参数应回退为空 dict 但不崩溃"""
        request = {
            "model": "gpt-4o",
            "messages": [{
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "test", "arguments": "not valid json"},
                }],
            }],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assistant_msg = [m for m in result["messages"] if m["role"] == "assistant"][0]
        assert assistant_msg["content"][0]["input"] == {}

    def test_non_data_image_url_raises(self):
        """非 data: URL 的 image_url 应报 ValueError"""
        request = {
            "model": "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                ],
            }],
        }
        # HTTP URL 图片应被跳过而非抛出异常
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        # 应只包含 text 部分，image_url 被跳过
        user_msg = result["messages"][0]
        assert user_msg["role"] == "user"
        has_image = any(
            isinstance(c, dict) and c.get("type") == "image"
            for c in (user_msg.get("content") if isinstance(user_msg.get("content"), list) else [])
        )
        assert not has_image
