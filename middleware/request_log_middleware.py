"""请求/响应日志中间件：受保护代理路径记录 [REQ] / [RES] 文本日志。

链序最内层（紧邻应用）。认证失败路径（ProxyAuth / BodyBuffer 返回 401/403/413）
由各失败中间件调用 common._log_request 直接记录；本中间件负责请求成功进入下游后
在 finally 记录（含下游异常时按 500 记录），与旧 CombinedMiddleware 行为一致。
"""

import time
from datetime import datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from middleware.common import _is_protected_proxy_path, _log_request


class RequestLogMiddleware:
    """纯 ASGI 中间件：请求/响应日志（含认证失败路径也要记录）。"""

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

        state = scope.get("state", {})
        original_path = state.get("original_path", path)
        start = time.time()
        ts_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        query = scope.get("query_string", b"").decode("utf-8", errors="replace")
        model = state.get("model", "")
        stream = state.get("stream", False)

        # Track response status
        response_status: int | None = None
        original_send = send

        async def tracking_send(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 200)
            await original_send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            raise
        finally:
            channel = scope.get("state", {}).get("selected_channel_name", "")
            _log_request(
                ts_start,
                method,
                path,
                original_path,
                query,
                model,
                stream,
                channel,
                response_status or 500,
                start,
            )
