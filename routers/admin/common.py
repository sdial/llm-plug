"""管理后台共享 helper：会话路由类、CSRF 校验、URL/日志名校验、渠道/Key 读取与装饰。"""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

import admin_auth
from channel_catalog import catalog
from models.api_key import ApiKey
from models.channel import Channel
from request_logs import REQUEST_SOURCES
from storage import load_api_keys

# 本文件位于 routers/admin/ 包内，比原 routers/admin.py 深一级：需要多一次 parent
_APP_ROOT = Path(__file__).parent.parent.parent
LOGS_DIR = _APP_ROOT / "logs"
STATIC_DIR = _APP_ROOT / "static"
ADMIN_FRAGMENT_DIR = STATIC_DIR / "fragments" / "admin"
DATA_DIR = _APP_ROOT / "data"
WHITELIST_PATH = DATA_DIR / "whitelist.csv"
_ALLOWED_LOG_SUFFIX = ".jsonl"

_CSRF_HEADER_NAME = "x-csrf-token"
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_AUTH_PUBLIC_PATHS = {
    "/admin/auth/status",
    "/admin/auth/setup",
    "/admin/auth/login",
    "/admin/auth/setup-login",
}


def _parsed_request_sources(raw: str | tuple[str, ...] | list[str] | None) -> tuple[str, ...] | None:
    """归一 request_source 查询参数：重复键(tuple/list)与逗号串两种多选形式统一拆分去空。

    Why 入口即校验而非放任非法值进存储层：SQL IN 对未知值静默命中 0 行，
    排障者会把「参数打错」误读为「该来源没流量」（ADR-0009 User Story 6）。
    """
    if raw is None:
        return None
    parts = [raw] if isinstance(raw, str) else list(raw)
    tokens = [token.strip() for part in parts for token in part.split(",")]
    tokens = [token for token in tokens if token]
    if not tokens:
        return None
    invalid_sources = sorted(set(tokens).difference(REQUEST_SOURCES))
    if invalid_sources:
        raise HTTPException(
            status_code=400,
            detail=f"非法的请求来源取值: {', '.join(invalid_sources)}（合法取值: {', '.join(REQUEST_SOURCES)}）",
        )
    # Why dict.fromkeys 去重保序：逗号串里重复出现同一来源不影响 IN 查询，但让下游收到的形态确定
    return tuple(dict.fromkeys(tokens))


def _validate_outbound_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="上游地址必须是 http 或 https URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="上游地址不允许包含认证信息")


async def _validate_channel_outbound_urls(base_url: str, endpoint_url: str | None = None, models_url: str | None = None) -> None:
    """校验渠道潜在出站 URL（base_url / endpoint_url / models_url）的格式。"""
    for url in (base_url, endpoint_url, models_url):
        if url and url.strip():
            _validate_outbound_url(url)


def _validate_log_filename(filename: str) -> None:
    if not filename.endswith(_ALLOWED_LOG_SUFFIX) or filename != Path(filename).name or any(sep in filename for sep in ("/", "\\")):
        raise HTTPException(status_code=400, detail="日志文件名不合法")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _csrf_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": "CSRF token required",
                "type": "csrf_error",
            },
        },
    )


def _requires_csrf(request: Request) -> bool:
    if request.method.upper() in _CSRF_SAFE_METHODS:
        return False
    if request.url.path in _AUTH_PUBLIC_PATHS:
        return False
    return request.url.path.startswith("/admin")


async def _validate_csrf_for_request(request: Request, session_token: str | None) -> bool:
    csrf_token = request.headers.get(_CSRF_HEADER_NAME)
    return await admin_auth.validate_admin_csrf_token(session_token, csrf_token)


class AdminAuthRoute(APIRoute):
    """给管理端点增加路由级会话校验，避免只依赖 main.py 中间件。"""

    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def admin_auth_route_handler(request: Request):
            if request.url.path in _AUTH_PUBLIC_PATHS:
                return await original_route_handler(request)

            cookie_token = request.cookies.get(admin_auth.get_session_cookie_name())
            if not await admin_auth.validate_admin_session(cookie_token):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "message": "Admin login required",
                            "type": "admin_login_required",
                        },
                    },
                )
            if _requires_csrf(request) and not await _validate_csrf_for_request(request, cookie_token):
                return _csrf_error_response()
            return await original_route_handler(request)

        return admin_auth_route_handler


async def _get_channels() -> list[Channel]:
    return list((await catalog.snapshot()).channels)


async def _get_api_keys() -> list[ApiKey]:
    data = await load_api_keys()
    return [ApiKey(**k) for k in data.get("api_keys", [])]


async def _attach_api_key_names(result: dict) -> dict:
    keys = await _get_api_keys()
    name_by_id = {key.id: key.name for key in keys}
    for item in result.get("items", []):
        api_key_id = item.get("api_key_id")
        item["api_key_name"] = name_by_id.get(api_key_id) if api_key_id else None
    return result


async def _attach_channel_api_types(result: dict) -> dict:
    channels = (await catalog.snapshot()).channels

    def static_api_type(channel: Channel) -> str:
        endpoint = next((candidate for candidate in channel.endpoints if candidate.enabled), channel.endpoints[0])
        return endpoint.api_type.value

    api_type_by_id = {channel.id: static_api_type(channel) for channel in channels}
    api_type_by_name = {channel.name: static_api_type(channel) for channel in channels}
    for item in result.get("items", []):
        # 行值来自该次尝试实际服务的格式（executor 的 source_type），优先于渠道静态配置；
        # 存量旧行（api_type 为 None）才回退到渠道静态推断
        item["api_type"] = item.get("api_type") or (api_type_by_id.get(item.get("channel_id")) or api_type_by_name.get(item.get("channel_name")))
    return result


async def _decorate_request_items(result: dict) -> dict:
    result = await _attach_api_key_names(result)
    return await _attach_channel_api_types(result)
