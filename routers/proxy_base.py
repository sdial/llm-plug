import json

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from models.api_types import APIType
from proxy_core import proxy_request
from routers.auth import check_proxy_authorization
from routers.proxy_errors import invalid_request, response_from_proxy_exception, unauthorized


def make_proxy_router(path: str, api_type: APIType, tags: list[str] | None = None) -> APIRouter:
    router = APIRouter(tags=tags or ["代理"])

    @router.post(path)
    async def proxy_handler(request: Request, authorization: str | None = Header(None)):
        if not check_proxy_authorization(authorization):
            return unauthorized()

        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            return invalid_request(f"Invalid JSON: {e}")

        model = body.get("model", "")
        is_stream = body.get("stream", False)
        query_string = str(request.url.query) if request.url.query else None

        try:
            result, _channel = await proxy_request(model, body, api_type, is_stream, query_string=query_string)
        except ValueError as e:
            return invalid_request(str(e))
        except Exception as e:
            return response_from_proxy_exception(e)

        if is_stream:
            return StreamingResponse(
                result,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return result

    return router
