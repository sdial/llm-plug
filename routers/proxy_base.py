import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Header, Request
import httpx
from fastapi.responses import Response, StreamingResponse
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


async def _prime_stream(gen: AsyncGenerator) -> AsyncGenerator:
    """在发送 HTTP 响应头前取到首个 chunk，便于透传首包前错误。"""
    try:
        first_chunk = await anext(gen)
    except StopAsyncIteration:
        return gen

    async def _replay():
        try:
            yield first_chunk
            async for chunk in gen:
                yield chunk
        finally:
            await gen.aclose()

    return _replay()


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
        is_stream = body.get("stream", False)
        query_string = str(request.url.query) if request.url.query else None

        client_headers = dict(request.headers)

        api_key_id = getattr(request.state, 'api_key_id', None)
        try:
            result, _channel = await proxy_request(
                model, body, api_type, is_stream,
                query_string=query_string, client_headers=client_headers,
                api_key_id=api_key_id,
            )
            request.state.selected_channel_name = _channel.name
            if is_stream:
                result = await _prime_stream(result)
        except ValueError as e:
            logger.error(f"{path} ValueError: {e}")
            return err_invalid(str(e))
        except httpx.HTTPStatusError as e:
            logger.error(f"{path} upstream HTTP {e.response.status_code}: {e}")
            return _response_from_upstream_http_error(e)
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


def _response_from_upstream_http_error(exc: httpx.HTTPStatusError) -> Response:
    """透传上游 HTTP 错误状态码和响应体。"""
    content = exc.response.content
    media_type = exc.response.headers.get("content-type")
    return Response(
        content=content,
        status_code=exc.response.status_code,
        media_type=media_type,
    )
