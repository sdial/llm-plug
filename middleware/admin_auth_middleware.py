"""Admin 会话中间件：/admin 路径除豁免项外校验会话，未登录返回 401 或重定向 302。"""

from starlette.types import ASGIApp, Receive, Scope, Send

from middleware.common import _extract_cookie, _send_error, _send_redirect


class AdminAuthMiddleware:
    """纯 ASGI 中间件：admin 会话校验（401/302）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path.startswith("/admin"):
            _ADMIN_EXEMPT = ("/admin/login", "/admin/login/")
            _ADMIN_EXEMPT_PREFIXES = ("/admin/auth", "/admin/static/")

            def _is_exempt(p: str) -> bool:
                return p in _ADMIN_EXEMPT or any(p.startswith(px) for px in _ADMIN_EXEMPT_PREFIXES)

            if not _is_exempt(path):
                from admin_auth import get_session_cookie_name, validate_admin_session

                session_cookie = _extract_cookie(scope, get_session_cookie_name())
                if not await validate_admin_session(session_cookie):
                    if path in ("/admin", "/admin/"):
                        await _send_redirect(send, "/admin/login")
                    else:
                        await _send_error(
                            send,
                            401,
                            "Admin login required",
                            "admin_login_required",
                        )
                    return

        await self.app(scope, receive, send)
