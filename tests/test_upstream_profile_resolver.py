import json

import pytest

from channel_catalog import ChannelCatalog
from models.api_types import APIType
from models.channel import Channel, Endpoint
from models.upstream_profile import (
    CapabilityMatrix,
    CapabilityState,
    ModelMatchRule,
    ModelProfile,
    ProfileOverrides,
    UpstreamCatalogDocument,
    UpstreamEndpointProfile,
    UpstreamProfile,
)
from upstream_profile_resolver import AmbiguousModelProfileError, exact_url_profile_match, resolve_from_document


def _document(*models: ModelProfile) -> UpstreamCatalogDocument:
    return UpstreamCatalogDocument(
        revision="catalog-test",
        generated_at="2026-09-09T00:00:00Z",
        upstream_profiles=[
            UpstreamProfile(
                id="relay",
                name="Relay",
                capabilities=CapabilityMatrix(features={"tools": CapabilityState.SUPPORTED}),
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=APIType.OPENAI_CHAT,
                        canonical_base_url="https://relay.example/v1/",
                        capabilities=CapabilityMatrix(input_modalities={"image": CapabilityState.SUPPORTED}),
                    )
                ],
            )
        ],
        model_profiles=list(models),
    )


def _channel(**updates) -> Channel:
    payload = {
        "id": "ch_test",
        "name": "test",
        "api_key": "secret",
        "models": ["vision-1"],
        "upstream_profile_id": "relay",
        "catalog_revision": "catalog-test",
        "endpoints": [{"api_type": "openai-chat-completions", "base_url": "https://custom.example/v1"}],
    }
    payload.update(updates)
    return Channel(**payload)


def test_resolver_merges_layers_and_reports_source():
    model = ModelProfile(
        id="relay:vision-1",
        upstream_profile_id="relay",
        model_id="vision-1",
        capabilities=CapabilityMatrix(input_modalities={"audio": CapabilityState.SUPPORTED}),
    )
    channel = _channel(
        model_overrides={"vision-1": ProfileOverrides(capabilities=CapabilityMatrix(input_modalities={"image": CapabilityState.SUPPORTED}))},
        endpoints=[
            Endpoint(
                api_type=APIType.OPENAI_CHAT,
                base_url="https://custom.example/v1",
            )
        ],
    )
    resolved = resolve_from_document(_document(model), channel, channel.endpoints[0], "vision-1")
    assert resolved.capabilities.state("input_modalities", "text") is CapabilityState.SUPPORTED
    assert resolved.capabilities.state("input_modalities", "audio") is CapabilityState.SUPPORTED
    assert resolved.capabilities.state("input_modalities", "image") is CapabilityState.SUPPORTED
    assert resolved.sources["capabilities.input_modalities.image"] == "channel-model"
    assert resolved.sources["capabilities.input_modalities.audio"] == "model:relay:vision-1"


def test_model_match_uses_exact_then_alias_then_bounded_rule_and_rejects_ambiguity():
    exact = ModelProfile(id="relay:exact", upstream_profile_id="relay", model_id="model", aliases=["alias"])
    family = ModelProfile(
        id="relay:family",
        upstream_profile_id="relay",
        model_id="family-template",
        match_rules=[ModelMatchRule(kind="family", value="family")],
    )
    channel = _channel()
    assert resolve_from_document(_document(exact, family), channel, channel.endpoints[0], "model").model_profile_id == "relay:exact"
    assert resolve_from_document(_document(exact, family), channel, channel.endpoints[0], "alias").model_profile_id == "relay:exact"
    assert resolve_from_document(_document(exact, family), channel, channel.endpoints[0], "family-v2").model_profile_id == "relay:family"
    assert resolve_from_document(_document(exact, family), channel, channel.endpoints[0], "xfamily-v2").model_profile_id is None

    duplicate = family.model_copy(update={"id": "relay:family-2"}, deep=True)
    with pytest.raises(AmbiguousModelProfileError):
        resolve_from_document(_document(family, duplicate), channel, channel.endpoints[0], "family-v2")


def test_exact_url_association_requires_unique_full_normalized_match():
    document = _document()
    assert exact_url_profile_match(document, "HTTPS://RELAY.EXAMPLE/v1", APIType.OPENAI_CHAT) == "relay"
    assert exact_url_profile_match(document, "https://relay.example", APIType.OPENAI_CHAT) is None
    duplicate = document.upstream_profiles[0].model_copy(update={"id": "relay-copy"}, deep=True)
    document.upstream_profiles.append(duplicate)
    assert exact_url_profile_match(document, "https://relay.example/v1", APIType.OPENAI_CHAT) is None


@pytest.mark.asyncio
async def test_channel_catalog_migrates_legacy_capabilities_without_dual_write(tmp_path):
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "id": "ch_old",
                        "name": "old",
                        "api_key": "key",
                        "models": ["m"],
                        "endpoints": [{"api_type": "openai-chat-completions", "base_url": "https://relay.example/v1"}],
                        "capabilities": {"normalize_developer_role": True, "supports_parallel_tool_calls": False},
                        "model_capabilities": {"m": {"supports_image_content": True}},
                    }
                ],
                "model_groups": [],
            }
        ),
        encoding="utf-8",
    )
    catalog = ChannelCatalog(path=lambda: str(path))
    channel = (await catalog.snapshot()).channels[0]
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert channel.upstream_profile_id == "generic"
    assert channel.catalog_revision == "builtin-2"
    assert channel.profile_overrides.normalize_developer_role is True
    assert channel.profile_overrides.capabilities.state("features", "parallel_tool_calls") is CapabilityState.UNSUPPORTED
    assert channel.model_overrides["m"].capabilities.state("input_modalities", "image") is CapabilityState.SUPPORTED
    assert "model_overrides" not in stored["channels"][0]["endpoints"][0]
    assert stored["schema_version"] == 3
    assert "capabilities" not in stored["channels"][0]
    assert "model_capabilities" not in stored["channels"][0]
    assert len(list(tmp_path.glob("channels.json.pre-endpoints-*.bak"))) == 1
