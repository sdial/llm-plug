import re

import pytest
from pydantic import ValidationError

from models.api_key import ApiKey, ApiKeyCreate, ApiKeyUpdate
from models.api_types import APIType
from models.channel import (
    AnthropicBetaPolicy,
    AnthropicVersionPolicy,
    Channel,
    ChannelCreate,
    ChannelUpdate,
    ModelCapabilities,
)


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
        ch1 = Channel(
            name="A",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://a.com",
            api_key="k1",
        )
        ch2 = Channel(
            name="B",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://b.com",
            api_key="k2",
        )
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

    def test_anthropic_header_policies_have_defaults(self):
        ch = Channel(
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
        )
        assert ch.anthropic_version is None
        assert ch.anthropic_version_policy == AnthropicVersionPolicy.CHANNEL
        assert ch.anthropic_beta is None
        assert ch.anthropic_beta_policy == AnthropicBetaPolicy.CHANNEL

    def test_advanced_urls_default_to_none(self):
        ch = Channel(
            name="OpenAI",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
        )

        assert ch.endpoint_url is None
        assert ch.models_url is None

    def test_advanced_urls_can_be_configured(self):
        ch = Channel(
            name="Custom",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.example.com",
            endpoint_url="https://gateway.example.com/custom/chat",
            models_url="https://gateway.example.com/custom/models",
            api_key="sk-test",
        )

        assert ch.endpoint_url == "https://gateway.example.com/custom/chat"
        assert ch.models_url == "https://gateway.example.com/custom/models"

    def test_model_capabilities_defaults_to_none(self):
        ch = Channel(
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
        )
        assert ch.model_capabilities is None

    def test_model_capabilities_can_be_configured(self):
        ch = Channel(
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            model_capabilities={
                "gpt-4o": ModelCapabilities(
                    supports_image_content=True,
                    supports_file_content=True,
                ),
            },
        )
        assert ch.model_capabilities is not None
        assert "gpt-4o" in ch.model_capabilities
        assert ch.model_capabilities["gpt-4o"].supports_image_content is True
        assert ch.model_capabilities["gpt-4o"].supports_file_content is True
        assert ch.model_capabilities["gpt-4o"].supports_audio_content is False

    def test_model_capabilities_serialization(self):
        ch = Channel(
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            model_capabilities={
                "gpt-4o": ModelCapabilities(supports_image_content=True),
            },
        )
        dumped = ch.model_dump()
        assert dumped["model_capabilities"] == {
            "gpt-4o": {
                "supports_image_content": True,
                "supports_audio_content": False,
                "supports_file_content": False,
            }
        }


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

    def test_anthropic_policy_fields(self):
        cc = ChannelCreate(
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            anthropic_version="2024-10-22",
            anthropic_version_policy="client",
            anthropic_beta="prompt-caching-2024-07-31",
            anthropic_beta_policy="merge",
        )
        assert cc.anthropic_version == "2024-10-22"
        assert cc.anthropic_version_policy == AnthropicVersionPolicy.CLIENT
        assert cc.anthropic_beta_policy == AnthropicBetaPolicy.MERGE

    def test_advanced_urls_fields(self):
        cc = ChannelCreate(
            name="Custom",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.example.com",
            endpoint_url="https://gateway.example.com/custom/chat",
            models_url="https://gateway.example.com/custom/models",
            api_key="sk-test",
        )

        assert cc.endpoint_url == "https://gateway.example.com/custom/chat"
        assert cc.models_url == "https://gateway.example.com/custom/models"


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

    def test_policy_update_fields(self):
        cu = ChannelUpdate(
            anthropic_version="2024-10-22",
            anthropic_version_policy="channel_if_missing",
            anthropic_beta_policy="client",
        )
        assert cu.anthropic_version == "2024-10-22"
        assert cu.anthropic_version_policy == AnthropicVersionPolicy.CHANNEL_IF_MISSING
        assert cu.anthropic_beta_policy == AnthropicBetaPolicy.CLIENT

    def test_advanced_urls_update_fields(self):
        cu = ChannelUpdate(
            endpoint_url="https://gateway.example.com/custom/chat",
            models_url="https://gateway.example.com/custom/models",
        )

        assert cu.endpoint_url == "https://gateway.example.com/custom/chat"
        assert cu.models_url == "https://gateway.example.com/custom/models"

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
