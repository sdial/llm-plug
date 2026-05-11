import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from models.api_types import APIType


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
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    socks5_proxy: Optional[str] = None
    capabilities: Optional[dict[str, Any]] = None
    anthropic_version: Optional[str] = None
    anthropic_version_policy: AnthropicVersionPolicy = AnthropicVersionPolicy.CHANNEL
    anthropic_beta: Optional[str] = None
    anthropic_beta_policy: AnthropicBetaPolicy = AnthropicBetaPolicy.CHANNEL
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ChannelCreate(BaseModel):
    name: str
    api_type: APIType
    base_url: str
    api_key: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    weight: int = Field(default=1, ge=1)
    priority: int = Field(default=1, ge=1)
    socks5_proxy: Optional[str] = None
    capabilities: Optional[dict[str, Any]] = None
    anthropic_version: Optional[str] = None
    anthropic_version_policy: AnthropicVersionPolicy = AnthropicVersionPolicy.CHANNEL
    anthropic_beta: Optional[str] = None
    anthropic_beta_policy: AnthropicBetaPolicy = AnthropicBetaPolicy.CHANNEL


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    api_type: Optional[APIType] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None
    enabled: Optional[bool] = None
    weight: Optional[int] = Field(default=None, ge=1)
    priority: Optional[int] = Field(default=None, ge=1)
    socks5_proxy: Optional[str] = None
    capabilities: Optional[dict[str, Any]] = None
    anthropic_version: Optional[str] = None
    anthropic_version_policy: Optional[AnthropicVersionPolicy] = None
    anthropic_beta: Optional[str] = None
    anthropic_beta_policy: Optional[AnthropicBetaPolicy] = None
