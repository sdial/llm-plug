from collections.abc import Callable
import asyncio
import ipaddress
import secrets
import socket
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

import admin_auth
import request_logs
from client import get_upstream_headers, remove_channel_client
from models.api_key import ApiKey, ApiKeyCreate, ApiKeyUpdate
from models.channel import Channel, ChannelCreate, ChannelUpdate
from models.model_group import LBConfig, ModelGroup, ModelGroupCreate, ModelGroupUpdate
from proxy_core import _get_upstream_url
from url_builder import build_models_url
import whitelist as _whitelist_mod
from stats import (
    aggregate_daily_stats,
    get_daily_stats,
    get_daily_stats_from_requests,
    get_overall_stats,
    get_today_stats,
    refresh_missing_daily_stats,
    refresh_stats,
)
from stats import (
    list_requests as stats_list_requests,
)
from storage import (
    add_model_group,
    atomic_update_api_keys,
    atomic_update_data,
    delete_model_group,
    get_lb_config,
    invalidate_keys_cache,
    load_api_keys,
    load_data,
    load_model_groups,
    save_api_keys,
    save_data,
    save_lb_config,
    update_model_group,
)


class FetchModelsRequest(BaseModel):
    base_url: str
    models_url: str | None = None
    api_key: str | None = None
    api_type: str


class AdminPasswordSetup(BaseModel):
    password: str


class AdminLoginRequest(BaseModel):
    password: str

request_log_list_requests = request_logs.list_requests
request_log_get_request_field = request_logs.get_request_field

_CSRF_HEADER_NAME = "x-csrf-token"
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_AUTH_PUBLIC_PATHS = {
    "/admin/auth/status",
    "/admin/auth/setup",
    "/admin/auth/login",
}
_LOGIN_RATE_LIMIT_MAX_FAILURES = 5
_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60
_login_rate_limit_state: dict[tuple[str, str], list[float]] = {}

LOGS_DIR = Path(__file__).parent.parent / "logs"
STATIC_DIR = Path(__file__).parent.parent / "static"
ADMIN_FRAGMENT_DIR = STATIC_DIR / "fragments" / "admin"
DATA_DIR = Path(__file__).parent.parent / "data"
WHITELIST_PATH = DATA_DIR / "whitelist.csv"
_ALLOWED_LOG_SUFFIX = ".jsonl"


def _is_public_address(address: str) -> bool:
    return ipaddress.ip_address(address).is_global


async def _validate_outbound_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="上游地址必须是 http 或 https URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="上游地址不允许包含认证信息")

    host = parsed.hostname.rstrip(".").lower()
    try:
        if not _is_public_address(host):
            raise HTTPException(status_code=400, detail="不允许访问内网或本机地址")
        addresses = {host}
    except ValueError:
        try:
            addrinfo = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                parsed.port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise HTTPException(status_code=400, detail="上游地址无法解析") from exc
        addresses = {item[4][0] for item in addrinfo}

    if any(not _is_public_address(address) for address in addresses):
        raise HTTPException(status_code=400, detail="不允许访问内网或本机地址")


def _validate_log_filename(filename: str) -> None:
    if (
        not filename.endswith(_ALLOWED_LOG_SUFFIX)
        or filename != Path(filename).name
        or any(sep in filename for sep in ("/", "\\"))
    ):
        raise HTTPException(status_code=400, detail="日志文件名不合法")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_rate_limit_key(request: Request) -> tuple[str, str]:
    return (str(admin_auth._auth_file()), _client_ip(request))


def _is_login_rate_limited(request: Request) -> bool:
    now = time.monotonic()
    key = _login_rate_limit_key(request)
    failures = [
        ts for ts in _login_rate_limit_state.get(key, [])
        if now - ts < _LOGIN_RATE_LIMIT_WINDOW_SECONDS
    ]
    _login_rate_limit_state[key] = failures
    return len(failures) >= _LOGIN_RATE_LIMIT_MAX_FAILURES


def _record_login_failure(request: Request) -> None:
    now = time.monotonic()
    key = _login_rate_limit_key(request)
    failures = [
        ts for ts in _login_rate_limit_state.get(key, [])
        if now - ts < _LOGIN_RATE_LIMIT_WINDOW_SECONDS
    ]
    failures.append(now)
    _login_rate_limit_state[key] = failures


def _clear_login_failures(request: Request) -> None:
    _login_rate_limit_state.pop(_login_rate_limit_key(request), None)


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


router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


@router.get("/auth/status")
async def auth_status():
    return {
        "configured": await admin_auth.is_admin_password_configured(),
    }


@router.get("/auth/csrf")
async def auth_csrf(request: Request):
    cookie_token = request.cookies.get(admin_auth.get_session_cookie_name())
    csrf_token = await admin_auth.create_admin_csrf_token(cookie_token)
    if csrf_token is None:
        raise HTTPException(status_code=401, detail="Admin login required")
    return {"csrf_token": csrf_token}


@router.post("/auth/setup")
async def auth_setup(body: AdminPasswordSetup):
    if await admin_auth.is_admin_password_configured():
        raise HTTPException(status_code=409, detail="管理员密码已设置")
    try:
        await admin_auth.setup_admin_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "管理员密码已设置"}


@router.post("/auth/login")
async def auth_login(body: AdminLoginRequest, request: Request):
    if not await admin_auth.is_admin_password_configured():
        raise HTTPException(status_code=401, detail="管理员密码尚未设置")
    if _is_login_rate_limited(request):
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")
    if not await admin_auth.verify_admin_password(body.password):
        _record_login_failure(request)
        raise HTTPException(status_code=401, detail="密码错误")
    _clear_login_failures(request)
    token = await admin_auth.create_admin_session()
    csrf_token = await admin_auth.create_admin_csrf_token(token)
    response = JSONResponse({"message": "登录成功", "csrf_token": csrf_token})
    response.headers["Set-Cookie"] = admin_auth.build_session_cookie(token)
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request):
    cookie_token = request.cookies.get(admin_auth.get_session_cookie_name())
    await admin_auth.clear_admin_session(cookie_token)
    response = JSONResponse({"message": "已退出登录"})
    response.headers["Set-Cookie"] = admin_auth.build_cleared_session_cookie()
    return response


async def _get_channels() -> list[Channel]:
    data = await load_data()
    return [Channel(**ch) for ch in data.get("channels", [])]


async def _save_channels(channels: list[Channel]):
    data = await load_data()
    data["channels"] = [ch.model_dump() for ch in channels]
    await save_data(data)


@router.get("/channels")
async def list_channels():
    """获取所有渠道（API Key 脱敏）"""
    channels = await _get_channels()
    result = []
    for ch in channels:
        d = ch.model_dump()
        if d.get("api_key"):
            key = d["api_key"]
            d["api_key"] = key[:4] + "***" if len(key) > 4 else "***"
        result.append(d)
    return result


@router.get("/ui/{section}")
async def admin_ui_fragment(section: str):
    """返回管理页局部片段，供 htmx 局部刷新使用"""
    fragment_map = {
        "channels": "channels.html",
        "apikeys": "apikeys.html",
        "stats": "stats.html",
        "requests": "requests.html",
        "settings": "settings.html",
        "whitelist": "whitelist.html",
        "lb": "model-groups.html",
    }
    filename = fragment_map.get(section)
    if not filename:
        raise HTTPException(status_code=404, detail="片段不存在")
    fragment_path = ADMIN_FRAGMENT_DIR / filename
    if not fragment_path.exists():
        raise HTTPException(status_code=404, detail="片段文件不存在")
    return HTMLResponse(fragment_path.read_text(encoding="utf-8"))


@router.post("/channels", response_model=Channel)
async def create_channel(body: ChannelCreate):
    """添加渠道"""
    channel = Channel(**body.model_dump())

    def _mutate(data: dict):
        data.setdefault("channels", []).append(channel.model_dump())
        return data

    await atomic_update_data(_mutate)
    return channel


@router.put("/channels/{channel_id}", response_model=Channel)
async def update_channel(channel_id: str, body: ChannelUpdate):
    """更新渠道"""
    update_data = body.model_dump(exclude_unset=True)
    state: dict = {}

    def _mutate(data: dict):
        channels_raw = data.get("channels", [])
        for i, ch_dict in enumerate(channels_raw):
            if ch_dict.get("id") == channel_id:
                old = Channel(**ch_dict)
                updated = Channel(**{**ch_dict, **update_data})
                channels_raw[i] = updated.model_dump()
                state["old"] = old
                state["updated"] = updated
                data["channels"] = channels_raw
                return data
        raise HTTPException(status_code=404, detail="渠道不存在")

    await atomic_update_data(_mutate)
    await remove_channel_client(state["old"])
    return state["updated"]


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    """删除渠道"""
    state: dict = {}

    def _mutate(data: dict):
        channels_raw = data.get("channels", [])
        removed = next((ch for ch in channels_raw if ch.get("id") == channel_id), None)
        if removed is None:
            raise HTTPException(status_code=404, detail="渠道不存在")
        data["channels"] = [ch for ch in channels_raw if ch.get("id") != channel_id]
        state["removed"] = Channel(**removed)
        return data

    await atomic_update_data(_mutate)
    await remove_channel_client(state["removed"])
    return {"message": "删除成功"}


@router.patch("/channels/{channel_id}/toggle", response_model=Channel)
async def toggle_channel(channel_id: str):
    """启用/禁用渠道"""
    state: dict = {}

    def _mutate(data: dict):
        channels_raw = data.get("channels", [])
        for i, ch_dict in enumerate(channels_raw):
            if ch_dict.get("id") == channel_id:
                old = Channel(**ch_dict)
                updated = old.model_copy(update={"enabled": not old.enabled})
                channels_raw[i] = updated.model_dump()
                state["old"] = old
                state["updated"] = updated
                data["channels"] = channels_raw
                return data
        raise HTTPException(status_code=404, detail="渠道不存在")

    await atomic_update_data(_mutate)
    await remove_channel_client(state["old"])
    return state["updated"]


# ============ API Keys CRUD ============


async def _get_api_keys() -> list[ApiKey]:
    data = await load_api_keys()
    return [ApiKey(**k) for k in data.get("api_keys", [])]


async def _save_api_keys(keys: list[ApiKey]):
    await save_api_keys({"api_keys": [k.model_dump() for k in keys]})


async def _attach_api_key_names(result: dict) -> dict:
    keys = await _get_api_keys()
    name_by_id = {key.id: key.name for key in keys}
    for item in result.get("items", []):
        api_key_id = item.get("api_key_id")
        item["api_key_name"] = name_by_id.get(api_key_id) if api_key_id else None
    return result


@router.get("/api-keys")
async def list_api_keys():
    """获取所有 API Key（Key 脱敏），统计数据从 PG 聚合"""
    import stats as _stats

    keys = await _get_api_keys()
    key_stats = await _stats.get_api_key_stats()
    result = []
    for k in keys:
        d = k.model_dump()
        raw = d.get("key", "")
        d["key"] = raw[:8] + "***" if len(raw) > 8 else "***"
        lookup = k.name or k.id
        s = key_stats.get(lookup, {})
        d["request_count"] = s.get("request_count", 0)
        d["total_input_tokens"] = s.get("total_input_tokens", 0)
        d["total_output_tokens"] = s.get("total_output_tokens", 0)
        result.append(d)
    return result


@router.post("/api-keys", response_model=ApiKey)
async def create_api_key(body: ApiKeyCreate):
    """添加 API Key"""
    data = body.model_dump(exclude_none=True)
    key = ApiKey(**data)

    def _mutate(d: dict):
        d.setdefault("api_keys", []).append(key.model_dump())
        return d

    await atomic_update_api_keys(_mutate)
    await invalidate_keys_cache()
    return key


@router.put("/api-keys/{key_id}", response_model=ApiKey)
async def update_api_key(key_id: str, body: ApiKeyUpdate):
    """更新 API Key"""
    update_data = body.model_dump(exclude_unset=True)
    state: dict = {}

    def _mutate(d: dict):
        keys_raw = d.get("api_keys", [])
        for i, k_dict in enumerate(keys_raw):
            if k_dict.get("id") == key_id:
                old = ApiKey(**k_dict)
                updated = old.model_copy(update=update_data)
                keys_raw[i] = updated.model_dump()
                state["updated"] = updated
                d["api_keys"] = keys_raw
                return d
        raise HTTPException(status_code=404, detail="API Key 不存在")

    await atomic_update_api_keys(_mutate)
    await invalidate_keys_cache()
    return state["updated"]


@router.delete("/api-keys/{key_id}")
async def delete_api_key(key_id: str):
    """删除 API Key"""

    def _mutate(d: dict):
        keys_raw = d.get("api_keys", [])
        new_keys = [k for k in keys_raw if k.get("id") != key_id]
        if len(new_keys) == len(keys_raw):
            raise HTTPException(status_code=404, detail="API Key 不存在")
        d["api_keys"] = new_keys
        return d

    await atomic_update_api_keys(_mutate)
    await invalidate_keys_cache()
    return {"message": "删除成功"}


@router.get("/api-keys/{key_id}/key")
async def get_api_key_value(key_id: str):
    """获取 API Key 的完整值（用于复制）"""
    keys = await _get_api_keys()
    for k in keys:
        if k.id == key_id:
            return {"key": k.key}
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.patch("/api-keys/{key_id}/regenerate", response_model=ApiKey)
async def regenerate_api_key(key_id: str):
    """重新生成 API Key"""
    state: dict = {}

    def _mutate(d: dict):
        keys_raw = d.get("api_keys", [])
        for i, k_dict in enumerate(keys_raw):
            if k_dict.get("id") == key_id:
                old = ApiKey(**k_dict)
                new_key_value = f"sk-{secrets.token_hex(24)}"
                updated = old.model_copy(update={"key": new_key_value})
                keys_raw[i] = updated.model_dump()
                state["updated"] = updated
                d["api_keys"] = keys_raw
                return d
        raise HTTPException(status_code=404, detail="API Key 不存在")

    await atomic_update_api_keys(_mutate)
    await invalidate_keys_cache()
    return state["updated"]


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, model: Annotated[str | None, Query()] = None):
    """测试渠道连通性：发送最简prompt，检查返回"""
    channels = await _get_channels()
    channel = next((ch for ch in channels if ch.id == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")

    if not channel.models:
        return {"success": False, "message": "渠道无可用模型", "latency_ms": None}

    if model:
        if model not in channel.models:
            return {"success": False, "message": f"模型 '{model}' 不在此渠道的模型列表中", "latency_ms": None}
        test_model = model
    else:
        test_model = channel.models[0]
    api_type = channel.api_type.value

    url = _get_upstream_url(channel)

    if api_type == "openai-chat-completions":
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    elif api_type == "openai-response":
        payload = {
            "model": test_model,
            "input": "Hi",
            "max_output_tokens": 5,
        }
    elif api_type == "anthropic":
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
        # 如果是 thinking 模型，添加 thinking 参数
        if "thinking" in test_model.lower():
            payload["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    else:
        return {"success": False, "message": f"不支持的API类型: {api_type}", "latency_ms": None}

    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    if channel.socks5_proxy:
        test_client = httpx.AsyncClient(
            proxy=channel.socks5_proxy,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    else:
        test_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    start = time.monotonic()
    try:
        resp = await test_client.post(url, json=payload, headers=headers)
        latency_ms = round((time.monotonic() - start) * 1000)
        resp.raise_for_status()
        data = resp.json()

        if api_type == "openai-chat-completions":
            choices = data.get("choices", [])
            ok = bool(choices) and choices[0].get("message", {}).get("content") is not None
            reply = choices[0]["message"]["content"][:100] if ok else str(data)[:200]
        elif api_type == "openai-response":
            output = data.get("output", [])
            ok = bool(output)
            reply = str(output[0])[:100] if ok else str(data)[:200]
        elif api_type == "anthropic":
            content = data.get("content", [])
            ok = bool(content)
            # 处理 thinking 模式：找到第一个 text 类型的内容
            text_reply = None
            thinking_reply = None
            for part in content:
                if part.get("type") == "text":
                    text_reply = part.get("text", "")
                    break
                elif part.get("type") == "thinking":
                    thinking_reply = part.get("thinking", "")
            # 优先使用 text 内容，如果没有则使用 thinking 内容
            reply = (text_reply or thinking_reply or "")[:100] if ok else str(data)[:200]
        else:
            ok = True
            reply = str(data)[:200]

        return {
            "success": ok,
            "message": "测试通过" if ok else "返回数据格式异常",
            "model": test_model,
            "latency_ms": latency_ms,
            "reply": reply,
        }
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "success": False,
            "message": f"请求失败: {e!s}",
            "model": test_model,
            "latency_ms": latency_ms,
            "reply": None,
        }
    finally:
        await test_client.aclose()


@router.post("/channels/fetch-models")
async def fetch_models(body: FetchModelsRequest):
    """从上游 API 获取模型列表（代理请求，避免浏览器跨域）"""
    headers = {"Content-Type": "application/json"}
    if body.api_key:
        if body.api_type == "anthropic":
            headers["x-api-key"] = body.api_key
        else:
            headers["Authorization"] = f"Bearer {body.api_key}"

    models_url = build_models_url(body.base_url, body.models_url)
    await _validate_outbound_url(models_url)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(models_url, headers=headers, follow_redirects=False)
            if resp.status_code != 200:
                return {"error": f"上游返回 {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
            models = [m.get("id", m.get("name", "")) for m in data.get("data", data.get("models", []))]
            return {"models": sorted(set(filter(None, models)))}
    except httpx.TimeoutException:
        return {"error": "请求上游超时"}
    except Exception:
        return {"error": "请求失败"}


# ============ Logs API ============


@router.get("/logs")
async def list_logs():
    """列出所有日志文件"""
    if not LOGS_DIR.exists():
        return []
    files = sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size} for f in files]


@router.get("/logs/{filename}")
async def get_log(filename: str):
    """获取日志文件内容"""
    _validate_log_filename(filename)
    file_path = (LOGS_DIR / filename).resolve()
    if not file_path.is_relative_to(LOGS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="禁止访问")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, media_type="application/jsonl")


# ============ Stats API ============


@router.get("/stats")
async def get_stats(days: Annotated[int, Query(ge=1)] = 7):
    """获取统计数据"""
    overall = await get_overall_stats(days=days)
    raw_daily = await get_daily_stats(days=days)
    fallback_used = not bool(raw_daily)
    if fallback_used:
        raw_daily = await get_daily_stats_from_requests(days=days)
    daily_by_date: dict[str, dict] = {}
    for row in raw_daily:
        d = str(row["date"])
        if d not in daily_by_date:
            daily_by_date[d] = {
                "date": d,
                "total_requests": 0,
                "success_count": 0,
                "fail_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_latency_ms": 0,
                "total_lag_ms": 0,
                "latency_count": 0,
            }
        rec = daily_by_date[d]
        rec["total_requests"] += row["request_count"] or 0
        rec["success_count"] += row["success_count"] or 0
        rec["fail_count"] += row["fail_count"] or 0
        rec["total_input_tokens"] += row["input_tokens"] or 0
        rec["total_output_tokens"] += row["output_tokens"] or 0
        if row.get("avg_latency_ms") is not None:
            rec["total_latency_ms"] += row["avg_latency_ms"] * (row["request_count"] or 1)
            rec["latency_count"] += row["request_count"] or 1
        if row.get("avg_lag_ms") is not None:
            rec["total_lag_ms"] += row["avg_lag_ms"] * (row["request_count"] or 1)
    daily = []
    for rec in daily_by_date.values():
        avg_latency = round(rec.pop("total_latency_ms") / rec["latency_count"]) if rec["latency_count"] else 0
        avg_lag = round(rec.pop("total_lag_ms") / rec["latency_count"]) if rec["latency_count"] else 0
        rec.pop("latency_count")
        rec["avg_latency_ms"] = avg_latency
        rec["avg_lag_ms"] = avg_lag
        daily.append(rec)
    daily.sort(key=lambda r: r["date"])

    return {
        "overall": overall,
        "daily": daily,
        "_debug": {
            "server_now": datetime.now(timezone.utc).isoformat(),
            "query_days": days,
            "raw_daily_count": len(raw_daily),
            "fallback_used": fallback_used,
        },
    }


@router.get("/stats/today")
async def get_stats_today():
    """获取今天（东8区0点至今）的实时统计数据"""
    data = await get_today_stats()
    return {
        "overall": data["overall"],
        "daily": data["daily"],
        "_debug": {
            "server_now": datetime.now(timezone.utc).isoformat(),
            "mode": "today_realtime",
        },
    }


@router.post("/stats/refresh/daily")
async def refresh_daily_stats_endpoint():
    """补全缺失的日聚合统计（不含当天）"""
    result = await refresh_missing_daily_stats()
    msg = f"已刷新 {result['count']} 天的日聚合统计"
    if result.get("debug"):
        msg += f" | 服务器日期: {result['debug'].get('today', 'N/A')}"
        msg += f" | requests日期: {', '.join(result['debug'].get('request_dates', []))}"
        msg += f" | 缺失日期: {', '.join(result['debug'].get('missing_dates', []))}"
    return {"message": msg, **result}


@router.post("/stats/refresh")
async def refresh_stats_endpoint():
    """补全缺失历史聚合 + 强制刷新近3天日聚合"""
    result = await refresh_stats()
    return result


@router.post("/stats/aggregate/daily")
async def trigger_daily_aggregation(
    start_date: date, end_date: date,
):
    result = await aggregate_daily_stats(start_date, end_date)
    return {"message": f"已更新 {result['updated_rows']} 条日聚合记录", **result}


@router.get("/requests")
async def list_requests_endpoint(
    source: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    success: Annotated[bool | None, Query()] = None,
    api_key_id: Annotated[str | None, Query()] = None,
    client_ip: Annotated[str | None, Query()] = None,
    is_stream: Annotated[bool | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
):
    """查询请求记录（支持分页和过滤）"""
    if source == "stats":
        result = await stats_list_requests(
            model=model,
            channel=channel,
            start=start,
            end=end,
            success=success,
            api_key_id=api_key_id,
            client_ip=client_ip,
            is_stream=is_stream,
            page=page,
            page_size=page_size,
        )
        result["source"] = "stats"
        return await _attach_api_key_names(result)
    if source not in (None, "request_logs"):
        raise HTTPException(status_code=400, detail=f"不支持的请求记录来源: {source}")

    result = await request_log_list_requests(
        model=model,
        channel=channel,
        start=start,
        end=end,
        success=success,
        api_key_id=api_key_id,
        client_ip=client_ip,
        is_stream=is_stream,
        page=page,
        page_size=page_size,
    )
    if result.get("available") is False:
        raise HTTPException(status_code=503, detail=result.get("error") or "请求记录库不可用")
    return await _attach_api_key_names(result)


_FIELD_PATH_MAP = {
    "request-headers": "request_headers",
    "request-body": "request_body",
    "response-headers": "response_headers",
    "response-body": "response_body",
}


@router.get("/requests/{request_id}/{field_name}")
async def get_request_field_endpoint(request_id: int, field_name: str):
    """获取单个请求的单个 JSONB 字段（请求/返回的 Header 或 Body）"""
    field = _FIELD_PATH_MAP.get(field_name)
    if field is None:
        raise HTTPException(status_code=400, detail=f"不支持的字段: {field_name}")
    result = await request_log_get_request_field(request_id, field)
    if result is None:
        raise HTTPException(status_code=404, detail="请求记录不存在")
    return result


@router.post("/request-logs/cleanup")
async def cleanup_request_logs_endpoint():
    """手动触发请求记录 TTL 清理（按 settings 中的保留天数执行）"""
    return await request_logs.cleanup_old_records()


# ============ 模型组 CRUD ============


@router.get("/model-groups")
async def list_model_groups():
    """获取所有模型组"""
    return await load_model_groups()


@router.post("/model-groups", response_model=ModelGroup)
async def create_model_group(body: ModelGroupCreate):
    """创建模型组"""
    groups = await load_model_groups()
    if any(g.name == body.name for g in groups):
        raise HTTPException(status_code=400, detail="模型组名称已存在")
    group = ModelGroup(**body.model_dump())
    return await add_model_group(group)


@router.put("/model-groups/{group_id}", response_model=ModelGroup)
async def update_model_group_endpoint(group_id: str, body: ModelGroupUpdate):
    """更新模型组"""
    groups = await load_model_groups()
    for g in groups:
        if g.id == group_id:
            update_data = body.model_dump(exclude_unset=True)
            if "name" in update_data and any(other.id != group_id and other.name == update_data["name"] for other in groups):
                    raise HTTPException(status_code=400, detail="模型组名称已存在")
            updated = await update_model_group(group_id, update_data)
            if updated is None:
                break
            return updated
    raise HTTPException(status_code=404, detail="模型组不存在")


@router.delete("/model-groups/{group_id}")
async def delete_model_group_endpoint(group_id: str):
    """删除模型组"""
    deleted = await delete_model_group(group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="模型组不存在")
    return {"message": "删除成功"}


@router.patch("/model-groups/{group_id}/toggle", response_model=ModelGroup)
async def toggle_model_group(group_id: str):
    """启用/禁用模型组"""
    groups = await load_model_groups()
    for g in groups:
        if g.id == group_id:
            updated = await update_model_group(group_id, {"enabled": not g.enabled})
            if updated is None:
                break
            return updated
    raise HTTPException(status_code=404, detail="模型组不存在")


# ============ 负载均衡配置（兼容接口） ============


@router.get("/lb-config", response_model=LBConfig)
async def get_lb_config_endpoint():
    """获取负载均衡全局配置"""
    return await get_lb_config()


@router.put("/lb-config", response_model=LBConfig)
async def update_lb_config_endpoint(body: LBConfig):
    """更新负载均衡全局配置"""
    await save_lb_config(body)
    return body


# ============ 全局设置 ============


@router.get("/settings")
async def get_settings_endpoint():
    """获取所有配置项"""
    import config as _config
    settings = _config.get_settings()
    # max_body_size 转换为 MB 单位
    if settings.get("max_body_size"):
        settings["max_body_size_mb"] = settings["max_body_size"] // (1024 * 1024)
    else:
        settings["max_body_size_mb"] = _config._CONFIG_SCHEMA["max_body_size"]["default"] // (1024 * 1024)
    # max_log_body_size 转换为 KB 单位（0 表示不限制）
    raw = settings.get("max_log_body_size")
    if raw is None:
        raw = _config._CONFIG_SCHEMA["max_log_body_size"]["default"]
    settings["max_log_body_size_kb"] = raw // 1024
    return settings


@router.put("/settings")
async def update_settings_endpoint(body: dict):
    """批量更新配置"""
    import config as _config
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是对象")
    unknown = [k for k in body.keys() if k not in _config._CONFIG_SCHEMA]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知配置项: {unknown}",
        )
    try:
        result = await _config.update_settings(body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reload_result = await request_logs.reload_backend()
    result["request_log_backend"] = reload_result
    if not reload_result.get("available"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "请求记录库配置已保存，但新 backend 初始化失败，已保留旧 backend",
                "settings": result,
                "request_log_backend": reload_result,
            },
        )
    return result


@router.post("/restart")
async def restart_server(body: dict):
    """触发服务重启（Docker restart 策略自动拉起）"""
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="需要 confirm=true 确认重启")
    from loguru import logger as _logger
    import asyncio

    async def _shutdown_after_response() -> None:
        await asyncio.sleep(0.1)
        raise SystemExit(0)

    _logger.info("配置变更触发重启")
    asyncio.create_task(_shutdown_after_response())
    return {"message": "服务正在重启"}


# ============ IP 白名单 ============


@router.get("/whitelist")
async def get_whitelist(request: Request):
    """获取白名单 CSV 原始文本及有效规则数"""
    client_ip = request.client.host if request.client else ""
    if not WHITELIST_PATH.exists():
        return {"content": "", "rule_count": 0, "client_ip": client_ip}
    content = WHITELIST_PATH.read_text(encoding="utf-8")
    rules = _whitelist_mod.load_rules(str(WHITELIST_PATH))
    return {"content": content, "rule_count": len(rules), "client_ip": client_ip}


@router.put("/whitelist")
async def update_whitelist(body: dict):
    """校验并保存白名单 CSV，热重载自动生效"""
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content 必须是字符串")
    valid, error, rules = _whitelist_mod.validate_rules_text(content)
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WHITELIST_PATH.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(WHITELIST_PATH)
    return {"message": f"已保存 {len(rules)} 条规则", "rule_count": len(rules)}
