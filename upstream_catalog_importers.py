"""固定公共来源到 Upstream Catalog 的确定性导入器。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from models.api_types import APIType
from models.upstream_profile import (
    AuthScheme,
    CapabilityMatrix,
    CapabilityState,
    CatalogConflict,
    CatalogProvenance,
    ModelProfile,
    UpstreamCatalogDocument,
    UpstreamEndpointProfile,
    UpstreamProfile,
)
from upstream_catalog import builtin_profiles

IMPORTER_VERSION = "1"
MODELS_DEV_API_URL = "https://models.dev/api.json"
MODELS_DEV_CATALOG_URL = "https://models.dev/catalog.json"
LITELLM_MODELS_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LITELLM_PROVIDERS_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/llms/openai_like/providers.json"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _state(value: Any) -> CapabilityState:
    if value is True:
        return CapabilityState.SUPPORTED
    if value is False:
        return CapabilityState.UNSUPPORTED
    return CapabilityState.UNKNOWN


def _split_modalities(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, list):
        return [str(part) for part in value if part]
    return []


def _models_dev_api_type(provider: dict[str, Any]) -> APIType:
    npm = str(provider.get("npm", "")).lower()
    if "anthropic" in npm:
        return APIType.ANTHROPIC
    return APIType.OPENAI_CHAT


def _models_dev_capabilities(model: dict[str, Any]) -> CapabilityMatrix:
    modalities = model.get("modalities") if isinstance(model.get("modalities"), dict) else {}
    inputs = {name: CapabilityState.SUPPORTED for name in _split_modalities(modalities.get("input"))}
    outputs = {name: CapabilityState.SUPPORTED for name in _split_modalities(modalities.get("output"))}
    if model.get("attachment") is True:
        inputs.setdefault("file", CapabilityState.SUPPORTED)
    return CapabilityMatrix(
        input_modalities=inputs,
        output_modalities=outputs,
        features={
            "tools": _state(model.get("tool_call")),
            "structured_output": _state(model.get("structured_output")),
            "reasoning": _state(model.get("reasoning")),
        },
        parameters={"temperature": _state(model.get("temperature"))},
    )


def import_models_dev(
    raw: bytes, *, fetched_at: str | None = None, source_version: str | None = None
) -> tuple[list[UpstreamProfile], list[ModelProfile], CatalogProvenance]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("models.dev api.json 顶层必须是对象")
    upstreams: list[UpstreamProfile] = []
    models: list[ModelProfile] = []
    for provider_id, provider_value in sorted(payload.items()):
        if not isinstance(provider_value, dict):
            continue
        provider = provider_value
        stable_id = str(provider.get("id") or provider_id)
        api_type = _models_dev_api_type(provider)
        api = provider.get("api") if isinstance(provider.get("api"), str) else None
        auth = AuthScheme.X_API_KEY if api_type is APIType.ANTHROPIC else AuthScheme.BEARER
        upstreams.append(
            UpstreamProfile(
                id=stable_id,
                name=str(provider.get("name") or stable_id),
                filter_think_content=stable_id == "deepseek",
                requires_single_system_message=stable_id == "minimax",
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=api_type,
                        canonical_base_url=api,
                        auth_scheme=auth,
                        anthropic_version="2023-06-01" if api_type is APIType.ANTHROPIC else None,
                    )
                ],
            )
        )
        provider_models = provider.get("models")
        if not isinstance(provider_models, dict):
            continue
        for serving_id, model_value in sorted(provider_models.items()):
            if not isinstance(model_value, dict):
                continue
            model_id = str(model_value.get("id") or serving_id)
            limit = model_value.get("limit") if isinstance(model_value.get("limit"), dict) else {}
            models.append(
                ModelProfile(
                    id=f"{stable_id}:{model_id}",
                    upstream_profile_id=stable_id,
                    model_id=model_id,
                    family=str(model_value["family"]) if model_value.get("family") else None,
                    capabilities=_models_dev_capabilities(model_value),
                    limits={key: value for key, value in limit.items() if isinstance(value, (int, float, str)) or value is None},
                )
            )
    provenance = CatalogProvenance(
        source="models.dev",
        source_url=MODELS_DEV_API_URL,
        source_version=source_version,
        content_sha256=_sha256(raw),
        fetched_at=fetched_at or datetime.now(UTC).isoformat(),
        importer_version=IMPORTER_VERSION,
        license="MIT",
    )
    return upstreams, models, provenance


_LITELLM_FEATURE_FIELDS = {
    "supports_function_calling": "tools",
    "supports_tool_choice": "tool_choice",
    "supports_parallel_function_calling": "parallel_tool_calls",
    "supports_reasoning": "reasoning",
    "supports_response_schema": "structured_output",
    "supports_system_messages": "system_messages",
}


def _litellm_capabilities(entry: dict[str, Any]) -> CapabilityMatrix:
    inputs: dict[str, CapabilityState] = {}
    outputs: dict[str, CapabilityState] = {}
    for source_key, target_key in (
        ("supports_vision", "image"),
        ("supports_audio_input", "audio"),
        ("supports_pdf_input", "file"),
    ):
        if source_key in entry:
            inputs[target_key] = _state(entry[source_key])
    for source_key, target_key in (("supports_audio_output", "audio"), ("supports_image_generation", "image")):
        if source_key in entry:
            outputs[target_key] = _state(entry[source_key])
    features = {target: _state(entry[source]) for source, target in _LITELLM_FEATURE_FIELDS.items() if source in entry}
    return CapabilityMatrix(input_modalities=inputs, output_modalities=outputs, features=features)


def import_litellm_models(
    raw: bytes, *, fetched_at: str | None = None, source_version: str | None = None
) -> tuple[list[ModelProfile], CatalogProvenance]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("LiteLLM model catalog 顶层必须是对象")
    models: list[ModelProfile] = []
    for external_id, value in sorted(payload.items()):
        if external_id in {"sample_spec", "fallback_generalizations"} or not isinstance(value, dict):
            continue
        provider = value.get("litellm_provider")
        if not isinstance(provider, str) or not provider:
            continue
        model_id = external_id.split("/", 1)[-1] if "/" in external_id else external_id
        limits = {
            key: value[key]
            for key in ("max_input_tokens", "max_output_tokens", "max_tokens")
            if isinstance(value.get(key), (int, float, str)) or value.get(key) is None and key in value
        }
        models.append(
            ModelProfile(
                id=f"{provider}:{model_id}",
                upstream_profile_id=provider,
                model_id=model_id,
                capabilities=_litellm_capabilities(value),
                limits=limits,
            )
        )
    return models, CatalogProvenance(
        source="litellm-models",
        source_url=LITELLM_MODELS_URL,
        source_version=source_version,
        content_sha256=_sha256(raw),
        fetched_at=fetched_at or datetime.now(UTC).isoformat(),
        importer_version=IMPORTER_VERSION,
        license="MIT",
    )


def import_litellm_providers(
    raw: bytes, *, fetched_at: str | None = None, source_version: str | None = None
) -> tuple[list[UpstreamProfile], CatalogProvenance]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("LiteLLM provider catalog 顶层必须是对象")
    profiles: list[UpstreamProfile] = []
    for provider_id, value in sorted(payload.items()):
        if not isinstance(value, dict) or not isinstance(value.get("base_url"), str):
            continue
        endpoints = value.get("supported_endpoints") if isinstance(value.get("supported_endpoints"), list) else []
        api_types = [APIType.OPENAI_CHAT]
        if "/v1/responses" in endpoints:
            api_types.append(APIType.OPENAI_RESPONSE)
        if "/v1/messages" in endpoints:
            api_types.append(APIType.ANTHROPIC)
        profiles.append(
            UpstreamProfile(
                id=provider_id,
                name=provider_id,
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=api_type,
                        canonical_base_url=value["base_url"],
                        auth_scheme=AuthScheme.X_API_KEY if api_type is APIType.ANTHROPIC else AuthScheme.BEARER,
                        anthropic_version="2023-06-01" if api_type is APIType.ANTHROPIC else None,
                    )
                    for api_type in api_types
                ],
            )
        )
    return profiles, CatalogProvenance(
        source="litellm-providers",
        source_url=LITELLM_PROVIDERS_URL,
        source_version=source_version,
        content_sha256=_sha256(raw),
        fetched_at=fetched_at or datetime.now(UTC).isoformat(),
        importer_version=IMPORTER_VERSION,
        license="MIT",
    )


def _merge_capability_matrix(
    primary: CapabilityMatrix, supplement: CapabilityMatrix, *, path: str, conflicts: list[CatalogConflict]
) -> CapabilityMatrix:
    merged: dict[str, dict[str, CapabilityState]] = {}
    for namespace in ("input_modalities", "output_modalities", "content_carriers", "features", "parameters"):
        primary_values = dict(getattr(primary, namespace))
        for name, state in getattr(supplement, namespace).items():
            current = primary_values.get(name, CapabilityState.UNKNOWN)
            if current is CapabilityState.UNKNOWN:
                primary_values[name] = state
            elif state is not CapabilityState.UNKNOWN and state != current:
                conflicts.append(
                    CatalogConflict(
                        path=f"{path}.capabilities.{namespace}.{name}",
                        message=f"models.dev={current.value}, LiteLLM={state.value}",
                        sources=["models.dev", "litellm-models"],
                    )
                )
        merged[namespace] = primary_values
    return CapabilityMatrix(**merged)


def merge_sources(
    models_dev_profiles: list[UpstreamProfile],
    models_dev_models: list[ModelProfile],
    litellm_profiles: list[UpstreamProfile],
    litellm_models: list[ModelProfile],
    provenance: list[CatalogProvenance],
    *,
    generated_at: str | None = None,
) -> UpstreamCatalogDocument:
    conflicts: list[CatalogConflict] = []
    upstreams = {profile.id: profile.model_copy(deep=True) for profile in builtin_profiles()}
    for profile in models_dev_profiles:
        builtin = upstreams.get(profile.id)
        if builtin is None:
            upstreams[profile.id] = profile.model_copy(deep=True)
            continue
        # 外部目录负责更新展示名、URL 与模型数据；项目内置档案仍是少量协议动作的
        # 权威来源，避免一次自动刷新把 DeepSeek/MiniMax/Qwen 兼容动作抹掉。
        merged = profile.model_copy(deep=True)
        merged.aliases = sorted(set(builtin.aliases) | set(merged.aliases))
        merged.capabilities = _merge_capability_matrix(
            merged.capabilities,
            builtin.capabilities,
            path=f"upstream_profiles.{profile.id}",
            conflicts=conflicts,
        )
        merged.normalize_developer_role = builtin.normalize_developer_role
        merged.filter_think_content = builtin.filter_think_content
        merged.requires_single_system_message = builtin.requires_single_system_message
        upstreams[profile.id] = merged
    for profile in litellm_profiles:
        current = upstreams.get(profile.id)
        if current is None:
            upstreams[profile.id] = profile.model_copy(deep=True)
            continue
        existing_by_type = {endpoint.api_type: endpoint for endpoint in current.endpoints}
        for endpoint in profile.endpoints:
            existing = existing_by_type.get(endpoint.api_type)
            if existing is None:
                current.endpoints.append(endpoint.model_copy(deep=True))
            elif not existing.canonical_base_url and endpoint.canonical_base_url:
                existing.canonical_base_url = endpoint.canonical_base_url
            elif (
                existing.canonical_base_url
                and endpoint.canonical_base_url
                and existing.canonical_base_url.rstrip("/") != endpoint.canonical_base_url.rstrip("/")
            ):
                conflicts.append(
                    CatalogConflict(
                        path=f"upstream_profiles.{profile.id}.{endpoint.api_type.value}.canonical_base_url",
                        message=f"models.dev={existing.canonical_base_url}, LiteLLM={endpoint.canonical_base_url}",
                        sources=["models.dev", "litellm-providers"],
                    )
                )
    models = {model.id: model.model_copy(deep=True) for model in models_dev_models}
    for supplement in litellm_models:
        if supplement.upstream_profile_id not in upstreams:
            continue
        current = models.get(supplement.id)
        if current is None:
            models[supplement.id] = supplement.model_copy(deep=True)
            continue
        current.capabilities = _merge_capability_matrix(
            current.capabilities, supplement.capabilities, path=f"model_profiles.{current.id}", conflicts=conflicts
        )
        for key, value in supplement.limits.items():
            current.limits.setdefault(key, value)
    stamp = generated_at or datetime.now(UTC).isoformat()
    seed = json.dumps(
        {
            "upstreams": [profile.model_dump(mode="json") for profile in sorted(upstreams.values(), key=lambda item: item.id)],
            "models": [model.model_dump(mode="json") for model in sorted(models.values(), key=lambda item: item.id)],
            # 抓取时间、ETag 等运行噪声只用于审计，不参与内容 revision。
            "provenance": [
                {
                    "source": item.source,
                    "source_url": item.source_url,
                    "content_sha256": item.content_sha256,
                    "importer_version": item.importer_version,
                    "license": item.license,
                }
                for item in provenance
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    revision = f"catalog-{hashlib.sha256(seed).hexdigest()[:16]}"
    return UpstreamCatalogDocument(
        revision=revision,
        generated_at=stamp,
        upstream_profiles=sorted(upstreams.values(), key=lambda item: item.id),
        model_profiles=sorted(models.values(), key=lambda item: item.id),
        provenance=provenance,
        conflicts=conflicts,
    )


def build_candidate(
    models_dev_raw: bytes,
    litellm_models_raw: bytes,
    litellm_providers_raw: bytes,
    *,
    fetched_at: str | None = None,
    source_versions: dict[str, str | None] | None = None,
) -> UpstreamCatalogDocument:
    """把三个固定来源一次性标准化为 Catalog Candidate。"""
    stamp = fetched_at or datetime.now(UTC).isoformat()
    versions = source_versions or {}
    md_profiles, md_models, md_source = import_models_dev(
        models_dev_raw,
        fetched_at=stamp,
        source_version=versions.get("models.dev"),
    )
    llm_models, llm_models_source = import_litellm_models(
        litellm_models_raw,
        fetched_at=stamp,
        source_version=versions.get("litellm-models"),
    )
    llm_profiles, llm_profiles_source = import_litellm_providers(
        litellm_providers_raw,
        fetched_at=stamp,
        source_version=versions.get("litellm-providers"),
    )
    return merge_sources(
        md_profiles,
        md_models,
        llm_profiles,
        llm_models,
        [md_source, llm_models_source, llm_profiles_source],
        generated_at=stamp,
    )
