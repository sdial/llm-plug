from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from models.api_types import APIType
from proxy_core import proxy_request
from routers.proxy_chat import _check_auth

router = APIRouter(tags=["代理"])


@router.post("/v1/messages")
async def anthropic_messages(request: Request, authorization: str | None = Header(None)):
    if not _check_auth(authorization):
        return {"error": {"message": "无效的API Key", "type": "auth_error"}}

    body = await request.json()
    model = body.get("model", "")
    is_stream = body.get("stream", False)

    try:
        result, _channel = await proxy_request(model, body, APIType.ANTHROPIC, is_stream)
    except ValueError as e:
        return {"error": {"message": str(e), "type": "invalid_request_error"}}
    except Exception as e:
        return {"error": {"message": str(e), "type": "api_error"}}

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
