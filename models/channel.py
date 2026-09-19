import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.api_types import APIType
from models.upstream_profile import DEFAULT_CATALOG_REVISION, ProfileOverrides


class ModelCapabilities(BaseModel):
    """单个模型的能力覆盖配置"""

    supports_image_content: bool = False
    supports_audio_content: bool = False
    supports_file_content: bool = False


class AnthropicVersionPolicy(str, Enum):
    CHANNEL = "channel"
    CLIENT = "client"
    CHANNEL_IF_MISSING = "channel_if_missing"


class AnthropicBetaPolicy(str, Enum):
    CHANNEL = "channel"
    CLIENT = "client"
    MERGE = "merge"
    CHANNEL_IF_MISSING = "channel_if_missing"


class Endpoint(BaseModel):
    """渠道上一种 API 格式的原生入口（ADR-0006）"""

    api_type: APIType
    base_url: str
    url_override: str | None = None
    models_url: str | None = None
    api_key_override: str | None = None
    enabled: bool = True
    anthropic_version: str | None = None
    anthropic_version_policy: AnthropicVersionPolicy = AnthropicVersionPolicy.CHANNEL
    anthropic_beta: str | None = None
    anthropic_beta_policy: AnthropicBetaPolicy = AnthropicBetaPolicy.CHANNEL
    profile_overrides: ProfileOverrides = Field(default_factory=ProfileOverrides)

    @model_validator(mode="before")
    @classmethod
    def _reject_endpoint_model_overrides(cls, data: Any) -> Any:
        if isinstance(data, dict) and "model_overrides" in data:
            raise ValueError("请求包含已废弃的接入点模型覆盖；请改用渠道级 model_overrides")
        return data

    @model_validator(mode="after")
    def _require_non_blank_base_url(self):
        if not self.base_url.strip():
            raise ValueError("base_url 不能为空或纯空白")
        return self


# 扁平协议字段 → 接入点字段的映射（legacy 磁盘数据迁移专用：normalize_channel_payload 归一与管理端拒绝门 _reject_flat_payload 消费）；
# endpoint_url 在两级化中更名为 url_override。模型本体不再接受扁平形态。
_LEGACY_FLAT_FIELD_MAP: dict[str, str] = {
    "api_type": "api_type",
    "base_url": "base_url",
    "endpoint_url": "url_override",
    "models_url": "models_url",
    "anthropic_version": "anthropic_version",
    "anthropic_version_policy": "anthropic_version_policy",
    "anthropic_beta": "anthropic_beta",
    "anthropic_beta_policy": "anthropic_beta_policy",
}
_LEGACY_FLAT_KEYS = frozenset(_LEGACY_FLAT_FIELD_MAP)


def _reject_flat_payload(payload: Any) -> None:
    """管理端契约收紧：出现废弃扁平协议键即拒绝，避免静默丢弃或歧义覆写。

    仅约束管理 API 的 Create/Update 入口；存储迁移走 normalize_channel_payload，不受影响。
    """
    if not isinstance(payload, dict):
        return
    found = sorted(_LEGACY_FLAT_KEYS & payload.keys())
    if found:
        raise ValueError(
            f"请求包含已废弃的扁平渠道字段（扁平契约已下线）: {', '.join(found)}；请改用嵌套 endpoints 结构（[{{api_type, base_url, ...}}]）"
        )


def _reject_legacy_profile_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    found = sorted({"capabilities", "model_capabilities", "allow_format_conversion"} & payload.keys())
    if found:
        raise ValueError(f"请求包含已废弃的能力字段: {', '.join(found)}；请改用 upstream_profile_id、catalog_revision 与 profile_overrides")


def _reject_channel_input_modalities(payload: Any) -> None:
    """渠道本身不声明模型输入模态；管理员差异必须绑定到明确模型。"""
    if not isinstance(payload, dict):
        return
    profile_overrides = payload.get("profile_overrides")
    if isinstance(profile_overrides, ProfileOverrides):
        input_modalities = profile_overrides.capabilities.input_modalities
    elif isinstance(profile_overrides, dict):
        capabilities = profile_overrides.get("capabilities")
        input_modalities = capabilities.get("input_modalities") if isinstance(capabilities, dict) else None
    else:
        input_modalities = None
    if isinstance(input_modalities, dict) and input_modalities:
        raise ValueError("渠道不能覆盖图片、音频或文件能力；请改用渠道级 model_overrides")


def normalize_channel_payload(payload: Any) -> Any:
    """把渠道载荷归一为嵌套形态（纯函数，存储迁移与模型校验共用）。

    - 无 endpoints 且有扁平协议字段：包裹为单接入点（存量数据迁移）
    - 已有 endpoints 时扁平协议字段按"键存在即生效"覆写首个接入点
      （管理端 PUT 部分更新语义：显式 null 清空、缺省不动）
    """
    if not isinstance(payload, dict):
        return payload
    flat = {mapped_key: payload[flat_key] for flat_key, mapped_key in _LEGACY_FLAT_FIELD_MAP.items() if flat_key in payload}
    rest = {k: v for k, v in payload.items() if k not in _LEGACY_FLAT_KEYS}
    endpoints = rest.get("endpoints")
    if not endpoints:
        if flat:
            rest["endpoints"] = [flat]
    elif flat:
        first = dict(endpoints[0])
        first.update(flat)
        rest["endpoints"] = [first, *endpoints[1:]]
    return rest


def migrate_channel_profile_payload(payload: Any) -> tuple[Any, bool]:
    """把旧布尔能力迁入 ADR-0031 的档案引用与差异覆盖。"""
    if not isinstance(payload, dict):
        return payload, False
    migrated = "upstream_profile_id" not in payload or "catalog_revision" not in payload
    result = dict(payload)
    if "upstream_profile_id" not in result:
        exact_urls = {
            "https://api.openai.com/v1": "openai",
            "https://api.anthropic.com": "anthropic",
            "https://api.deepseek.com": "deepseek",
            "https://api.minimax.chat/v1": "minimax",
            "https://dashscope.aliyuncs.com/compatible-mode/v1": "qwen",
            "https://openrouter.ai/api/v1": "openrouter",
        }
        endpoint_urls = {
            str(endpoint.get("base_url", "")).strip().rstrip("/").lower() for endpoint in result.get("endpoints", []) if isinstance(endpoint, dict)
        }
        matches = {profile_id for url, profile_id in exact_urls.items() if url in endpoint_urls}
        result["upstream_profile_id"] = next(iter(matches)) if len(matches) == 1 else "generic"
    result.setdefault("catalog_revision", DEFAULT_CATALOG_REVISION)
    legacy = result.pop("capabilities", None)
    model_caps = result.pop("model_capabilities", None)
    if legacy is not None or model_caps is not None:
        migrated = True
    channel_override = dict(result.get("profile_overrides") or {})
    override_caps = dict(channel_override.get("capabilities") or {})
    legacy_input_modalities = override_caps.pop("input_modalities", None)
    if legacy_input_modalities:
        # 旧渠道级模态无法无歧义地分配给各模型；删除后重新继承 Model Profile。
        migrated = True
    features = dict(override_caps.get("features") or {})
    if isinstance(legacy, dict):
        for source, target in (
            ("supports_parallel_tool_calls", "parallel_tool_calls"),
            ("supports_tool_choice_auto", "tool_choice_auto"),
            ("supports_tool_choice_required", "tool_choice_required"),
            ("supports_response_format", "structured_output"),
            ("supports_reasoning_effort", "reasoning"),
            ("supports_strict_tools", "strict_tools"),
        ):
            if source in legacy:
                features[target] = "supported" if legacy[source] else "unsupported"
        for action in ("normalize_developer_role", "filter_think_content", "requires_single_system_message"):
            if action in legacy:
                channel_override[action] = bool(legacy[action])
    if features:
        override_caps["features"] = features
    elif "features" in override_caps:
        override_caps.pop("features")
    if any(override_caps.get(namespace) for namespace in override_caps):
        channel_override["capabilities"] = override_caps
    else:
        channel_override.pop("capabilities", None)
    if any(value is not None for value in channel_override.values()):
        result["profile_overrides"] = channel_override
    else:
        result.pop("profile_overrides", None)

    channel_model_overrides = dict(result.get("model_overrides") or {})
    endpoints = []
    for endpoint in result.get("endpoints", []):
        if not isinstance(endpoint, dict):
            endpoints.append(endpoint)
            continue
        next_endpoint = dict(endpoint)
        endpoint_model_overrides = next_endpoint.pop("model_overrides", None)
        if isinstance(endpoint_model_overrides, dict):
            migrated = True
            # 历史 UI 会向每个接入点写入相同值；异常冲突时按接入点顺序取首个值。
            for model_id, values in endpoint_model_overrides.items():
                channel_model_overrides.setdefault(model_id, values)
        endpoints.append(next_endpoint)
    result["endpoints"] = endpoints

    if isinstance(model_caps, dict):
        for model_id, values in model_caps.items():
            if not isinstance(values, dict):
                continue
            per_model = dict(channel_model_overrides.get(model_id) or {})
            per_caps = dict(per_model.get("capabilities") or {})
            per_inputs = dict(per_caps.get("input_modalities") or {})
            for source, target in (
                ("supports_image_content", "image"),
                ("supports_audio_content", "audio"),
                ("supports_file_content", "file"),
            ):
                if source in values:
                    per_inputs[target] = "supported" if values[source] else "unsupported"
            if per_inputs:
                per_caps["input_modalities"] = per_inputs
                per_model["capabilities"] = per_caps
                channel_model_overrides[model_id] = per_model
    if channel_model_overrides:
        result["model_overrides"] = channel_model_overrides
    else:
        result.pop("model_overrides", None)
    return result, migrated


class _EndpointCarrierMixin(BaseModel):
    """endpoints 数组的校验与持久化逻辑（Channel / ChannelCreate 共用）。

    扁平载荷的归一化 before-validator 不放在这里：子类归一化语义不同
    （Channel 直接嵌套；Create 需保证至少一个接入点），且 pydantic 会叠加
    父类验证器导致执行顺序难以推理。
    """

    endpoints: list[Endpoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_endpoint_invariants(self):
        if not self.endpoints:
            raise ValueError("渠道至少需要一个接入点（api_type / base_url）")
        seen: set[str] = set()
        for ep in self.endpoints:
            fmt = ep.api_type.value
            if fmt in seen:
                raise ValueError(f"渠道内 api_type 重复: {fmt}")
            seen.add(fmt)
        return self

    def to_storage_dict(self) -> dict[str, Any]:
        """持久化形态：嵌套结构的完整字典。

        兼容视图删除后与 model_dump() 等价；保留方法名以稳定既有调用方。
        """
        return self.model_dump(exclude={"capabilities", "model_capabilities"})

    def selected_endpoint(self) -> Endpoint:
        """当前选定的接入点（协议属性的唯一来源）。

        调度层按 ADR-0006 把渠道投影为单接入点形态（proxy/routing），
        下游消费方（URL 构造 / header 组装 / Capability 推断）据此取值，
        不再读渠道级扁平协议字段。
        """
        if not self.endpoints:
            raise ValueError("渠道没有任何接入点")
        return self.endpoints[0]

    def enabled_endpoint_for(self, api_type: APIType) -> Endpoint | None:
        """按格式查找启用接入点；不存在或已停用返回 None"""
        return next(
            (ep for ep in self.endpoints if ep.enabled and ep.api_type == api_type),
            None,
        )


class Channel(_EndpointCarrierMixin):
    id: str = Field(default_factory=lambda: f"ch_{uuid.uuid4().hex[:8]}")
    name: str
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    rate_limit_rpm: int | None = Field(default=None, ge=0)
    socks5_proxy: str | None = None
    upstream_profile_id: str = "generic"
    catalog_revision: str = DEFAULT_CATALOG_REVISION
    profile_overrides: ProfileOverrides = Field(default_factory=ProfileOverrides)
    model_overrides: dict[str, ProfileOverrides] = Field(default_factory=dict)
    capabilities: dict[str, Any] | None = None
    model_capabilities: dict[str, ModelCapabilities] | None = None
    allow_format_conversion: bool | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _reject_channel_modalities(cls, data: Any) -> Any:
        _reject_channel_input_modalities(data)
        return data


class ChannelCreate(_EndpointCarrierMixin):
    """创建渠道的请求体：仅接受嵌套结构，扁平契约显式拒绝（票据05 收紧）"""

    name: str
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    rate_limit_rpm: int | None = Field(default=None, ge=0)
    socks5_proxy: str | None = None
    upstream_profile_id: str = "generic"
    catalog_revision: str = DEFAULT_CATALOG_REVISION
    profile_overrides: ProfileOverrides = Field(default_factory=ProfileOverrides)
    model_overrides: dict[str, ProfileOverrides] = Field(default_factory=dict)
    capabilities: dict[str, Any] | None = None
    model_capabilities: dict[str, ModelCapabilities] | None = None
    allow_format_conversion: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_flat_payload(cls, data: Any) -> Any:
        _reject_flat_payload(data)
        _reject_legacy_profile_payload(data)
        _reject_channel_input_modalities(data)
        return data


class ChannelUpdate(BaseModel):
    """部分更新的请求体：仅保留真实渠道级字段与嵌套 endpoints（扁平键显式拒绝）。

    body 含 endpoints 即整组替换；不含则保持旧接入点不动（exclude_unset 合并）。
    """

    name: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    enabled: bool | None = None
    weight: int | None = Field(default=None, ge=1)
    priority: int | None = Field(default=None, ge=1)
    rate_limit_rpm: int | None = Field(default=None, ge=0)
    socks5_proxy: str | None = None
    upstream_profile_id: str | None = None
    catalog_revision: str | None = None
    profile_overrides: ProfileOverrides | None = None
    model_overrides: dict[str, ProfileOverrides] | None = None
    confirm_profile_change: bool = False
    capabilities: dict[str, Any] | None = None
    model_capabilities: dict[str, ModelCapabilities] | None = None
    allow_format_conversion: bool | None = None
    endpoints: list[Endpoint] | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_flat_payload(cls, data: Any) -> Any:
        _reject_flat_payload(data)
        _reject_legacy_profile_payload(data)
        _reject_channel_input_modalities(data)
        if isinstance(data, dict):
            # Optional 类型只表达 PATCH 中的“未提供”；这些领域字段本身不能清为 null。
            # 可清空的 socks5_proxy / rate_limit_rpm 保持原有语义。
            required = (
                "name",
                "api_key",
                "models",
                "enabled",
                "weight",
                "priority",
                "upstream_profile_id",
                "catalog_revision",
                "profile_overrides",
                "model_overrides",
                "endpoints",
            )
            null_fields = [name for name in required if name in data and data[name] is None]
            if null_fields:
                raise ValueError(f"以下字段不能为 null: {', '.join(null_fields)}")
            if "endpoints" in data and data["endpoints"] == []:
                raise ValueError("endpoints 不能为空")
        return data
