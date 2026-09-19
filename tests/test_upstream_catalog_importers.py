import json

from models.upstream_profile import CapabilityState
from upstream_catalog_importers import import_litellm_models, import_litellm_providers, import_models_dev, merge_sources


def _raw(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def test_models_dev_imports_provider_url_and_multimodal_capabilities():
    upstreams, models, provenance = import_models_dev(
        _raw(
            {
                "relay": {
                    "id": "relay",
                    "name": "Relay",
                    "npm": "@ai-sdk/openai-compatible",
                    "api": "https://relay.example/v1",
                    "models": {
                        "vision-1": {
                            "id": "vision-1",
                            "attachment": True,
                            "tool_call": True,
                            "modalities": {"input": "text image audio", "output": "text"},
                            "limit": {"context": 1000, "output": 100},
                        }
                    },
                }
            }
        ),
        fetched_at="2026-09-09T00:00:00Z",
    )
    assert upstreams[0].endpoints[0].canonical_base_url == "https://relay.example/v1"
    assert models[0].capabilities.state("input_modalities", "image") is CapabilityState.SUPPORTED
    assert models[0].capabilities.state("input_modalities", "audio") is CapabilityState.SUPPORTED
    assert models[0].capabilities.state("input_modalities", "file") is CapabilityState.SUPPORTED
    assert models[0].capabilities.state("features", "tools") is CapabilityState.SUPPORTED
    assert models[0].capabilities.state("features", "reasoning") is CapabilityState.UNKNOWN
    assert provenance.source == "models.dev"


def test_litellm_missing_boolean_remains_unknown_and_explicit_false_is_unsupported():
    models, _ = import_litellm_models(
        _raw(
            {
                "relay/model": {
                    "litellm_provider": "relay",
                    "supports_vision": False,
                    "supports_function_calling": True,
                }
            }
        ),
        fetched_at="2026-09-09T00:00:00Z",
    )
    caps = models[0].capabilities
    assert caps.state("input_modalities", "image") is CapabilityState.UNSUPPORTED
    assert caps.state("input_modalities", "audio") is CapabilityState.UNKNOWN
    assert caps.state("features", "tools") is CapabilityState.SUPPORTED


def test_merge_is_deterministic_and_models_dev_wins_conflicts():
    md_upstreams, md_models, md_source = import_models_dev(
        _raw(
            {
                "relay": {
                    "id": "relay",
                    "name": "Relay",
                    "npm": "@ai-sdk/openai-compatible",
                    "api": "https://relay.example/v1",
                    "models": {"model": {"id": "model", "tool_call": True, "modalities": {"input": "text", "output": "text"}}},
                }
            }
        ),
        fetched_at="first-fetch",
    )
    llm_upstreams, llm_provider_source = import_litellm_providers(
        _raw({"relay": {"base_url": "https://other.example/v1"}}),
        fetched_at="first-fetch",
    )
    llm_models, llm_model_source = import_litellm_models(
        _raw({"relay/model": {"litellm_provider": "relay", "supports_function_calling": False, "supports_vision": True}}),
        fetched_at="first-fetch",
    )
    first = merge_sources(
        md_upstreams,
        md_models,
        llm_upstreams,
        llm_models,
        [md_source, llm_provider_source, llm_model_source],
        generated_at="first",
    )
    second = merge_sources(
        md_upstreams,
        md_models,
        llm_upstreams,
        llm_models,
        [md_source, llm_provider_source, llm_model_source],
        generated_at="second",
    )
    assert first.revision == second.revision
    assert first.model_profiles[0].capabilities.state("features", "tools") is CapabilityState.SUPPORTED
    assert first.model_profiles[0].capabilities.state("input_modalities", "image") is CapabilityState.SUPPORTED
    assert {conflict.path for conflict in first.conflicts} == {
        "model_profiles.relay:model.capabilities.features.tools",
        "upstream_profiles.relay.openai-chat-completions.canonical_base_url",
    }


def test_revision_ignores_fetch_metadata_and_builtin_actions_survive_refresh():
    raw = _raw(
        {
            "deepseek": {
                "id": "deepseek",
                "name": "DeepSeek refreshed",
                "npm": "@ai-sdk/openai-compatible",
                "api": "https://api.deepseek.com",
                "models": {},
            }
        }
    )
    first_profiles, first_models, first_source = import_models_dev(raw, fetched_at="first", source_version="etag-a")
    second_profiles, second_models, second_source = import_models_dev(raw, fetched_at="second", source_version="etag-b")
    first = merge_sources(first_profiles, first_models, [], [], [first_source], generated_at="first")
    second = merge_sources(second_profiles, second_models, [], [], [second_source], generated_at="second")

    assert first.revision == second.revision
    deepseek = next(profile for profile in first.upstream_profiles if profile.id == "deepseek")
    assert deepseek.filter_think_content is True
    assert deepseek.capabilities.state("features", "parallel_tool_calls") is CapabilityState.UNSUPPORTED
