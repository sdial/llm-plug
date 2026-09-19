"""共享 helper：错误响应 / 重定向 / 请求日志 / cookie 提取 / 保护路径判定 / 路径归一化。

逻辑与旧 CombinedMiddleware 中对应私有方法等价（搬家而非改写）。
"""

import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import NamedTuple

from loguru import logger
from starlette.types import Message, Receive, Scope, Send

import config

_V1_DEDUP = re.compile(r"^(/v1)+(/.*)")

# 已知的裸端点前缀（按长度倒序，确保最长匹配优先）
_BARE_PREFIXES = (
    "/chat/completions",
    "/anthropic/models",
    "/responses",
    "/messages",
    "/models",
)

# 需要 API Key 认证的代理端点
_PROXY_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
_MODEL_LIST_PATHS = ("/v1/models", "/v1/anthropic/models")
_PROTECTED_RESPONSE_PREFIX = "/v1/responses/"


def normalize_path(path: str) -> str:
    """路径归一化：重复 /v1 去重 + 已知裸端点补 /v1。"""
    m = _V1_DEDUP.match(path)
    if m:
        return "/v1" + m.group(2)
    for prefix in _BARE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return "/v1" + path
    return path


def _is_protected_proxy_path(method: str, path: str) -> bool:
    """判断请求是否需要经过 API Key 认证。"""
    if method == "POST" and path in _PROXY_PATHS:
        return True
    if method == "GET" and path in _MODEL_LIST_PATHS:
        return True
    if path.startswith(_PROTECTED_RESPONSE_PREFIX):
        return method in ("GET", "POST", "DELETE")
    return False


class _BufferedBody(NamedTuple):
    """_read_and_buffer_body 的返回值（含可重放的 buffered_receive）。"""

    ts_start: str
    start: float
    query: str
    headers_dict: dict[str, str]
    model: str
    stream: bool
    buffered_receive: Callable[[], Awaitable[Message]]


async def _read_and_buffer_body(
    scope: Scope,
    state: dict,
    receive: Receive,
    send: Send,
) -> _BufferedBody | None:
    """读取并缓冲受保护代理路径的请求体：413 上限检查 + model/stream 解析 + 写 state。

    调用方已判定为受保护代理路径。body 超限时发送 413（含日志）并返回 None；
    成功返回 _BufferedBody。buffered_receive 恰好重放一次已缓冲 body。
    """
    method = scope["method"]
    path = scope["path"]
    original_path = state.get("original_path", path)

    start = time.time()
    ts_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    query = scope.get("query_string", b"").decode("utf-8", errors="replace")
    headers_dict = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

    content_length = headers_dict.get("content-length")
    if content_length:
        try:
            if int(content_length) > config.MAX_BODY_SIZE:
                await _send_error(send, 413, "Request body too large", path=path)
                _log_request(ts_start, method, path, original_path, query, "", False, "", 413, start)
                return None
        except ValueError:
            pass

    body_parts = []
    more_body = True
    total_size = 0
    while more_body:
        message = await receive()
        chunk = message.get("body", b"")
        body_parts.append(chunk)
        total_size += len(chunk)
        if total_size > config.MAX_BODY_SIZE:
            await _send_error(send, 413, "Request body too large", path=path)
            _log_request(ts_start, method, path, original_path, query, "", False, "", 413, start)
            return None
        more_body = message.get("more_body", False)
    body_bytes = b"".join(body_parts)

    model = ""
    stream = False
    try:
        body = json.loads(body_bytes)
        model = body.get("model", "")
        stream = body.get("stream", False)
    except Exception:
        pass

    state["body_bytes"] = body_bytes
    state["model"] = model
    state["stream"] = stream

    body_received = False

    async def buffered_receive() -> Message:
        nonlocal body_received
        if not body_received:
            body_received = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return await receive()

    return _BufferedBody(ts_start, start, query, headers_dict, model, stream, buffered_receive)


def _extract_cookie(scope: Scope, cookie_name: str) -> str | None:
    """从 ASGI scope 中提取指定 cookie 的值。"""
    for key, value in scope.get("headers", []):
        if key.lower() == b"cookie":
            for part in value.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == cookie_name:
                    return val
    return None


def _log_request(
    ts_start: str,
    method: str,
    path: str,
    original_path: str,
    query: str,
    model: str,
    stream: bool,
    channel: str,
    status: int,
    start: float,
) -> None:
    """写 [REQ] / [RES] 文本日志（与旧 CombinedMiddleware._log_request 等价）。"""
    qs = f"?{query}" if query else ""
    channel_tag = f" channel={channel}" if channel else ""
    original_tag = f" original={original_path}" if original_path != path else ""
    ts_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag = "OK" if status < 400 else "ERR"
    elapsed = time.time() - start
    logger.info(f"[{ts_start}] [REQ]  {method} {path}{qs} model={model} stream={stream}{channel_tag}{original_tag}")
    logger.info(f"[{ts_end}] [RES]  {method} {path}{qs} -> {status} {tag} ({elapsed:.2f}s){original_tag}")


async def _send_error(
    send: Send,
    status: int,
    message: str,
    error_type: str = "auth_error",
    path: str = "",
) -> None:
    """发送统一 JSON 错误响应（/v1/messages 用 Anthropic 错误格式）。"""
    if path == "/v1/messages":
        anthropic_type = {
            "auth_error": "authentication_error",
            "ip_whitelist_error": "permission_error",
        }.get(error_type, "api_error")
        error_body = json.dumps(
            {
                "type": "error",
                "error": {"type": anthropic_type, "message": message},
            }
        ).encode()
    else:
        error_body = json.dumps({"error": {"message": message, "type": error_type}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": error_body,
        }
    )


async def _send_redirect(send: Send, location: str) -> None:
    """发送 302 重定向响应。"""
    await send(
        {
            "type": "http.response.start",
            "status": 302,
            "headers": [[b"location", location.encode("utf-8")]],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b"",
        }
    )
