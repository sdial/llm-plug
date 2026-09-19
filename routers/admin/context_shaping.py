"""Context Shaping 管理端权威预览。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from context_shaping.prompts import PromptInjectionError, preview_prompt
from models.api_types import APIType

from .common import AdminAuthRoute

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


class ContextShapingPreviewRequest(BaseModel):
    api_type: str
    caveman_enabled: bool = False
    custom_enabled: bool = False
    custom_text: str = ""


@router.post("/context-shaping/preview")
async def preview_context_shaping(body: ContextShapingPreviewRequest):
    if body.api_type not in {item.value for item in APIType}:
        raise HTTPException(status_code=400, detail="unsupported api_type")
    try:
        return preview_prompt(
            api_type=body.api_type,
            caveman_enabled=body.caveman_enabled,
            custom_enabled=body.custom_enabled,
            custom_text=body.custom_text,
        )
    except (ValueError, PromptInjectionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
