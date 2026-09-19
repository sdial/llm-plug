"""代理鉴权中间件：对受保护代理路径做 API Key 鉴权（401/403 + allowed_models）。

allowed_models 校验需要请求体里的 model，故本中间件须读取并缓冲 body（含 413 上限
检查），并把 body_bytes 写入 scope["state"] —— 与旧 CombinedMiddleware 的顺序一致
（先缓冲再鉴权）。
"""

import asyncio

from starlette.types import ASGIApp, Receive, Scope, Send

from middleware.common import (
    _is_protected_proxy_path,
    _log_request,
    _read_and_buffer_body,
    _send_error,
)
from storage import load_api_keys, register_api_keys_save_callback

_api_key_index: dict[str, dict] | None = None
_api_key_index_lock = asyncio.Lock()


def _invalidate_api_key_index() -> None:
    global _api_key_index
    _api_key_index = None


register_api_keys_save_callback(_invalidate_api_key_index)


async def _get_api_key_index() -> dict[str, dict]:
    global _api_key_index
    if _api_key_index is not None:
        return _api_key_index

    async with _api_key_index_lock:
        if _api_key_index is not None:
            return _api_key_index
        keys_data = await load_api_keys()
        _api_key_index = {key.get("key") or "": key for key in keys_data.get("api_keys", []) if key.get("key")}
        return _api_key_index


class ProxyAuthMiddleware:
    """纯 ASGI 中间件：代理鉴权（401/403 + allowed_models）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]
        if not _is_protected_proxy_path(method, path):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        client_ip = state.get("client_ip", (scope.get("client") or ("", 0))[0])
        state["client_ip"] = client_ip
        original_path = state.get("original_path", path)

        result = await _read_and_buffer_body(scope, state, receive, send)
        if result is None:
            return
        ts_start = result.ts_start
        start = result.start
        query = result.query
        headers_dict = result.headers_dict
        model = result.model
        stream = result.stream
        buffered_receive = result.buffered_receive

        api_key_index = await _get_api_key_index()

        if api_key_index:
            # 支持两种认证方式：Authorization: Bearer xxx 或 x-api-key: xxx
            auth_header = headers_dict.get("authorization", "")
            x_api_key = headers_dict.get("x-api-key", "")

            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer ") :]
            elif x_api_key:
                token = x_api_key
            else:
                await _send_error(send, 401, "Missing or invalid Authorization header", path=path)
                _log_request(ts_start, method, path, original_path, query, model, stream, "", 401, start)
                return

            matched_key = api_key_index.get(token)

            if matched_key is None:
                await _send_error(send, 401, "Invalid API key", path=path)
                _log_request(ts_start, method, path, original_path, query, model, stream, "", 401, start)
                return

            api_key_id = matched_key.get("name") or matched_key.get("id")
            state["api_key_id"] = api_key_id

            allowed_models = matched_key.get("allowed_models", [])
            response_lifecycle_path = path.startswith("/v1/responses/") and method in {"GET", "DELETE", "POST"}
            if allowed_models and (not model and response_lifecycle_path):
                await _send_error(
                    send,
                    403,
                    "Response lifecycle endpoints require an unrestricted API key",
                    path=path,
                )
                _log_request(ts_start, method, path, original_path, query, model, stream, "", 403, start)
                return
            if allowed_models and model and model not in allowed_models:
                await _send_error(
                    send,
                    403,
                    f"Model '{model}' is not allowed for this API key",
                    path=path,
                )
                _log_request(ts_start, method, path, original_path, query, model, stream, "", 403, start)
                return

            state["auth_result"] = {
                "api_key_id": api_key_id,
                "allowed_models": allowed_models,
            }

        state["proxy_auth_checked"] = True

        # 使用 _read_and_buffer_body 返回的可重放 receive（body 恰好重放一次）
        await self.app(scope, buffered_receive, send)
