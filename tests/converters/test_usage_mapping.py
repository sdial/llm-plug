from converters.usage import (
    anthropic_to_openai_chat,
    anthropic_to_openai_response,
    openai_chat_to_anthropic,
    openai_response_to_anthropic,
)


class TestAnthropicToOpenAIChat:
    def test_full_fields(self):
        result = anthropic_to_openai_chat(
            {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 50,
            }
        )
        assert result == {
            "prompt_tokens": 1110,
            "completion_tokens": 50,
            "total_tokens": 1160,
            "prompt_tokens_details": {"cached_tokens": 1000},
        }

    def test_missing_cache_fields(self):
        result = anthropic_to_openai_chat({"input_tokens": 5, "output_tokens": 7})
        assert result == {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 0},
        }

    def test_empty_input(self):
        result = anthropic_to_openai_chat({})
        assert result == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 0},
        }


class TestAnthropicToOpenAIResponse:
    def test_full_fields(self):
        result = anthropic_to_openai_response(
            {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 50,
            }
        )
        assert result == {
            "input_tokens": 1110,
            "output_tokens": 50,
            "total_tokens": 1160,
            "input_tokens_details": {"cached_tokens": 1000},
        }


class TestOpenAIChatToAnthropic:
    def test_full_fields(self):
        result = openai_chat_to_anthropic(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 900},
            }
        )
        assert result == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        }

    def test_no_details(self):
        result = openai_chat_to_anthropic(
            {"prompt_tokens": 50, "completion_tokens": 20}
        )
        assert result == {
            "input_tokens": 50,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def test_cached_exceeds_prompt_clamps_to_zero(self):
        result = openai_chat_to_anthropic(
            {
                "prompt_tokens": 10,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 100},
            }
        )
        assert result["input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 100


class TestOpenAIResponseToAnthropic:
    def test_full_fields(self):
        result = openai_response_to_anthropic(
            {
                "input_tokens": 1000,
                "output_tokens": 50,
                "input_tokens_details": {"cached_tokens": 900},
            }
        )
        assert result == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        }
