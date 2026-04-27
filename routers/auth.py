"""代理 API 的鉴权（与 /admin 管理接口分离）"""

from config import PROXY_API_KEY


def check_proxy_authorization(authorization: str | None) -> bool:
    """若配置了 PROXY_API_KEY，则要求 Authorization Bearer 与之匹配。"""
    if not PROXY_API_KEY:
        return True
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return parts[1].strip() == PROXY_API_KEY
