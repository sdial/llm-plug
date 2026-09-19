"""IP 白名单中间件：对所有 HTTP 请求做 IP 白名单检查，拒绝时返回 403。"""

import os

from starlette.types import ASGIApp, Receive, Scope, Send

import config
import whitelist as _whitelist
from middleware.common import _send_error, normalize_path

# 白名单缓存（与 main.py 的装配等价；测试中可直接替换为指向临时文件的实例）
_whitelist_cache = _whitelist.WhitelistCache(os.path.join(config.DATA_DIR, "whitelist.csv"))


class WhitelistMiddleware:
    """纯 ASGI 中间件：IP 白名单（403）。

    链序最外层：负责路径归一化（scope["path"]）并写入 scope["state"] 的
    client_ip / original_path，供下游中间件与路由使用。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        original_path = scope["path"]
        path = normalize_path(original_path)
        scope["path"] = path

        client_ip = (scope.get("client") or ("", 0))[0]
        scope.setdefault("state", {})
        scope["state"]["client_ip"] = client_ip
        scope["state"]["original_path"] = original_path

        rules = _whitelist_cache.get_rules()
        allowed, reason = _whitelist.check_request(rules, path, scope["method"], client_ip)
        if not allowed:
            await _send_error(send, 403, reason, "ip_whitelist_error", path=path)
            return
        await self.app(scope, receive, send)
