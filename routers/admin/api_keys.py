"""API Key 管理域路由：CRUD、明文查看、重新生成。"""

import secrets

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from models.api_key import ApiKey, ApiKeyCreate, ApiKeyUpdate
from storage import atomic_update_api_keys, invalidate_keys_cache

from .common import AdminAuthRoute, _get_api_keys

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


def _normalized_api_key_name(name: str) -> str:
    """Return the comparison form used by the API Key name uniqueness rule."""
    return name.strip().casefold()


def _ensure_unique_api_key_name(keys: list[dict], name: str, *, exclude_id: str | None = None) -> None:
    """Raise a conflict while the storage lock is held if a name is already used."""
    normalized_name = _normalized_api_key_name(name)
    if any(key.get("id") != exclude_id and _normalized_api_key_name(str(key.get("name", ""))) == normalized_name for key in keys):
        raise HTTPException(status_code=409, detail="API Key 名称已存在")


@router.get("/api-keys")
async def list_api_keys():
    """获取所有 API Key（Key 脱敏），统计数据从 PG 聚合"""
    import stats as _stats

    keys = await _get_api_keys()
    key_stats = await _stats.get_api_key_stats()
    result = []
    for k in keys:
        d = k.model_dump()
        raw = d.get("key", "")
        d["key"] = raw[:8] + "***" if len(raw) > 8 else "***"
        lookup = k.name or k.id
        s = key_stats.get(lookup, {})
        d["request_count"] = s.get("request_count", 0)
        d["total_input_tokens"] = s.get("total_input_tokens", 0)
        d["total_output_tokens"] = s.get("total_output_tokens", 0)
        result.append(d)
    return result


@router.post("/api-keys", response_model=ApiKey)
async def create_api_key(body: ApiKeyCreate):
    """添加 API Key"""
    data = body.model_dump(exclude_none=True)
    key = ApiKey(**data)

    def _mutate(d: dict):
        keys_raw = d.setdefault("api_keys", [])
        _ensure_unique_api_key_name(keys_raw, key.name)
        keys_raw.append(key.model_dump())
        return d

    await atomic_update_api_keys(_mutate)
    await invalidate_keys_cache()
    return key


@router.put("/api-keys/{key_id}", response_model=ApiKey)
async def update_api_key(key_id: str, body: ApiKeyUpdate):
    """更新 API Key"""
    update_data = body.model_dump(exclude_unset=True)
    state: dict = {}

    def _mutate(d: dict):
        keys_raw = d.get("api_keys", [])
        for i, k_dict in enumerate(keys_raw):
            if k_dict.get("id") == key_id:
                # 完整重新校验（model_copy 不做校验，显式 null 会绕过 pydantic 写坏数据）
                updated = ApiKey(**{**k_dict, **update_data})
                _ensure_unique_api_key_name(keys_raw, updated.name, exclude_id=key_id)
                keys_raw[i] = updated.model_dump()
                state["updated"] = updated
                d["api_keys"] = keys_raw
                return d
        return None  # not found

    try:
        result = await atomic_update_api_keys(_mutate)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"字段校验失败: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    await invalidate_keys_cache()
    return state["updated"]


@router.delete("/api-keys/{key_id}")
async def delete_api_key(key_id: str):
    """删除 API Key"""

    def _mutate(d: dict):
        keys_raw = d.get("api_keys", [])
        new_keys = [k for k in keys_raw if k.get("id") != key_id]
        if len(new_keys) == len(keys_raw):
            return None  # not found
        d["api_keys"] = new_keys
        return d

    result = await atomic_update_api_keys(_mutate)
    if result is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    await invalidate_keys_cache()
    return {"message": "删除成功"}


@router.get("/api-keys/{key_id}/key")
async def get_api_key_value(key_id: str):
    """获取 API Key 的完整值（用于复制）"""
    keys = await _get_api_keys()
    for k in keys:
        if k.id == key_id:
            return {"key": k.key}
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.patch("/api-keys/{key_id}/regenerate", response_model=ApiKey)
async def regenerate_api_key(key_id: str):
    """重新生成 API Key"""
    state: dict = {}

    def _mutate(d: dict):
        keys_raw = d.get("api_keys", [])
        for i, k_dict in enumerate(keys_raw):
            if k_dict.get("id") == key_id:
                old = ApiKey(**k_dict)
                new_key_value = f"sk-{secrets.token_hex(24)}"
                updated = old.model_copy(update={"key": new_key_value})
                keys_raw[i] = updated.model_dump()
                state["updated"] = updated
                d["api_keys"] = keys_raw
                return d
        return None  # not found

    result = await atomic_update_api_keys(_mutate)
    if result is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    await invalidate_keys_cache()
    return state["updated"]
