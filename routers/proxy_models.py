from fastapi import APIRouter, Header, Query

from models.api_types import APIType
from models.channel import Channel
from routers.auth import check_proxy_authorization
from routers.proxy_errors import unauthorized
from storage import load_data

router = APIRouter(tags=["代理"])


def _collect_models() -> list[dict]:
    """从所有已启用渠道中聚合模型列表（去重）"""
    data = load_data()
    channels = [Channel(**ch) for ch in data.get("channels", [])]
    seen: set[str] = set()
    models: list[dict] = []
    for ch in channels:
        if not ch.enabled:
            continue
        for m in ch.models:
            if m not in seen:
                seen.add(m)
                models.append({"id": m, "api_type": ch.api_type.value})
    return models


# ── OpenAI Chat Completions / Response 共用 ──

@router.get("/v1/models")
async def list_models_openai(authorization: str | None = Header(None)):
    if not check_proxy_authorization(authorization):
        return unauthorized()

    models = _collect_models()
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
    authorization: str | None = Header(None),
    limit: int = Query(default=20, ge=1, le=100),
    before: str | None = Query(default=None),
    after: str | None = Query(default=None),
):
    if not check_proxy_authorization(authorization):
        return unauthorized()

    models = _collect_models()
    # 只取 anthropic 类型的模型
    anthropic_models = [m for m in models if m["api_type"] == APIType.ANTHROPIC.value]
    # 如果没有专门的 anthropic 模型，则返回全部
    if not anthropic_models:
        anthropic_models = models

    # 简单分页
    start = 0
    if after:
        for i, m in enumerate(anthropic_models):
            if m["id"] == after:
                start = i + 1
                break
    if before:
        end_idx = len(anthropic_models)
        for i, m in enumerate(anthropic_models):
            if m["id"] == before:
                end_idx = i
                break
    else:
        end_idx = len(anthropic_models)
    page = anthropic_models[start:start + limit]
    if before:
        page = anthropic_models[max(0, end_idx - limit):end_idx]

    data = [
        {
            "id": m["id"],
            "type": "model",
            "display_name": m["id"],
            "created_at": "",
        }
        for m in page
    ]
    has_more = end_idx < len(anthropic_models)
    return {"data": data, "has_more": has_more, "first_id": page[0]["id"] if page else "", "last_id": page[-1]["id"] if page else ""}
