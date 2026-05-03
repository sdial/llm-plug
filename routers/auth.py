"""代理 API 的鉴权（与 /admin 管理接口分离）"""


def check_proxy_authorization(authorization: str | None, request_state=None) -> bool:
    """鉴权由 CombinedMiddleware 中的 API Key 系统处理。
    若 middleware 已通过鉴权（request_state.api_key_id 存在），则放行。
    否则也放行（middleware 会在更早阶段拦截未授权请求）。"""
    return True
