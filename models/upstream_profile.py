"""Upstream Catalog 的领域模型（ADR-0031）。"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.api_types import APIType

DEFAULT_CATALOG_REVISION = "builtin-2"


class CapabilityState(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CapabilityMatrix(BaseModel):
    """按可扩展名称空间表达三态能力；缺失键即 unknown。"""

    model_config = ConfigDict(extra="forbid")

    input_modalities: dict[str, CapabilityState] = Field(default_factory=dict)
    output_modalities: dict[str, CapabilityState] = Field(default_factory=dict)
    content_carriers: dict[str, CapabilityState] = Field(default_factory=dict)
    features: dict[str, CapabilityState] = Field(default_factory=dict)
    parameters: dict[str, CapabilityState] = Field(default_factory=dict)

    def state(self, namespace: str, name: str) -> CapabilityState:
        values = getattr(self, namespace)
        return values.get(name, CapabilityState.UNKNOWN)


class AuthScheme(str, Enum):
    NONE = "none"
    BEARER = "bearer"
    X_API_KEY = "x-api-key"
    API_KEY = "api-key"


class UpstreamEndpointProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_type: APIType
    canonical_base_url: str | None = None
    base_url_template: str | None = None
    auth_scheme: AuthScheme = AuthScheme.BEARER
    anthropic_version: str | None = None
    capabilities: CapabilityMatrix = Field(default_factory=CapabilityMatrix)

    @model_validator(mode="after")
    def _one_url_shape(self):
        if self.canonical_base_url and self.base_url_template:
            raise ValueError("canonical_base_url 与 base_url_template 只能设置一个")
        return self


class UpstreamProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    endpoints: list[UpstreamEndpointProfile]
    capabilities: CapabilityMatrix = Field(default_factory=CapabilityMatrix)
    normalize_developer_role: bool = False
    filter_think_content: bool = False
    requires_single_system_message: bool = False

    @model_validator(mode="after")
    def _unique_api_formats(self):
        formats = [endpoint.api_type for endpoint in self.endpoints]
        if len(formats) != len(set(formats)):
            raise ValueError(f"Upstream Profile {self.id} 中 api_type 重复")
        return self


class ModelMatchRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["prefix", "suffix", "family"]
    value: str


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    upstream_profile_id: str
    model_id: str
    aliases: list[str] = Field(default_factory=list)
    family: str | None = None
    match_rules: list[ModelMatchRule] = Field(default_factory=list)
    capabilities: CapabilityMatrix = Field(default_factory=CapabilityMatrix)
    limits: dict[str, int | float | str | None] = Field(default_factory=dict)


class CatalogProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    source_url: str
    source_version: str | None = None
    content_sha256: str
    fetched_at: str
    importer_version: str
    license: str | None = None


class CatalogConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    message: str
    sources: list[str] = Field(default_factory=list)


class UpstreamCatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    revision: str
    generated_at: str
    upstream_profiles: list[UpstreamProfile]
    model_profiles: list[ModelProfile] = Field(default_factory=list)
    provenance: list[CatalogProvenance] = Field(default_factory=list)
    conflicts: list[CatalogConflict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _catalog_invariants(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.revision) or ".." in self.revision:
            raise ValueError("revision 只能包含字母、数字、点、下划线和连字符，且不能含 '..'")
        profile_ids = [profile.id for profile in self.upstream_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("Upstream Profile ID 重复")
        model_ids = [model.id for model in self.model_profiles]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("Model Profile ID 重复")
        known = set(profile_ids)
        dangling = sorted({model.upstream_profile_id for model in self.model_profiles if model.upstream_profile_id not in known})
        if dangling:
            raise ValueError(f"Model Profile 引用了不存在的 Upstream Profile: {', '.join(dangling)}")
        return self


class ProfileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_profile_id: str
    catalog_revision: str


class ProfileOverrides(BaseModel):
    """Channel/Endpoint/Model 可复用的差异覆盖形状。"""

    model_config = ConfigDict(extra="forbid")

    capabilities: CapabilityMatrix = Field(default_factory=CapabilityMatrix)
    normalize_developer_role: bool | None = None
    filter_think_content: bool | None = None
    requires_single_system_message: bool | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
