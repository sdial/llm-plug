import json
from collections.abc import AsyncGenerator
from typing import Annotated

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
    safe_httpx_response_content,
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
    async def proxy_handler(request: Request, authorization: Annotated[str | None, Header()] = None):
        if not check_proxy_authorization(authorization, request.state):
            return err_unauth()

        try:
            body_bytes = getattr(request.state, "body_bytes", None)
            body = json.loads(body_bytes) if body_bytes else await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return err_invalid(f"Invalid JSON: {e}")

        model = body.get("model", "")
        is_stream = body.get("stream", False)
        query_string = str(request.url.query) if request.url.query else None

        client_headers = dict(request.headers)

        api_key_id = getattr(request.state, 'api_key_id', None)
        client_ip = request.client.host if request.client else None
        try:
            result, _channel = await proxy_request(
                model, body, api_type, is_stream,
                query_string=query_string, client_headers=client_headers,
                api_key_id=api_key_id,
                client_ip=client_ip,
            )
            request.state.selected_channel_name = _channel.name
        except ValueError as e:
            logger.error(f"{path} ValueError: {e}")
            return err_invalid(str(e))
        except httpx.HTTPStatusError as e:
            logger.error(f"{path} upstream HTTP {e.response.status_code}: {e}")
            return _response_from_upstream_http_error(e, api_type)
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


def _response_from_upstream_http_error(exc: httpx.HTTPStatusError, api_type: APIType) -> Response:
    """透传上游 HTTP 错误状态码和响应体。"""
    content = safe_httpx_response_content(exc.response)
    if content is None:
        if api_type == APIType.ANTHROPIC:
            return JSONResponse(
                status_code=exc.response.status_code,
                content={
                    "type": "error",
                    "error": {"type": "api_error", "message": f"上游 HTTP {exc.response.status_code}: {exc}"},
                },
            )
        return JSONResponse(
            status_code=exc.response.status_code,
            content={"error": {"message": f"上游 HTTP {exc.response.status_code}: {exc}", "type": "api_error"}},
        )
    media_type = exc.response.headers.get("content-type")
    return Response(
        content=content,
        status_code=exc.response.status_code,
        media_type=media_type,
    )
