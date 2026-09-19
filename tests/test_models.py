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
    Endpoint,
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
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
            api_key="sk-test",
        )
        assert ch.name == "Test Channel"
        ep = ch.selected_endpoint()
        assert ep.api_type == APIType.OPENAI_CHAT
        assert ep.base_url == "https://api.openai.com"
        assert ch.api_key == "sk-test"
        assert ch.models == []
        assert ch.enabled is True
        assert ch.weight == 1
        assert ch.priority == 1
        assert ch.socks5_proxy is None
        assert ch.id.startswith("ch_")
        assert re.match(r"ch_[a-f0-9]{8}", ch.id)

    def test_id_is_unique(self):
        ch1 = Channel(name="A", endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://a.com")], api_key="k1")
        ch2 = Channel(name="B", endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://b.com")], api_key="k2")
        assert ch1.id != ch2.id

    def test_weight_must_be_positive(self):
        with pytest.raises(ValidationError):
            Channel(
                name="Test",
                endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
                api_key="sk-test",
                weight=0,
            )

    def test_priority_must_be_positive(self):
        with pytest.raises(ValidationError):
            Channel(
                name="Test",
                endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
                api_key="sk-test",
                priority=0,
            )

    def test_created_at_is_iso_format(self):
        ch = Channel(
            name="Test",
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
            api_key="sk-test",
        )
        assert ch.created_at.endswith("+00:00")

    def test_models_list(self):
        ch = Channel(
            name="Test",
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
            api_key="sk-test",
            models=["gpt-4", "gpt-3.5-turbo"],
        )
        assert ch.models == ["gpt-4", "gpt-3.5-turbo"]

    def test_capabilities_can_be_configured(self):
        ch = Channel(
            name="Test",
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
            api_key="sk-test",
            capabilities={"filter_think_content": True},
        )
        assert ch.capabilities == {"filter_think_content": True}

    def test_anthropic_header_policies_have_defaults(self):
        ch = Channel(
            name="Anthropic",
            endpoints=[Endpoint(api_type=APIType.ANTHROPIC, base_url="https://api.anthropic.com")],
            api_key="ak-test",
        )
        ep = ch.endpoints[0]
        assert ep.anthropic_version is None
        assert ep.anthropic_version_policy == AnthropicVersionPolicy.CHANNEL
        assert ep.anthropic_beta is None
        assert ep.anthropic_beta_policy == AnthropicBetaPolicy.CHANNEL

    def test_advanced_urls_default_to_none(self):
        ch = Channel(
            name="OpenAI",
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
            api_key="sk-test",
        )

        assert ch.endpoints[0].url_override is None
        assert ch.endpoints[0].models_url is None

    def test_advanced_urls_can_be_configured(self):
        ch = Channel(
            name="Custom",
            endpoints=[
                Endpoint(
                    api_type=APIType.OPENAI_CHAT,
                    base_url="https://api.example.com",
                    url_override="https://gateway.example.com/custom/chat",
                    models_url="https://gateway.example.com/custom/models",
                ),
            ],
            api_key="sk-test",
        )

        assert ch.endpoints[0].url_override == "https://gateway.example.com/custom/chat"
        assert ch.endpoints[0].models_url == "https://gateway.example.com/custom/models"

    def test_model_capabilities_defaults_to_none(self):
        ch = Channel(
            name="Test",
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
            api_key="sk-test",
        )
        assert ch.model_capabilities is None

    def test_model_capabilities_can_be_configured(self):
        ch = Channel(
            name="Test",
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
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
            endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
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
            endpoints=[{"api_type": APIType.ANTHROPIC, "base_url": "https://api.anthropic.com"}],
            api_key="ak-test",
        )
        assert cc.name == "New Channel"
        assert cc.endpoints[0].api_type == APIType.ANTHROPIC
        assert cc.models == []
        assert cc.enabled is True

    def test_create_without_any_endpoint_rejected(self):
        with pytest.raises(ValidationError, match="接入点"):
            ChannelCreate(name="No Endpoint", api_key="ak-test")

    def test_weight_validation(self):
        with pytest.raises(ValidationError):
            ChannelCreate(
                name="Test",
                endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.openai.com")],
                api_key="sk-test",
                weight=0,
            )

    def test_anthropic_policy_fields(self):
        cc = ChannelCreate(
            name="Anthropic",
            endpoints=[
                {
                    "api_type": APIType.ANTHROPIC,
                    "base_url": "https://api.anthropic.com",
                    "anthropic_version": "2024-10-22",
                    "anthropic_version_policy": "client",
                    "anthropic_beta": "prompt-caching-2024-07-31",
                    "anthropic_beta_policy": "merge",
                }
            ],
            api_key="ak-test",
        )
        ep = cc.endpoints[0]
        assert ep.anthropic_version == "2024-10-22"
        assert ep.anthropic_version_policy == AnthropicVersionPolicy.CLIENT
        assert ep.anthropic_beta_policy == AnthropicBetaPolicy.MERGE

    def test_advanced_urls_fields(self):
        cc = ChannelCreate(
            name="Custom",
            endpoints=[
                {
                    "api_type": APIType.OPENAI_CHAT,
                    "base_url": "https://api.example.com",
                    "url_override": "https://gateway.example.com/custom/chat",
                    "models_url": "https://gateway.example.com/custom/models",
                }
            ],
            api_key="sk-test",
        )

        ep = cc.endpoints[0]
        assert ep.url_override == "https://gateway.example.com/custom/chat"
        assert ep.models_url == "https://gateway.example.com/custom/models"

    def test_flat_protocol_keys_are_rejected(self):
        # 票据05 契约收紧：扁平键不再被归一或覆写首个接入点，显式拒绝并点名「扁平」
        with pytest.raises(ValidationError, match="扁平"):
            ChannelCreate(
                name="Flat",
                api_type=APIType.OPENAI_CHAT,
                base_url="https://api.openai.com",
                api_key="sk-test",
            )


class TestChannelUpdate:
    def test_all_fields_optional(self):
        cu = ChannelUpdate()
        assert cu.name is None
        assert cu.api_key is None
        assert cu.models is None
        assert cu.enabled is None
        assert cu.weight is None
        assert cu.priority is None
        assert cu.socks5_proxy is None
        assert cu.endpoints is None

    def test_partial_update(self):
        cu = ChannelUpdate(name="Updated", enabled=False)
        assert cu.name == "Updated"
        assert cu.enabled is False
        # 不传 endpoints 即整组不动（PUT 合并语义由 exclude_unset 保证）
        assert cu.endpoints is None

    def test_policy_update_fields(self):
        cu = ChannelUpdate(
            endpoints=[
                {
                    "api_type": APIType.ANTHROPIC,
                    "base_url": "https://api.anthropic.com",
                    "anthropic_version": "2024-10-22",
                    "anthropic_version_policy": "channel_if_missing",
                    "anthropic_beta_policy": "client",
                }
            ]
        )
        ep = cu.endpoints[0]
        assert ep.anthropic_version == "2024-10-22"
        assert ep.anthropic_version_policy == AnthropicVersionPolicy.CHANNEL_IF_MISSING
        assert ep.anthropic_beta_policy == AnthropicBetaPolicy.CLIENT

    def test_advanced_urls_update_fields(self):
        cu = ChannelUpdate(
            endpoints=[
                {
                    "api_type": APIType.OPENAI_CHAT,
                    "base_url": "https://api.example.com",
                    "url_override": "https://gateway.example.com/custom/chat",
                    "models_url": "https://gateway.example.com/custom/models",
                }
            ]
        )

        ep = cu.endpoints[0]
        assert ep.url_override == "https://gateway.example.com/custom/chat"
        assert ep.models_url == "https://gateway.example.com/custom/models"

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
