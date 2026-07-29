import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from models.api_types import APIType


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


class Channel(BaseModel):
    id: str = Field(default_factory=lambda: f"ch_{uuid.uuid4().hex[:8]}")
    name: str
    api_type: APIType
    base_url: str
    endpoint_url: str | None = None
    models_url: str | None = None
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    socks5_proxy: str | None = None
    capabilities: dict[str, Any] | None = None
    model_capabilities: dict[str, ModelCapabilities] | None = None
    anthropic_version: str | None = None
    anthropic_version_policy: AnthropicVersionPolicy = AnthropicVersionPolicy.CHANNEL
    anthropic_beta: str | None = None
    anthropic_beta_policy: AnthropicBetaPolicy = AnthropicBetaPolicy.CHANNEL
    allow_format_conversion: bool | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ChannelCreate(BaseModel):
    name: str
    api_type: APIType
    base_url: str
    endpoint_url: str | None = None
    models_url: str | None = None
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    socks5_proxy: str | None = None
    capabilities: dict[str, Any] | None = None
    model_capabilities: dict[str, ModelCapabilities] | None = None
    anthropic_version: str | None = None
    anthropic_version_policy: AnthropicVersionPolicy = AnthropicVersionPolicy.CHANNEL
    anthropic_beta: str | None = None
    anthropic_beta_policy: AnthropicBetaPolicy = AnthropicBetaPolicy.CHANNEL
    allow_format_conversion: bool | None = None


class ChannelUpdate(BaseModel):
    name: str | None = None
    api_type: APIType | None = None
    base_url: str | None = None
    endpoint_url: str | None = None
    models_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    enabled: bool | None = None
    weight: int | None = Field(default=None, ge=1)
    priority: int | None = Field(default=None, ge=1)
    socks5_proxy: str | None = None
    capabilities: dict[str, Any] | None = None
    model_capabilities: dict[str, ModelCapabilities] | None = None
    anthropic_version: str | None = None
    anthropic_version_policy: AnthropicVersionPolicy | None = None
    anthropic_beta: str | None = None
    anthropic_beta_policy: AnthropicBetaPolicy | None = None
    allow_format_conversion: bool | None = None
