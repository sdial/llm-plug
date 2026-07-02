"""代理 API 的鉴权（与 /admin 管理接口分离）"""


def check_proxy_authorization(authorization: str | None, request_state=None) -> bool:
    """确认代理请求已经通过 CombinedMiddleware 的鉴权阶段。"""
    return bool(getattr(request_state, "proxy_auth_checked", False))
