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
