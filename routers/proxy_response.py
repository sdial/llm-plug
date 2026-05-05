import os

from fastapi import HTTPException

from config import DATA_DIR, get_setting
from models.api_types import APIType
from routers.proxy_base import make_proxy_router
from state_store import FileStore

router = make_proxy_router("/v1/responses", APIType.OPENAI_RESPONSE)

# 初始化 FileStore
_session_dir = os.path.join(DATA_DIR, "responses_session")
_store = FileStore(
    data_dir=_session_dir,
    max_entries=get_setting("response_state_max_entries") or 1000,
    ttl_minutes=get_setting("response_state_ttl_minutes") or 60,
)


@router.get("/v1/responses/{response_id}")
async def get_response(response_id: str):
    """获取已存储的响应"""
    response = await _store.get_response(response_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Response {response_id} not found")
    return response


@router.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str):
    """删除存储的响应"""
    deleted = await _store.delete(response_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Response {response_id} not found")
    return {"deleted": True, "id": response_id}
