"""PII 脱敏管理接口：实时测试。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from pii_errors import SensitiveBlockError
from pii_filter import apply_pii_filter

from .common import AdminAuthRoute

router = APIRouter(prefix="/admin/pii-filter", tags=["管理"], route_class=AdminAuthRoute)


class PiiTestRequest(BaseModel):
    text: str
    settings: dict[str, Any]


class PiiTestResponse(BaseModel):
    text: str
    action: str | None = None
    triggered: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/test")
async def pii_filter_test(req: PiiTestRequest):
    data = {"messages": [{"role": "user", "content": req.text}]}
    try:
        _, info = apply_pii_filter(
            data,
            target_api_type="openai-chat",
            settings=req.settings,
            return_info=True,
        )
    except SensitiveBlockError as exc:
        return PiiTestResponse(
            text=data["messages"][0]["content"],
            action="block",
            triggered=[{"entity": entity} for entity in exc.triggered],
        )
    triggered = [{"entity": entity} for entity in info.get("rules_triggered", [])]
    return PiiTestResponse(
        text=data["messages"][0]["content"],
        action=info.get("action"),
        triggered=triggered,
    )
