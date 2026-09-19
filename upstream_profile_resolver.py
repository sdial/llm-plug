"""把 Channel、Endpoint、模型与 Catalog Revision 合并为唯一运行契约。"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from models.api_types import APIType
from models.channel import Channel, Endpoint
from models.upstream_profile import (
    CapabilityMatrix,
    CapabilityState,
    ModelProfile,
    ProfileOverrides,
    UpstreamCatalogDocument,
    UpstreamEndpointProfile,
    UpstreamProfile,
)
from upstream_catalog import UpstreamCatalog, UpstreamCatalogError, catalog


class ProfileResolutionError(RuntimeError):
    pass


class AmbiguousModelProfileError(ProfileResolutionError):
    pass


class ResolvedUpstreamProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream_profile_id: str
    catalog_revision: str
    api_type: APIType
    model_id: str
    model_profile_id: str | None = None
    capabilities: CapabilityMatrix
    normalize_developer_role: bool = False
    filter_think_content: bool = False
    requires_single_system_message: bool = False
    sources: dict[str, str] = Field(default_factory=dict)


_CAPABILITY_NAMESPACES = (
    "input_modalities",
    "output_modalities",
    "content_carriers",
    "features",
    "parameters",
)


def _api_baseline(api_type: APIType) -> CapabilityMatrix:
    return CapabilityMatrix(
        input_modalities={"text": CapabilityState.SUPPORTED},
        output_modalities={"text": CapabilityState.SUPPORTED},
        features={"streaming": CapabilityState.SUPPORTED},
    )


def _merge_matrix(target: CapabilityMatrix, overlay: CapabilityMatrix, layer: str, sources: dict[str, str]) -> CapabilityMatrix:
    values = target.model_dump()
    for namespace in _CAPABILITY_NAMESPACES:
        destination = values[namespace]
        for name, state in getattr(overlay, namespace).items():
            destination[name] = state
            sources[f"capabilities.{namespace}.{name}"] = layer
    return CapabilityMatrix.model_validate(values)


def _legacy_overrides(channel: Channel, model_id: str) -> ProfileOverrides:
    raw = channel.capabilities if isinstance(channel.capabilities, dict) else {}
    inputs = {}
    for legacy_name, modality in (
        ("supports_image_content", "image"),
        ("supports_audio_content", "audio"),
        ("supports_file_content", "file"),
    ):
        if legacy_name in raw:
            inputs[modality] = CapabilityState.SUPPORTED if raw[legacy_name] else CapabilityState.UNSUPPORTED
    features = {}
    for legacy_name, feature in (
        ("supports_parallel_tool_calls", "parallel_tool_calls"),
        ("supports_tool_choice_auto", "tool_choice_auto"),
        ("supports_tool_choice_required", "tool_choice_required"),
        ("supports_response_format", "structured_output"),
        ("supports_reasoning_effort", "reasoning"),
        ("supports_strict_tools", "strict_tools"),
    ):
        if legacy_name in raw:
            features[feature] = CapabilityState.SUPPORTED if raw[legacy_name] else CapabilityState.UNSUPPORTED
    model_caps = channel.model_capabilities.get(model_id) if channel.model_capabilities and model_id else None
    if model_caps is not None:
        inputs.update(
            {
                "image": CapabilityState.SUPPORTED if model_caps.supports_image_content else CapabilityState.UNSUPPORTED,
                "audio": CapabilityState.SUPPORTED if model_caps.supports_audio_content else CapabilityState.UNSUPPORTED,
                "file": CapabilityState.SUPPORTED if model_caps.supports_file_content else CapabilityState.UNSUPPORTED,
            }
        )
    return ProfileOverrides(
        capabilities=CapabilityMatrix(input_modalities=inputs, features=features),
        normalize_developer_role=raw.get("normalize_developer_role") if "normalize_developer_role" in raw else None,
        filter_think_content=raw.get("filter_think_content") if "filter_think_content" in raw else None,
    )


def _select_model_profile(document: UpstreamCatalogDocument, upstream_profile_id: str, model_id: str) -> ModelProfile | None:
    candidates = [model for model in document.model_profiles if model.upstream_profile_id == upstream_profile_id]
    stages = [
        [model for model in candidates if model.model_id == model_id],
        [model for model in candidates if model_id in model.aliases],
        [
            model
            for model in candidates
            if any(
                (rule.kind == "prefix" and model_id.startswith(rule.value))
                or (rule.kind == "suffix" and model_id.endswith(rule.value))
                or (rule.kind == "family" and (model_id == rule.value or model_id.startswith(f"{rule.value}-")))
                for rule in model.match_rules
            )
        ],
    ]
    for matches in stages:
        if len(matches) > 1:
            raise AmbiguousModelProfileError(f"模型 {model_id} 匹配多个档案: {', '.join(sorted(model.id for model in matches))}")
        if matches:
            return matches[0]
    return None


def _profile_and_endpoint(
    document: UpstreamCatalogDocument,
    upstream_profile_id: str,
    api_type: APIType,
) -> tuple[UpstreamProfile, UpstreamEndpointProfile]:
    profile = next((item for item in document.upstream_profiles if item.id == upstream_profile_id), None)
    if profile is None:
        raise ProfileResolutionError(f"Upstream Profile 不存在: {upstream_profile_id}")
    endpoint = next((item for item in profile.endpoints if item.api_type == api_type), None)
    if endpoint is None:
        raise ProfileResolutionError(f"Upstream Profile {upstream_profile_id} 不支持 {api_type.value}")
    return profile, endpoint


def resolve_from_document(
    document: UpstreamCatalogDocument,
    channel: Channel,
    endpoint: Endpoint,
    model_id: str,
) -> ResolvedUpstreamProfile:
    if document.revision != channel.catalog_revision:
        raise ProfileResolutionError(f"Channel revision={channel.catalog_revision}，加载的是 {document.revision}")
    profile, endpoint_profile = _profile_and_endpoint(document, channel.upstream_profile_id, endpoint.api_type)
    sources: dict[str, str] = {}
    capabilities = _merge_matrix(CapabilityMatrix(), _api_baseline(endpoint.api_type), "api-format", sources)
    capabilities = _merge_matrix(capabilities, profile.capabilities, f"upstream:{profile.id}", sources)
    capabilities = _merge_matrix(capabilities, endpoint_profile.capabilities, f"upstream-endpoint:{endpoint.api_type.value}", sources)
    model_profile = _select_model_profile(document, profile.id, model_id)
    if model_profile is not None:
        capabilities = _merge_matrix(capabilities, model_profile.capabilities, f"model:{model_profile.id}", sources)

    normalize_developer_role = profile.normalize_developer_role
    filter_think_content = profile.filter_think_content
    requires_single_system_message = profile.requires_single_system_message
    overlays = [
        ("legacy-channel", _legacy_overrides(channel, model_id)),
        ("channel", channel.profile_overrides),
        ("endpoint", endpoint.profile_overrides),
        ("channel-model", channel.model_overrides.get(model_id, ProfileOverrides())),
    ]
    for layer, overlay in overlays:
        capabilities = _merge_matrix(capabilities, overlay.capabilities, layer, sources)
        if overlay.normalize_developer_role is not None:
            normalize_developer_role = overlay.normalize_developer_role
            sources["normalize_developer_role"] = layer
        if overlay.filter_think_content is not None:
            filter_think_content = overlay.filter_think_content
            sources["filter_think_content"] = layer
        if overlay.requires_single_system_message is not None:
            requires_single_system_message = overlay.requires_single_system_message
            sources["requires_single_system_message"] = layer
    return ResolvedUpstreamProfile(
        upstream_profile_id=profile.id,
        catalog_revision=document.revision,
        api_type=endpoint.api_type,
        model_id=model_id,
        model_profile_id=model_profile.id if model_profile else None,
        capabilities=capabilities,
        normalize_developer_role=normalize_developer_role,
        filter_think_content=filter_think_content,
        requires_single_system_message=requires_single_system_message,
        sources=sources,
    )


async def resolve_upstream_profile(
    channel: Channel,
    endpoint: Endpoint,
    model_id: str,
    *,
    upstream_catalog: UpstreamCatalog = catalog,
) -> ResolvedUpstreamProfile:
    try:
        document = await upstream_catalog.revision(channel.catalog_revision)
    except UpstreamCatalogError as exc:
        raise ProfileResolutionError(f"Channel {channel.id} 无法加载 revision {channel.catalog_revision}: {exc}") from exc
    return resolve_from_document(document, channel, endpoint, model_id)


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        raise ValueError("Base URL 必须包含 scheme 与 host")
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, f"{host}{port}", path, "", ""))


def exact_url_profile_match(document: UpstreamCatalogDocument, base_url: str, api_type: APIType) -> str | None:
    normalized = normalize_base_url(base_url)
    matches = {
        profile.id
        for profile in document.upstream_profiles
        for endpoint in profile.endpoints
        if endpoint.api_type == api_type and endpoint.canonical_base_url and normalize_base_url(endpoint.canonical_base_url) == normalized
    }
    return next(iter(matches)) if len(matches) == 1 else None
