"""代理 API 的鉴权（与 /admin 管理接口分离）"""

from config import PROXY_API_KEY


def check_proxy_authorization(authorization: str | None) -> bool:
    """若配置了 PROXY_API_KEY，则要求 Authorization Bearer 与之匹配。"""
    if PROXY_API_KEY and (
        not authorization or authorization.replace("Bearer ", "") != PROXY_API_KEY
    ):
        return False
    return True
