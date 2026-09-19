"""管理员鉴权域路由：会话状态、登录/登出、密码设置与修改、安全配置、登录速率限制。"""

import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import admin_auth

from . import _login_attempts
from .common import AdminAuthRoute, _client_ip

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)

# 阶梯封锁系数（固定）
_LOCKOUT_MULTIPLIERS = [1, 2, 4, 10, 60, 1440]


class AdminPasswordSetup(BaseModel):
    password: str


class AdminLoginRequest(BaseModel):
    password: str


class AdminChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    confirm_password: str


def _get_lockout_seconds(failure_count: int) -> int:
    """根据失败次数计算封锁时间（秒）"""
    from config import get_setting

    max_attempts = get_setting("admin_max_attempts") or 10
    base_seconds = get_setting("admin_lockout_base_seconds") or 60

    if failure_count <= 0:
        return 0

    # 计算所在阶梯（从0开始）
    tier = min((failure_count - 1) // max_attempts, len(_LOCKOUT_MULTIPLIERS) - 1)
    return base_seconds * _LOCKOUT_MULTIPLIERS[tier]


def _cleanup_expired_attempts(ip: str, now: float) -> list[float]:
    """清理过期的失败记录"""
    from . import _LOGIN_RATE_LIMIT_WINDOW_SECONDS

    attempts = _login_attempts.get(ip, [])
    cutoff = now - _LOGIN_RATE_LIMIT_WINDOW_SECONDS
    return [ts for ts in attempts if ts > cutoff]


def _check_login_allowed(ip: str) -> tuple[bool, int]:
    """检查IP是否允许登录，返回 (是否允许, 重试等待秒数)"""
    from config import get_setting

    now = time.monotonic()
    attempts = _cleanup_expired_attempts(ip, now)
    _login_attempts[ip] = attempts

    if not attempts:
        return True, 0

    failure_count = len(attempts)
    max_attempts = get_setting("admin_max_attempts") or 10
    if failure_count < max_attempts:
        return True, 0

    lockout_seconds = _get_lockout_seconds(failure_count)
    last_attempt = max(attempts)
    unlock_time = last_attempt + lockout_seconds

    if now < unlock_time:
        remaining = min(int(unlock_time - now) + 1, lockout_seconds)
        return False, remaining

    return True, 0


def _record_login_failure(ip: str) -> None:
    """记录一次登录失败"""
    now = time.monotonic()
    attempts = _cleanup_expired_attempts(ip, now)
    attempts.append(now)
    _login_attempts[ip] = attempts


def _clear_login_failures(ip: str) -> None:
    """清除IP的失败记录"""
    _login_attempts.pop(ip, None)


def _format_duration(seconds: int) -> str:
    """格式化时长为中文"""
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        return f"{seconds // 60}分钟"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes == 0:
            return f"{hours}小时"
        return f"{hours}小时{minutes}分钟"
    else:
        days = seconds // 86400
        return f"{days}天"


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
    ip = _client_ip(request)
    allowed, retry_after = _check_login_allowed(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="登录失败次数过多，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )
    if not await admin_auth.verify_admin_password(body.password):
        _record_login_failure(ip)
        raise HTTPException(status_code=401, detail="密码错误")
    _clear_login_failures(ip)
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


@router.post("/auth/setup-login")
async def auth_setup_login(body: AdminLoginRequest, request: Request):
    """原子操作：若管理员密码尚未设置则先初始化，然后验证并登录。

    消除 setup → login 两步流程的竞态条件和重复网络请求。
    """
    ip = _client_ip(request)
    allowed, retry_after = _check_login_allowed(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="登录失败次数过多，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        token = await admin_auth.setup_and_login(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if token is None:
        _record_login_failure(ip)
        raise HTTPException(status_code=401, detail="密码错误")
    _clear_login_failures(ip)
    csrf_token = await admin_auth.create_admin_csrf_token(token)
    response = JSONResponse({"message": "登录成功", "csrf_token": csrf_token})
    response.headers["Set-Cookie"] = admin_auth.build_session_cookie(token)
    return response


@router.post("/auth/change-password")
async def auth_change_password(body: AdminChangePasswordRequest, request: Request):
    """修改管理员密码，需登录+CSRF"""
    try:
        await admin_auth.change_admin_password(
            body.old_password,
            body.new_password,
            body.confirm_password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "密码修改成功"}


@router.get("/auth/security-config")
async def auth_security_config_get():
    """获取安全配置"""
    from config import get_setting

    max_attempts = get_setting("admin_max_attempts") or 10
    base_seconds = get_setting("admin_lockout_base_seconds") or 60

    multipliers = _LOCKOUT_MULTIPLIERS
    tiers = []
    for i, m in enumerate(multipliers):
        tier_seconds = base_seconds * m
        start = i * max_attempts + 1
        end = (i + 1) * max_attempts
        tiers.append(
            {
                "range": f"{start}-{end}",
                "seconds": tier_seconds,
                "display": _format_duration(tier_seconds),
            }
        )

    return {
        "admin_max_attempts": max_attempts,
        "admin_lockout_base_seconds": base_seconds,
        "lockout_tiers": tiers,
    }


@router.put("/auth/security-config")
async def auth_security_config_update(body: dict, request: Request):
    """更新安全配置"""
    from config import update_settings

    allowed_keys = {"admin_max_attempts", "admin_lockout_base_seconds"}
    updates = {k: v for k, v in body.items() if k in allowed_keys}

    if not updates:
        raise HTTPException(status_code=400, detail="无有效配置项")

    try:
        await update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"message": "安全配置已更新"}
