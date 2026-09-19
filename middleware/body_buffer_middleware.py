"""Body 缓冲中间件：受保护代理路径缓冲请求体 + 413 体积上限，非代理路径直通。

链中位于 ProxyAuth 之后：若上游已把 body 缓冲到 scope["state"]["body_bytes"]，
本中间件直接透传 receive（上游的 buffered_receive 会重放一次 body）；单独装配时
自行读取并做 413 检查与解析（独立 seam）。
"""

from starlette.types import ASGIApp, Receive, Scope, Send

from middleware.common import _is_protected_proxy_path, _read_and_buffer_body


class BodyBufferMiddleware:
    """纯 ASGI 中间件：body 解析 + 413 体积上限 + 非代理路径直通。"""

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
        body_bytes = state.get("body_bytes")
        if body_bytes is not None:
            # 上游中间件（ProxyAuth）已缓冲 body，透传其 buffered receive
            await self.app(scope, receive, send)
            return

        result = await _read_and_buffer_body(scope, state, receive, send)
        if result is None:
            return
        await self.app(scope, result.buffered_receive, send)
