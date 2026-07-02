import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class ModelGroup(BaseModel):
    """模型组配置，组内模型按顺序 Fallback"""

    id: str = Field(default_factory=lambda: f"grp_{uuid.uuid4().hex[:8]}")
    name: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ModelGroupCreate(BaseModel):
    name: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True


class ModelGroupUpdate(BaseModel):
    name: Optional[str] = None
    models: Optional[list[str]] = None
    enabled: Optional[bool] = None


class LBConfig(BaseModel):
    """负载均衡全局配置"""

    max_fail_count: int = Field(default=5, ge=1)
    cooldown_seconds: int = Field(default=60, ge=1)
