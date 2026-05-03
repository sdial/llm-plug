import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from models.api_types import APIType
from proxy_core import proxy_request
from routers.auth import check_proxy_authorization
from routers.proxy_errors import (
    anthropic_invalid_request,
    anthropic_response_from_exception,
    anthropic_unauthorized,
    invalid_request,
    response_from_proxy_exception,
    unauthorized,
)



async def _closeable_stream(gen: AsyncGenerator):
    """包装流式生成器，确保客户端断开时显式关闭，释放 converter 等资源。"""
    try:
        async for chunk in gen:
            yield chunk
    finally:
        await gen.aclose()


def _pick_error_helpers(api_type: APIType):
    """根据 API 类型选择对应格式的错误响应函数"""
    if api_type == APIType.ANTHROPIC:
        return anthropic_unauthorized, anthropic_invalid_request, anthropic_response_from_exception
    return unauthorized, invalid_request, response_from_proxy_exception


def make_proxy_router(path: str, api_type: APIType, tags: list[str] | None = None) -> APIRouter:
    router = APIRouter(tags=tags or ["代理"])
    err_unauth, err_invalid, err_exception = _pick_error_helpers(api_type)

    @router.post(path)
    async def proxy_handler(request: Request, authorization: str | None = Header(None)):
        if not check_proxy_authorization(authorization, request.state):
            return err_unauth()

        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            return err_invalid(f"Invalid JSON: {e}")

        model = body.get("model", "")
        # 对于 Anthropic API，默认使用流式响应（客户端通常期望流式）
        # 如果客户端明确设置 stream: false，则使用非流式
        if api_type == APIType.ANTHROPIC and "stream" not in body:
            is_stream = True  # Anthropic API 默认流式
        else:
            is_stream = body.get("stream", False)
        query_string = str(request.url.query) if request.url.query else None

        client_headers = dict(request.headers)

        api_key_id = getattr(request.state, 'api_key_id', None)
        tracked_headers = getattr(request.state, 'tracked_headers', None)
        try:
            result, _channel = await proxy_request(
                model, body, api_type, is_stream,
                query_string=query_string, client_headers=client_headers,
                api_key_id=api_key_id, tracked_headers=tracked_headers,
            )
            request.state.selected_channel_name = _channel.name
        except ValueError as e:
            logger.error(f"{path} ValueError: {e}")
            return err_invalid(str(e))
        except Exception as e:
            logger.error(f"{path} {type(e).__name__}: {e}")
            return err_exception(e)

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

        return result

    return router
