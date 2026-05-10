import re

import pytest
from pydantic import ValidationError

from models.api_key import ApiKey, ApiKeyCreate, ApiKeyUpdate
from models.api_types import APIType
from models.channel import Channel, ChannelCreate, ChannelUpdate


class TestAPIType:
    def test_enum_values(self):
        assert APIType.OPENAI_CHAT == "openai-chat-completions"
        assert APIType.OPENAI_RESPONSE == "openai-response"
        assert APIType.ANTHROPIC == "anthropic"

    def test_from_string(self):
        assert APIType("openai-chat-completions") == APIType.OPENAI_CHAT
        assert APIType("openai-response") == APIType.OPENAI_RESPONSE
        assert APIType("anthropic") == APIType.ANTHROPIC

    def test_invalid_string_raises_error(self):
        with pytest.raises(ValueError):
            APIType("invalid")


class TestChannel:
    def test_creates_with_defaults(self):
        ch = Channel(
            name="Test Channel",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
        )
        assert ch.name == "Test Channel"
        assert ch.api_type == APIType.OPENAI_CHAT
        assert ch.base_url == "https://api.openai.com"
        assert ch.api_key == "sk-test"
        assert ch.models == []
        assert ch.enabled is True
        assert ch.weight == 1
        assert ch.priority == 1
        assert ch.socks5_proxy is None
        assert ch.id.startswith("ch_")
        assert re.match(r"ch_[a-f0-9]{8}", ch.id)

    def test_id_is_unique(self):
        ch1 = Channel(name="A", api_type=APIType.OPENAI_CHAT, base_url="https://a.com", api_key="k1")
        ch2 = Channel(name="B", api_type=APIType.OPENAI_CHAT, base_url="https://b.com", api_key="k2")
        assert ch1.id != ch2.id

    def test_weight_must_be_positive(self):
        with pytest.raises(ValidationError):
            Channel(
                name="Test",
                api_type=APIType.OPENAI_CHAT,
                base_url="https://api.openai.com",
                api_key="sk-test",
                weight=0,
            )

    def test_priority_must_be_positive(self):
        with pytest.raises(ValidationError):
            Channel(
                name="Test",
                api_type=APIType.OPENAI_CHAT,
                base_url="https://api.openai.com",
                api_key="sk-test",
                priority=0,
            )

    def test_created_at_is_iso_format(self):
        ch = Channel(
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
        )
        assert ch.created_at.endswith("+00:00")

    def test_models_list(self):
        ch = Channel(
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4", "gpt-3.5-turbo"],
        )
        assert ch.models == ["gpt-4", "gpt-3.5-turbo"]

    def test_capabilities_can_be_configured(self):
        ch = Channel(
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            capabilities={"filter_think_content": True},
        )
        assert ch.capabilities == {"filter_think_content": True}


class TestChannelCreate:
    def test_all_fields_required_except_defaults(self):
        cc = ChannelCreate(
            name="New Channel",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
        )
        assert cc.name == "New Channel"
        assert cc.api_type == APIType.ANTHROPIC
        assert cc.models == []
        assert cc.enabled is True

    def test_weight_validation(self):
        with pytest.raises(ValidationError):
            ChannelCreate(
                name="Test",
                api_type=APIType.OPENAI_CHAT,
                base_url="https://api.openai.com",
                api_key="sk-test",
                weight=0,
            )


class TestChannelUpdate:
    def test_all_fields_optional(self):
        cu = ChannelUpdate()
        assert cu.name is None
        assert cu.api_type is None
        assert cu.base_url is None
        assert cu.api_key is None
        assert cu.models is None
        assert cu.enabled is None
        assert cu.weight is None
        assert cu.priority is None
        assert cu.socks5_proxy is None

    def test_partial_update(self):
        cu = ChannelUpdate(name="Updated", enabled=False)
        assert cu.name == "Updated"
        assert cu.enabled is False
        assert cu.base_url is None

    def test_weight_validation(self):
        with pytest.raises(ValidationError):
            ChannelUpdate(weight=0)

    def test_priority_validation(self):
        with pytest.raises(ValidationError):
            ChannelUpdate(priority=0)


class TestApiKey:
    def test_creates_with_defaults(self):
        key = ApiKey(name="Test Key")
        assert key.name == "Test Key"
        assert key.id.startswith("key_")
        assert re.match(r"key_[a-f0-9]{8}", key.id)
        assert key.key.startswith("llmplug-api-")
        assert key.allowed_models == []
        assert key.notes == ""
        assert key.request_count == 0
        assert key.total_input_tokens == 0
        assert key.total_output_tokens == 0
        assert key.created_at.endswith("+00:00")

    def test_key_is_unique(self):
        k1 = ApiKey(name="A")
        k2 = ApiKey(name="B")
        assert k1.key != k2.key

    def test_key_format(self):
        key = ApiKey(name="Test")
        assert re.match(r"llmplug-api-[a-f0-9]{32}", key.key)

    def test_allowed_models(self):
        key = ApiKey(name="Test", allowed_models=["gpt-4", "claude-opus-4-7"])
        assert key.allowed_models == ["gpt-4", "claude-opus-4-7"]


class TestApiKeyCreate:
    def test_name_required(self):
        with pytest.raises(ValidationError):
            ApiKeyCreate()

    def test_optional_key(self):
        akc = ApiKeyCreate(name="Test", key="custom-key")
        assert akc.key == "custom-key"

    def test_defaults(self):
        akc = ApiKeyCreate(name="Test")
        assert akc.allowed_models == []
        assert akc.notes == ""
        assert akc.key is None


class TestApiKeyUpdate:
    def test_all_fields_optional(self):
        aku = ApiKeyUpdate()
        assert aku.name is None
        assert aku.key is None
        assert aku.allowed_models is None
        assert aku.notes is None

    def test_partial_update(self):
        aku = ApiKeyUpdate(name="Updated", notes="New notes")
        assert aku.name == "Updated"
        assert aku.notes == "New notes"
        assert aku.key is None
