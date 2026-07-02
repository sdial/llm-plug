# 管理员安全功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为管理后台增加密码修改功能和阶梯式IP封锁机制

**Architecture:** 在现有 `admin_auth.py` 和 `routers/admin.py` 基础上扩展，新增修改密码API和阶梯式IP封锁逻辑，前端在设置页和登录页增加相应UI

**Tech Stack:** Python, FastAPI, HTML, TailwindCSS, htmx

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `admin_auth.py` | 新增 `change_admin_password()` 函数 |
| `routers/admin.py` | 新增API端点，重构IP封锁逻辑为阶梯式 |
| `config.py` | 新增 `admin_max_attempts` 和 `admin_lockout_base_seconds` 配置项 |
| `static/fragments/admin/settings.html` | 新增"安全"导航和安全设置分区 |
| `static/admin-login.html` | 增加忘记密码折叠区域 |
| `tests/test_admin_security.py` | 新增安全功能测试 |

---

## Task 1: 配置项扩展

**Files:**
- Modify: `config.py:27-107`
- Test: `tests/test_admin_security.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_security.py
import pytest
from config import _CONFIG_SCHEMA, _CONFIG_CONSTRAINTS


def test_admin_security_config_schema():
    """验证管理员安全配置项存在于schema中"""
    assert "admin_max_attempts" in _CONFIG_SCHEMA
    assert "admin_lockout_base_seconds" in _CONFIG_SCHEMA
    assert _CONFIG_SCHEMA["admin_max_attempts"]["default"] == 10
    assert _CONFIG_SCHEMA["admin_lockout_base_seconds"]["default"] == 60


def test_admin_security_config_constraints():
    """验证配置约束"""
    assert _CONFIG_CONSTRAINTS["admin_max_attempts"]["min"] == 1
    assert _CONFIG_CONSTRAINTS["admin_max_attempts"]["max"] == 100
    assert _CONFIG_CONSTRAINTS["admin_lockout_base_seconds"]["min"] == 10
    assert _CONFIG_CONSTRAINTS["admin_lockout_base_seconds"]["max"] == 86400
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_admin_security.py::test_admin_security_config_schema -v`
Expected: FAIL with KeyError

- [ ] **Step 3: 实现配置项**

```python
# config.py - 在 _CONFIG_SCHEMA 中添加（约第97行后）
_CONFIG_SCHEMA: dict[str, ConfigSchemaEntry] = {
    # ... 现有配置 ...
    "request_log_raw_retention_days": {
        "type": "int",
        "default": 1,
        "requires_restart": False,
    },
    "admin_max_attempts": {
        "type": "int",
        "default": 10,
        "requires_restart": False,
    },
    "admin_lockout_base_seconds": {
        "type": "int",
        "default": 60,
        "requires_restart": False,
    },
}

# config.py - 在 _CONFIG_CONSTRAINTS 中添加（约第141行后）
_CONFIG_CONSTRAINTS: dict[str, dict] = {
    # ... 现有约束 ...
    "request_log_raw_retention_days": {"min": 0},
    "admin_max_attempts": {"min": 1, "max": 100},
    "admin_lockout_base_seconds": {"min": 10, "max": 86400},
}
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_admin_security.py::test_admin_security_config_schema -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_admin_security.py
git commit -m "feat: add admin security config schema"
```

---

## Task 2: 阶梯式IP封锁逻辑

**Files:**
- Modify: `routers/admin.py:87-198`
- Test: `tests/test_admin_security.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_security.py - 追加
import time
from unittest.mock import patch


def test_lockout_tier_calculation():
    """验证阶梯封锁时间计算"""
    from routers.admin import _get_lockout_seconds

    # 1-10次：60秒
    assert _get_lockout_seconds(1) == 60
    assert _get_lockout_seconds(10) == 60

    # 11-20次：120秒
    assert _get_lockout_seconds(11) == 120
    assert _get_lockout_seconds(20) == 120

    # 21-30次：240秒
    assert _get_lockout_seconds(21) == 240
    assert _get_lockout_seconds(30) == 240

    # 31-40次：600秒
    assert _get_lockout_seconds(31) == 600
    assert _get_lockout_seconds(40) == 600

    # 41-50次：3600秒
    assert _get_lockout_seconds(41) == 3600
    assert _get_lockout_seconds(50) == 3600

    # 51+次：86400秒
    assert _get_lockout_seconds(51) == 86400
    assert _get_lockout_seconds(100) == 86400


def test_lockout_check_blocks_during_lockout():
    """验证封锁期间拒绝请求"""
    from routers.admin import _login_attempts, _check_login_allowed

    # 模拟10次失败
    ip = "192.168.1.1"
    _login_attempts[ip] = [time.monotonic() - 10] * 10  # 10秒前的失败

    # 应该被封锁（还有50秒）
    allowed, retry_after = _check_login_allowed(ip)
    assert allowed is False
    assert retry_after > 0
    assert retry_after <= 60


def test_lockout_check_allows_after_cooldown():
    """验证冷却后允许请求"""
    from routers.admin import _login_attempts, _check_login_allowed

    ip = "192.168.1.2"
    # 模拟10次失败，但都是70秒前（超过60秒封锁）
    _login_attempts[ip] = [time.monotonic() - 70] * 10

    allowed, retry_after = _check_login_allowed(ip)
    assert allowed is True
    assert retry_after == 0


def test_lockout_tier_escalation():
    """验证阶梯递增"""
    from routers.admin import _login_attempts, _check_login_allowed, _record_login_failure

    ip = "192.168.1.3"
    _login_attempts.pop(ip, None)

    # 模拟20次失败（跨越两个阶梯）
    for _ in range(20):
        _record_login_failure(ip)

    # 应该在第二阶梯（120秒封锁）
    allowed, retry_after = _check_login_allowed(ip)
    assert allowed is False
    assert retry_after > 60
    assert retry_after <= 120
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_admin_security.py::test_lockout_tier_calculation -v`
Expected: FAIL with ImportError

- [ ] **Step 3: 实现阶梯封锁逻辑**

```python
# routers/admin.py - 替换现有封锁逻辑（约第87-198行）

_LOGIN_RATE_LIMIT_MAX_FAILURES = 5  # 保留兼容，但不再使用
_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 86400  # 24小时过期

# 阶梯封锁系数（固定）
_LOCKOUT_MULTIPLIERS = [1, 2, 4, 10, 60, 1440]

_login_attempts: dict[str, list[float]] = {}


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
    attempts = _login_attempts.get(ip, [])
    cutoff = now - _LOGIN_RATE_LIMIT_WINDOW_SECONDS
    return [ts for ts in attempts if ts > cutoff]


def _check_login_allowed(ip: str) -> tuple[bool, int]:
    """检查IP是否允许登录，返回 (是否允许, 重试等待秒数)"""
    now = time.monotonic()
    attempts = _cleanup_expired_attempts(ip, now)
    _login_attempts[ip] = attempts

    if not attempts:
        return True, 0

    failure_count = len(attempts)
    lockout_seconds = _get_lockout_seconds(failure_count)
    last_attempt = max(attempts)
    unlock_time = last_attempt + lockout_seconds

    if now < unlock_time:
        remaining = int(unlock_time - now) + 1
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_admin_security.py::test_lockout_tier_calculation tests/test_admin_security.py::test_lockout_check_blocks_during_lockout tests/test_admin_security.py::test_lockout_check_allows_after_cooldown tests/test_admin_security.py::test_lockout_tier_escalation -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add routers/admin.py tests/test_admin_security.py
git commit -m "feat: implement tiered IP lockout mechanism"
```

---

## Task 3: 修改密码后端

**Files:**
- Modify: `admin_auth.py`
- Test: `tests/test_admin_security.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_security.py - 追加
import pytest
from admin_auth import change_admin_password, setup_admin_password, get_admin_auth_state


@pytest.mark.asyncio
async def test_change_password_success(tmp_path, monkeypatch):
    """验证成功修改密码"""
    monkeypatch.setattr("admin_auth._auth_file", lambda: tmp_path / "admin_auth.json")

    # 先设置初始密码
    await setup_admin_password("old_password")

    # 修改密码
    result = await change_admin_password("old_password", "new_password", "new_password")
    assert result is True

    # 验证新密码生效
    state = await get_admin_auth_state()
    from admin_auth import _verify_password
    assert _verify_password("new_password", state["password_hash"]) is True


@pytest.mark.asyncio
async def test_change_password_wrong_old(tmp_path, monkeypatch):
    """验证旧密码错误"""
    monkeypatch.setattr("admin_auth._auth_file", lambda: tmp_path / "admin_auth.json")

    await setup_admin_password("old_password")

    with pytest.raises(ValueError, match="旧密码错误"):
        await change_admin_password("wrong_password", "new_password", "new_password")


@pytest.mark.asyncio
async def test_change_password_mismatch(tmp_path, monkeypatch):
    """验证新密码不一致"""
    monkeypatch.setattr("admin_auth._auth_file", lambda: tmp_path / "admin_auth.json")

    await setup_admin_password("old_password")

    with pytest.raises(ValueError, match="两次输入的新密码不一致"):
        await change_admin_password("old_password", "new_password", "different_password")


@pytest.mark.asyncio
async def test_change_password_too_short(tmp_path, monkeypatch):
    """验证密码过短"""
    monkeypatch.setattr("admin_auth._auth_file", lambda: tmp_path / "admin_auth.json")

    await setup_admin_password("old_password")

    with pytest.raises(ValueError, match="新密码长度不能少于6位"):
        await change_admin_password("old_password", "12345", "12345")


@pytest.mark.asyncio
async def test_change_password_revokes_sessions(tmp_path, monkeypatch):
    """验证修改密码后撤销所有会话"""
    monkeypatch.setattr("admin_auth._auth_file", lambda: tmp_path / "admin_auth.json")

    await setup_admin_password("old_password")

    # 创建一个会话
    from admin_auth import create_admin_session, validate_admin_session
    token = await create_admin_session()
    assert await validate_admin_session(token) is True

    # 修改密码
    await change_admin_password("old_password", "new_password", "new_password")

    # 旧会话应该失效
    assert await validate_admin_session(token) is False
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_admin_security.py::test_change_password_success -v`
Expected: FAIL with ImportError

- [ ] **Step 3: 实现修改密码函数**

```python
# admin_auth.py - 在文件末尾添加

async def change_admin_password(
    old_password: str,
    new_password: str,
    confirm_password: str,
) -> bool:
    """修改管理员密码。

    验证旧密码、检查新密码一致性、检查密码长度。
    成功后撤销所有现有会话，强制重新登录。

    Raises:
        ValueError: 验证失败时抛出
    """
    if not old_password or not old_password.strip():
        raise ValueError("旧密码不能为空")
    if not new_password or not new_password.strip():
        raise ValueError("新密码不能为空")
    if new_password != confirm_password:
        raise ValueError("两次输入的新密码不一致")
    if len(new_password) < 6:
        raise ValueError("新密码长度不能少于6位")

    async with _auth_lock:
        data = _normalize_auth_data(await _read_auth_file())
        password_hash = data.get("password_hash", "")

        if not password_hash:
            raise RuntimeError("管理员密码尚未设置")

        if not _verify_password(old_password, password_hash):
            raise ValueError("旧密码错误")

        # 更新密码哈希
        data["password_hash"] = _hash_password(new_password)
        data["updated_at"] = int(_now())
        # 清空所有撤销会话（因为所有旧会话都将失效）
        data["revoked_sessions"] = {}

        await _write_auth_file(data)

    return True
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_admin_security.py -k "change_password" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add admin_auth.py tests/test_admin_security.py
git commit -m "feat: add change_admin_password function"
```

---

## Task 4: 修改密码API端点

**Files:**
- Modify: `routers/admin.py`
- Test: `tests/test_admin_security.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_security.py - 追加
import pytest
from httpx import AsyncClient
from tests.admin_auth_utils import login_admin


@pytest.mark.asyncio
async def test_change_password_endpoint(e2e_client):
    """测试修改密码API端点"""
    client = e2e_client
    csrf = await login_admin(client, "old_password")

    # 修改密码
    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "old_password",
            "new_password": "new_password",
            "confirm_password": "new_password",
        },
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "密码修改成功"


@pytest.mark.asyncio
async def test_change_password_endpoint_wrong_old(e2e_client):
    """测试修改密码API - 旧密码错误"""
    client = e2e_client
    csrf = await login_admin(client, "old_password")

    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "wrong_password",
            "new_password": "new_password",
            "confirm_password": "new_password",
        },
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 400
    assert "旧密码错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_change_password_endpoint_requires_auth(e2e_client):
    """测试修改密码API - 需要登录"""
    client = e2e_client

    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "old_password",
            "new_password": "new_password",
            "confirm_password": "new_password",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_change_password_endpoint_requires_csrf(e2e_client):
    """测试修改密码API - 需要CSRF"""
    client = e2e_client
    await login_admin(client, "old_password")

    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "old_password",
            "new_password": "new_password",
            "confirm_password": "new_password",
        },
        # 不传CSRF
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_admin_security.py::test_change_password_endpoint -v`
Expected: FAIL with 404

- [ ] **Step 3: 实现API端点**

```python
# routers/admin.py - 在 auth_setup_login 函数后添加

class AdminChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    confirm_password: str


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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_admin_security.py -k "change_password_endpoint" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add routers/admin.py tests/test_admin_security.py
git commit -m "feat: add change-password API endpoint"
```

---

## Task 5: 安全配置API端点

**Files:**
- Modify: `routers/admin.py`
- Test: `tests/test_admin_security.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_security.py - 追加
import pytest


@pytest.mark.asyncio
async def test_security_config_get(e2e_client):
    """测试获取安全配置"""
    client = e2e_client
    csrf = await login_admin(client, "password")

    resp = await client.get(
        "/admin/auth/security-config",
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "admin_max_attempts" in data
    assert "admin_lockout_base_seconds" in data
    assert "lockout_tiers" in data


@pytest.mark.asyncio
async def test_security_config_update(e2e_client):
    """测试更新安全配置"""
    client = e2e_client
    csrf = await login_admin(client, "password")

    resp = await client.put(
        "/admin/auth/security-config",
        json={
            "admin_max_attempts": 5,
            "admin_lockout_base_seconds": 30,
        },
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "安全配置已更新"


@pytest.mark.asyncio
async def test_security_config_validation(e2e_client):
    """测试配置验证"""
    client = e2e_client
    csrf = await login_admin(client, "password")

    # 尝试设置过小的值
    resp = await client.put(
        "/admin/auth/security-config",
        json={
            "admin_max_attempts": 0,  # 最小值为1
        },
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_admin_security.py::test_security_config_get -v`
Expected: FAIL with 404

- [ ] **Step 3: 实现API端点**

```python
# routers/admin.py - 在 change-password 端点后添加

@router.get("/auth/security-config")
async def auth_security_config_get():
    """获取安全配置"""
    from config import get_setting

    max_attempts = get_setting("admin_max_attempts") or 10
    base_seconds = get_setting("admin_lockout_base_seconds") or 60

    # 计算阶梯表用于展示
    multipliers = [1, 2, 4, 10, 60, 1440]
    tiers = []
    for i, m in enumerate(multipliers):
        tier_seconds = base_seconds * m
        start = i * max_attempts + 1
        end = (i + 1) * max_attempts
        tiers.append({
            "range": f"{start}-{end}",
            "seconds": tier_seconds,
            "display": _format_duration(tier_seconds),
        })

    return {
        "admin_max_attempts": max_attempts,
        "admin_lockout_base_seconds": base_seconds,
        "lockout_tiers": tiers,
    }


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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_admin_security.py -k "security_config" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add routers/admin.py tests/test_admin_security.py
git commit -m "feat: add security-config API endpoints"
```

---

## Task 6: 集成阶梯封锁到登录端点

**Files:**
- Modify: `routers/admin.py:284-329`
- Test: `tests/test_admin_security.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_security.py - 追加
import pytest


@pytest.mark.asyncio
async def test_login_rate_limiting_works(e2e_client):
    """测试登录速率限制生效"""
    client = e2e_client

    # 先设置密码
    await client.post("/admin/auth/setup", json={"password": "correct_password"})

    # 连续失败10次
    for i in range(10):
        resp = await client.post(
            "/admin/auth/login",
            json={"password": "wrong_password"},
        )
        if i < 9:
            assert resp.status_code == 401
        else:
            # 第10次应该被封锁
            assert resp.status_code == 429
            assert "请" in resp.json()["detail"]
            assert "秒后再试" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_login_rate_limit_allows_after_cooldown(e2e_client):
    """测试冷却后允许登录"""
    from routers.admin import _login_attempts
    import time

    client = e2e_client
    await client.post("/admin/auth/setup", json={"password": "correct_password"})

    # 模拟10次失败（70秒前）
    ip = "testclient"
    _login_attempts[ip] = [time.monotonic() - 70] * 10

    # 现在应该允许登录
    resp = await client.post(
        "/admin/auth/login",
        json={"password": "correct_password"},
    )
    assert resp.status_code == 200
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_admin_security.py::test_login_rate_limiting_works -v`
Expected: FAIL (可能通过，取决于现有逻辑)

- [ ] **Step 3: 修改登录端点使用新封锁逻辑**

```python
# routers/admin.py - 修改 auth_login 和 auth_setup_login 函数

@router.post("/auth/login")
async def auth_login(body: AdminLoginRequest, request: Request):
    if not await admin_auth.is_admin_password_configured():
        raise HTTPException(status_code=401, detail="管理员密码尚未设置")

    # 使用新的阶梯封锁检查
    ip = _client_ip(request)
    allowed, retry_after = _check_login_allowed(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {retry_after} 秒后再试",
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


@router.post("/auth/setup-login")
async def auth_setup_login(body: AdminLoginRequest, request: Request):
    """原子操作：若管理员密码尚未设置则先初始化，然后验证并登录。"""
    # 使用新的阶梯封锁检查
    ip = _client_ip(request)
    allowed, retry_after = _check_login_allowed(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {retry_after} 秒后再试",
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
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_admin_security.py -k "login_rate_limit" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add routers/admin.py tests/test_admin_security.py
git commit -m "feat: integrate tiered lockout into login endpoints"
```

---

## Task 7: 设置页前端 - 安全设置分区

**Files:**
- Modify: `static/fragments/admin/settings.html`
- Modify: `static/index.html` (添加Tab)

- [ ] **Step 1: 添加安全导航按钮**

```html
<!-- static/fragments/admin/settings.html - 在设置导航列表中添加（约第47行后） -->
<li>
  <button onclick="switchSettingsSection('security')" data-section="security" class="settings-nav-btn w-full flex items-center gap-2.5 px-3 py-2.5 text-sm font-medium rounded-lg transition-colors duration-150">
    <svg class="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    <span>安全</span>
    <span class="settings-dirty-dot hidden w-1.5 h-1.5 rounded-full bg-brand-500 ml-auto flex-shrink-0"></span>
  </button>
</li>
```

- [ ] **Step 2: 添加安全设置分区HTML**

```html
<!-- static/fragments/admin/settings.html - 在数据库分区后、底部操作栏前添加 -->
<div id="settings_security" class="settings-section hidden">
  <div class="mb-5">
    <h2 class="text-base font-semibold text-ink-900">安全设置</h2>
    <p class="text-sm text-ink-600 mt-0.5">管理员登录保护和密码管理。</p>
  </div>

  <!-- 修改密码 -->
  <div class="card p-5">
    <div class="text-sm font-medium text-ink-900 mb-4">修改密码</div>
    <form id="changePasswordForm" class="space-y-4">
      <div>
        <label class="block text-sm text-ink-700 mb-1">当前密码</label>
        <input type="password" id="cp_old_password" required
          class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
      </div>
      <div>
        <label class="block text-sm text-ink-700 mb-1">新密码</label>
        <input type="password" id="cp_new_password" required minlength="6"
          class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
        <p class="text-xs text-ink-400 mt-1">至少6位</p>
      </div>
      <div>
        <label class="block text-sm text-ink-700 mb-1">确认新密码</label>
        <input type="password" id="cp_confirm_password" required
          class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
      </div>
      <div id="cp_message" class="text-sm hidden"></div>
      <button type="submit" class="btn-primary text-sm px-4 py-2 font-medium">修改密码</button>
    </form>
  </div>

  <!-- 登录保护配置 -->
  <div class="card p-5 mt-4">
    <div class="flex items-center gap-2 mb-4">
      <span class="text-sm font-medium text-ink-900">登录保护</span>
      <span class="pill pill-success">热更新</span>
    </div>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-5 mb-5">
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1.5">每阶梯最大尝试次数</label>
        <input type="number" id="set_admin_max_attempts" min="1" max="100" data-section="security"
          class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
        <p class="text-xs text-ink-400 mt-1">每个封锁阶梯允许的失败尝试次数</p>
      </div>
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1.5">基础封锁时间</label>
        <input type="number" id="set_admin_lockout_base_seconds" min="10" max="86400" data-section="security"
          class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
        <p class="text-xs text-ink-400 mt-1">单位：秒，第一阶梯的封锁时长</p>
      </div>
    </div>

    <!-- 阶梯表展示 -->
    <div class="bg-surface-50 rounded-lg border border-surface-200 p-4">
      <div class="text-xs font-medium text-ink-700 mb-2">封锁阶梯表</div>
      <table class="w-full text-xs">
        <thead>
          <tr class="text-ink-500">
            <th class="text-left py-1">失败次数</th>
            <th class="text-left py-1">封锁时间</th>
          </tr>
        </thead>
        <tbody id="lockoutTiersBody" class="text-ink-700">
          <!-- 动态填充 -->
        </tbody>
      </table>
    </div>
  </div>
</div>
```

- [ ] **Step 3: 添加JavaScript函数**

```html
<!-- static/fragments/admin/settings.html - 在script标签中添加 -->

// 修改密码表单提交
document.getElementById('changePasswordForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = document.getElementById('cp_message');
    msg.classList.add('hidden');
    msg.classList.remove('text-rose-600', 'text-green-600');

    const old_password = document.getElementById('cp_old_password').value;
    const new_password = document.getElementById('cp_new_password').value;
    const confirm_password = document.getElementById('cp_confirm_password').value;

    if (new_password !== confirm_password) {
        msg.textContent = '两次输入的新密码不一致';
        msg.classList.add('text-rose-600');
        msg.classList.remove('hidden');
        return;
    }

    try {
        const csrf = await getCsrfToken();
        const resp = await fetch('/admin/auth/change-password', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'x-csrf-token': csrf,
            },
            body: JSON.stringify({ old_password, new_password, confirm_password }),
        });

        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || '修改失败');
        }

        msg.textContent = '密码修改成功，请重新登录';
        msg.classList.add('text-green-600');
        msg.classList.remove('hidden');
        document.getElementById('changePasswordForm').reset();

        // 2秒后跳转到登录页
        setTimeout(() => {
            window.location.href = '/admin/login';
        }, 2000);
    } catch (err) {
        msg.textContent = err.message;
        msg.classList.add('text-rose-600');
        msg.classList.remove('hidden');
    }
});

// 加载安全配置
async function loadSecurityConfig() {
    try {
        const resp = await fetch('/admin/auth/security-config');
        if (!resp.ok) return;
        const data = await resp.json();

        document.getElementById('set_admin_max_attempts').value = data.admin_max_attempts;
        document.getElementById('set_admin_lockout_base_seconds').value = data.admin_lockout_base_seconds;

        // 填充阶梯表
        const tbody = document.getElementById('lockoutTiersBody');
        tbody.innerHTML = '';
        for (const tier of data.lockout_tiers) {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td class="py-1">${tier.range} 次</td><td>${tier.display}</td>`;
            tbody.appendChild(tr);
        }
    } catch (err) {
        console.error('Failed to load security config:', err);
    }
}

// 在 loadSettings() 中调用 loadSecurityConfig()
```

- [ ] **Step 4: 运行服务验证UI**

Run: `uv run python main.py --no-reload`
Expected: 设置页显示"安全"分区，可看到修改密码表单和阶梯配置

- [ ] **Step 5: 提交**

```bash
git add static/fragments/admin/settings.html
git commit -m "feat: add security settings UI with password change form"
```

---

## Task 8: 登录页前端 - 忘记密码提示

**Files:**
- Modify: `static/admin-login.html`

- [ ] **Step 1: 添加忘记密码折叠区域**

```html
<!-- static/admin-login.html - 在form标签后、message标签前添加 -->

<details class="mt-4">
  <summary class="text-sm text-ink-500 cursor-pointer hover:text-ink-700">忘记密码？</summary>
  <div class="mt-3 text-sm text-ink-600 bg-surface-50 rounded-lg p-4 border border-surface-200">
    <p class="font-medium text-ink-800 mb-2">管理员强制重置密码：</p>
    <ol class="list-decimal list-inside space-y-1.5 text-ink-600">
      <li>删除 <code class="text-xs bg-surface-100 px-1.5 py-0.5 rounded font-mono">data/admin_auth.json</code> 文件</li>
      <li>重启服务</li>
      <li>重新访问此页面设置新密码</li>
    </ol>
    <p class="mt-3 text-xs text-ink-400">注意：此操作会清除所有已登录的会话。</p>
  </div>
</details>
```

- [ ] **Step 2: 运行服务验证UI**

Run: `uv run python main.py --no-reload`
Expected: 登录页底部显示"忘记密码？"折叠区域，点击可展开

- [ ] **Step 3: 提交**

```bash
git add static/admin-login.html
git commit -m "feat: add forgot password hint to login page"
```

---

## Task 9: 清理旧速率限制代码

**Files:**
- Modify: `routers/admin.py`

- [ ] **Step 1: 删除旧速率限制函数**

删除以下旧函数和变量：
- `_LOGIN_RATE_LIMIT_MAX_FAILURES`
- `_LOGIN_RATE_LIMIT_WINDOW_SECONDS`
- `_login_rate_limit_state`
- `_cleanup_stale_login_rate_limits()`
- `_is_login_rate_limited()`
- `_record_login_failure()` (旧版)
- `_clear_login_failures()` (旧版)

- [ ] **Step 2: 更新 `_login_rate_limit_key` 函数**

```python
def _login_rate_limit_key(request: Request) -> str:
    """返回IP地址作为速率限制key"""
    return _client_ip(request)
```

- [ ] **Step 3: 运行测试验证无回归**

Run: `uv run pytest tests/test_admin_security.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add routers/admin.py
git commit -m "refactor: remove old rate limiting code"
```

---

## Task 10: 端到端测试

**Files:**
- Test: `tests/test_admin_security.py`

- [ ] **Step 1: 写端到端测试**

```python
# tests/test_admin_security.py - 追加
import pytest
import time


@pytest.mark.asyncio
async def test_full_security_flow(e2e_client):
    """完整的安全功能端到端测试"""
    client = e2e_client

    # 1. 初始设置密码
    resp = await client.post("/admin/auth/setup-login", json={"password": "initial_pass"})
    assert resp.status_code == 200
    csrf = resp.json()["csrf_token"]

    # 2. 修改密码
    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "initial_pass",
            "new_password": "new_pass_123",
            "confirm_password": "new_pass_123",
        },
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 200

    # 3. 用旧密码登录应该失败
    resp = await client.post("/admin/auth/login", json={"password": "initial_pass"})
    assert resp.status_code == 401

    # 4. 用新密码登录应该成功
    resp = await client.post("/admin/auth/login", json={"password": "new_pass_123"})
    assert resp.status_code == 200

    # 5. 连续失败触发封锁
    for _ in range(10):
        await client.post("/admin/auth/login", json={"password": "wrong"})

    resp = await client.post("/admin/auth/login", json={"password": "new_pass_123"})
    assert resp.status_code == 429

    # 6. 验证安全配置可读
    resp = await client.get("/admin/auth/security-config")
    assert resp.status_code == 200
    assert resp.json()["admin_max_attempts"] == 10
```

- [ ] **Step 2: 运行端到端测试**

Run: `uv run pytest tests/test_admin_security.py::test_full_security_flow -v`
Expected: PASS

- [ ] **Step 3: 运行全部测试**

Run: `uv run pytest tests/test_admin_security.py -v`
Expected: ALL PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_admin_security.py
git commit -m "test: add end-to-end security flow test"
```

---

## Task 11: Lint 和 Type Check

- [ ] **Step 1: 运行 lint**

Run: `uv run ruff check .`
Expected: No errors

- [ ] **Step 2: 运行格式化**

Run: `uv run ruff format . --check`
Expected: All files formatted

- [ ] **Step 3: 修复任何问题**

如果有错误，修复后重新运行测试。

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "chore: lint and format"
```
