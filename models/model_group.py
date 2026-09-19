import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator, model_validator


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


class ModelGroupEntry(BaseModel):
    """模型组条目：model + 可选硬绑定渠道 + 条目级调度"""

    model: str
    channel_id: str | None = None  # None = 不指定渠道，走负载均衡
    schedules: list[TimeWindow] = Field(default_factory=list)


def _migrate_legacy_fields(data: object) -> object:
    """把旧结构 ``models`` + ``model_schedules`` 构造入参转换为 ``items``。"""
    if not isinstance(data, dict) or "items" in data:
        return data
    models = data.get("models") or []
    model_schedules = data.get("model_schedules") or {}
    data = dict(data)
    data.pop("models", None)
    data.pop("model_schedules", None)
    data["items"] = [
        {
            "model": m,
            "channel_id": None,
            "schedules": model_schedules.get(m, []),
        }
        for m in models
    ]
    return data


class ModelGroup(BaseModel):
    """模型组配置，组内条目按顺序 Fallback"""

    id: str = Field(default_factory=lambda: f"grp_{uuid.uuid4().hex[:8]}")
    name: str
    items: list[ModelGroupEntry] = Field(default_factory=list)
    enabled: bool = True
    lazy_sticky: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: object) -> object:
        return _migrate_legacy_fields(data)


class ModelGroupCreate(BaseModel):
    name: str
    items: list[ModelGroupEntry] = Field(default_factory=list)
    enabled: bool = True
    lazy_sticky: bool = False

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: object) -> object:
        return _migrate_legacy_fields(data)


class ModelGroupUpdate(BaseModel):
    name: str | None = None
    items: list[ModelGroupEntry] | None = None
    enabled: bool | None = None
    lazy_sticky: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: object) -> object:
        return _migrate_legacy_fields(data)


class LBConfig(BaseModel):
    """负载均衡全局配置"""

    max_fail_count: int = Field(default=3, ge=1)
    cooldown_seconds: int = Field(default=120, ge=1)
