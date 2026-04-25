from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from models.api_types import APIType
from proxy_core import proxy_request
from routers.auth import check_proxy_authorization
from routers.proxy_errors import invalid_request, response_from_proxy_exception, unauthorized

router = APIRouter(tags=["代理"])


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None)):
    if not check_proxy_authorization(authorization):
        return unauthorized()

    body = await request.json()
    model = body.get("model", "")
    is_stream = body.get("stream", False)

    try:
        result, _channel = await proxy_request(model, body, APIType.OPENAI_CHAT, is_stream)
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
