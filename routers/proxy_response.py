import json
import os
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from config import DATA_DIR, get_setting
from models.api_types import APIType
from proxy_core import proxy_request
from routers.auth import check_proxy_authorization
from routers.proxy_errors import invalid_request, response_from_proxy_exception, unauthorized
from state_store import FileStore

router = APIRouter(tags=["代理"])

# 初始化 FileStore
_session_dir = os.path.join(DATA_DIR, "responses_session")
_store = FileStore(
    data_dir=_session_dir,
    max_entries=get_setting("response_state_max_entries") or 1000,
    ttl_minutes=get_setting("response_state_ttl_minutes") or 60,
)


def _chat_completion_to_response(data: dict, response_id: str) -> dict:
    """将 Chat Completions 格式转换为 Responses API 格式"""
    choices = data.get("choices", [])
    text = ""
    if choices:
        msg = choices[0].get("message", {})
        text = msg.get("content", "") or ""

    usage = data.get("usage", {})

    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": data.get("model", ""),
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": f"msg_{response_id}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


@router.post("/v1/responses")
async def post_response(request: Request, authorization: str | None = None):
    """处理 Responses API 请求"""
    if not check_proxy_authorization(authorization, request.state):
        return unauthorized()

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        return invalid_request(f"Invalid JSON: {e}")

    model = body.get("model", "")
    is_stream = body.get("stream", False)
    query_string = str(request.url.query) if request.url.query else None
    client_headers = dict(request.headers)
    api_key_id = getattr(request.state, 'api_key_id', None)

    try:
        result, _channel = await proxy_request(
            model, body, APIType.OPENAI_RESPONSE, is_stream,
            query_string=query_string, client_headers=client_headers,
            api_key_id=api_key_id,
        )
        request.state.selected_channel_name = _channel.name
    except ValueError as e:
        logger.error(f"/v1/responses ValueError: {e}")
        return invalid_request(str(e))
    except Exception as e:
        logger.error(f"/v1/responses {type(e).__name__}: {e}")
        return response_from_proxy_exception(e)

    response_id = _store.generate_response_id()
    response_data = _chat_completion_to_response(result, response_id)

    conversation = {
        "messages": [
            {"role": "user", "content": body.get("input", "")},
            {"role": "assistant", "content": response_data["output"][0]["content"][0]["text"] if response_data["output"] else ""},
        ],
        "reasoning_history": [],
        "tool_calls": [],
    }

    await _store.put(response_id, conversation, response_data)

    return JSONResponse(content=response_data)


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
