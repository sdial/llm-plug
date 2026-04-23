from enum import Enum


class APIType(str, Enum):
    OPENAI_CHAT = "openai-chat-completions"
    OPENAI_RESPONSE = "openai-response"
    ANTHROPIC = "anthropic"
