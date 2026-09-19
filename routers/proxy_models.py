from typing import Annotated

from fastapi import APIRouter, Query

from channel_catalog import catalog
from models.api_types import APIType

router = APIRouter(tags=["代理"])


async def _collect_models() -> list[dict]:
    """从已启用渠道和模型组中聚合可请求的模型 ID（去重）。"""
    snapshot = await catalog.snapshot()
    seen: set[str] = set()
    models: list[dict] = []
    for ch in snapshot.channels:
        if not ch.enabled:
            continue
        for m in ch.models:
            if m not in seen:
                seen.add(m)
                models.append({"id": m, "api_type": ch.selected_endpoint().api_type.value, "is_group": False})
    for group in snapshot.model_groups:
        if not group.enabled:
            continue
        if group.name in seen:
            # 同名时请求会优先命中模型组，因此也按模型组的跨入口可请求语义展示。
            next(model for model in models if model["id"] == group.name)["is_group"] = True
            continue
        seen.add(group.name)
        models.append({"id": group.name, "api_type": None, "is_group": True})
    return models


# ── OpenAI Chat Completions / Response 共用 ──


@router.get("/v1/models")
async def list_models_openai():
    models = await _collect_models()
    data = [
        {
            "id": m["id"],
            "object": "model",
            "created": 0,
            "owned_by": "proxy",
        }
        for m in models
    ]
    return {"object": "list", "data": data}


# ── Anthropic ──


@router.get("/v1/anthropic/models")
async def list_models_anthropic(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[str | None, Query()] = None,
    after: Annotated[str | None, Query()] = None,
):
    models = await _collect_models()
    anthropic_models = [m for m in models if m["is_group"] or m["api_type"] == APIType.ANTHROPIC.value]

    start = 0
    end = len(anthropic_models)
    if after:
        for i, m in enumerate(anthropic_models):
            if m["id"] == after:
                start = i + 1
                break
    if before:
        for i, m in enumerate(anthropic_models):
            if m["id"] == before:
                end = i
                break

    window = anthropic_models[start:end]
    if before and not after:
        page_start = max(0, len(window) - limit)
        page = window[page_start:]
        has_more = page_start > 0
    else:
        page = window[:limit]
        has_more = len(window) > limit

    data = [
        {
            "id": m["id"],
            "type": "model",
            "display_name": m["id"],
            "created_at": "",
        }
        for m in page
    ]
    return {
        "data": data,
        "has_more": has_more,
        "first_id": page[0]["id"] if page else "",
        "last_id": page[-1]["id"] if page else "",
    }
