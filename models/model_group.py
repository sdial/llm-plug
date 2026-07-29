import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class TimeWindow(BaseModel):
    """每日重复的屏蔽时间窗口"""

    start: str  # "HH:MM"
    end: str  # "HH:MM"
    enabled: bool = True

    @field_validator("start", "end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError(f"时间格式必须为 HH:MM，收到: {v}")
        h, m = int(v[:2]), int(v[3:])
        if h > 23 or m > 59:
            raise ValueError(f"时间值无效: {v}")
        return v


class ModelGroup(BaseModel):
    """模型组配置，组内模型按顺序 Fallback"""

    id: str = Field(default_factory=lambda: f"grp_{uuid.uuid4().hex[:8]}")
    name: str
    models: list[str] = Field(default_factory=list)
    model_schedules: dict[str, list[TimeWindow]] = Field(default_factory=dict)
    enabled: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ModelGroupCreate(BaseModel):
    name: str
    models: list[str] = Field(default_factory=list)
    model_schedules: dict[str, list[TimeWindow]] = Field(default_factory=dict)
    enabled: bool = True


class ModelGroupUpdate(BaseModel):
    name: str | None = None
    models: list[str] | None = None
    model_schedules: dict[str, list[TimeWindow]] | None = None
    enabled: bool | None = None


class LBConfig(BaseModel):
    """负载均衡全局配置"""

    max_fail_count: int = Field(default=5, ge=1)
    cooldown_seconds: int = Field(default=60, ge=1)
