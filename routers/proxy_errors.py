"""OpenAI / Anthropic 风格错误体 + 正确 HTTP 状态码"""

import httpx
from fastapi.responses import JSONResponse


def safe_httpx_response_content(response: httpx.Response) -> bytes | None:
    """Best-effort response body read for httpx errors, including closed streams."""
    try:
        return response.content
    except httpx.ResponseNotRead:
        try:
            return response.read()
        except (httpx.StreamClosed, httpx.StreamConsumed):
            return None


def safe_httpx_response_text(response: httpx.Response) -> str:
    content = safe_httpx_response_content(response)
    if content is None:
        return ""
    return content.decode(response.encoding or "utf-8", errors="replace")


def upstream_http_error_message(exc: httpx.HTTPStatusError) -> str:
    body = safe_httpx_response_text(exc.response)
    if len(body) > 800:
        body = body[:800] + "..."
    if body:
        return f"上游 HTTP {exc.response.status_code}: {body}"
    return f"上游 HTTP {exc.response.status_code}: {exc}"


# ── Anthropic 格式错误 ──


def anthropic_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def anthropic_unauthorized() -> JSONResponse:
    return anthropic_error(401, "authentication_error", "无效的 API Key")


def anthropic_invalid_request(message: str) -> JSONResponse:
    return anthropic_error(400, "invalid_request_error", message)


def anthropic_bad_gateway(message: str) -> JSONResponse:
    return anthropic_error(502, "api_error", message)


def anthropic_gateway_timeout() -> JSONResponse:
    return anthropic_error(504, "api_error", "上游请求超时")


def anthropic_response_from_exception(exc: BaseException) -> JSONResponse:
    if isinstance(exc, httpx.HTTPStatusError):
        return anthropic_bad_gateway(upstream_http_error_message(exc))
    if isinstance(exc, httpx.TimeoutException):
        return anthropic_gateway_timeout()
    if isinstance(exc, httpx.RequestError):
        return anthropic_bad_gateway(f"上游网络错误: {exc}")
    return anthropic_bad_gateway(str(exc))


# ── OpenAI 格式错误 ──


def unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": "无效的 API Key",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )


def invalid_request(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"message": message, "type": "invalid_request_error"}},
    )


def bad_gateway(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"error": {"message": message, "type": "api_error"}},
    )


def gateway_timeout(message: str = "上游请求超时") -> JSONResponse:
    return JSONResponse(
        status_code=504,
        content={
            "error": {
                "message": message,
                "type": "api_error",
                "code": "timeout",
            }
        },
    )


def response_from_proxy_exception(exc: BaseException) -> JSONResponse:
    """将代理链路上的 httpx 等异常映射为对客户端一致的错误 JSON。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return bad_gateway(upstream_http_error_message(exc))
    if isinstance(exc, httpx.TimeoutException):
        return gateway_timeout()
    if isinstance(exc, httpx.RequestError):
        return bad_gateway(f"上游网络错误: {exc}")
    return bad_gateway(str(exc))
