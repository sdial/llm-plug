import json
import os
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
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


async def _closeable_stream(gen: AsyncGenerator):
    """包装流式生成器，确保客户端断开时显式关闭。"""
    try:
        async for chunk in gen:
            yield chunk
    finally:
        await gen.aclose()


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

    logger.debug(f"[RESPONSES REQUEST] model={model} stream={is_stream} api_key_id={api_key_id}")

    try:
        result, _channel = await proxy_request(
            model, body, APIType.OPENAI_RESPONSE, is_stream,
            query_string=query_string, client_headers=client_headers,
            api_key_id=api_key_id,
        )
        request.state.selected_channel_name = _channel.name
        logger.debug(f"[RESPONSES SUCCESS] model={model} channel={_channel.name}")
    except ValueError as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses ValueError: {e}")
        return invalid_request(str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses upstream HTTP {e.response.status_code}: {e}")
        return _response_from_upstream_http_error(e)
    except Exception as e:
        logger.error(f"[RESPONSES ERROR] /v1/responses {type(e).__name__}: {e}")
        return response_from_proxy_exception(e)

    if is_stream:
        return StreamingResponse(
            _closeable_stream(result),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 同格式透传：上游已经是 Responses 格式，直接返回
    if _channel.api_type == APIType.OPENAI_RESPONSE:
        return JSONResponse(content=result)

    # 跨格式转换：Chat Completions → Responses
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


def _response_from_upstream_http_error(exc: httpx.HTTPStatusError) -> Response:
    """透传上游 HTTP 错误状态码和响应体。"""
    content = exc.response.content
    media_type = exc.response.headers.get("content-type")
    return Response(
        content=content,
        status_code=exc.response.status_code,
        media_type=media_type,
    )
