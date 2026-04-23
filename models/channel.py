import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from models.api_types import APIType


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


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    api_type: Optional[APIType] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None
    enabled: Optional[bool] = None
    weight: Optional[int] = None
    priority: Optional[int] = None
    socks5_proxy: Optional[str] = None
