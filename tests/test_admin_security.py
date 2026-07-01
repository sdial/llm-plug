import json
import time

import pytest
import pytest_asyncio
import httpx

import config
import storage
from admin_auth import change_admin_password, setup_admin_password, get_admin_auth_state
from config import _CONFIG_SCHEMA, _CONFIG_CONSTRAINTS
from main import app
from tests.admin_auth_utils import login_admin

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"
    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None
    yield


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


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


@pytest.mark.asyncio
async def test_change_password_endpoint(client):
    """测试修改密码API端点"""
    await login_admin(client, "old_password")

    # 修改密码
    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "old_password",
            "new_password": "new_password",
            "confirm_password": "new_password",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "密码修改成功"


@pytest.mark.asyncio
async def test_change_password_endpoint_wrong_old(client):
    """测试修改密码API - 旧密码错误"""
    await login_admin(client, "old_password")

    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "wrong_password",
            "new_password": "new_password",
            "confirm_password": "new_password",
        },
    )
    assert resp.status_code == 400
    assert "旧密码错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_change_password_endpoint_requires_auth(client):
    """测试修改密码API - 需要登录"""
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
async def test_change_password_endpoint_requires_csrf(client):
    """测试修改密码API - 需要CSRF"""
    await login_admin(client, "old_password")

    # 移除CSRF头
    client.headers.pop("X-CSRF-Token", None)

    resp = await client.post(
        "/admin/auth/change-password",
        json={
            "old_password": "old_password",
            "new_password": "new_password",
            "confirm_password": "new_password",
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_security_config_get(client):
    """测试获取安全配置"""
    await login_admin(client, "password")

    resp = await client.get(
        "/admin/auth/security-config",
        headers={"X-CSRF-Token": client.headers.get("X-CSRF-Token", "")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "admin_max_attempts" in data
    assert "admin_lockout_base_seconds" in data
    assert "lockout_tiers" in data


@pytest.mark.asyncio
async def test_security_config_update(client):
    """测试更新安全配置"""
    await login_admin(client, "password")

    resp = await client.put(
        "/admin/auth/security-config",
        json={
            "admin_max_attempts": 5,
            "admin_lockout_base_seconds": 30,
        },
        headers={"X-CSRF-Token": client.headers.get("X-CSRF-Token", "")},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "安全配置已更新"


@pytest.mark.asyncio
async def test_security_config_validation(client):
    """测试配置验证"""
    await login_admin(client, "password")

    resp = await client.put(
        "/admin/auth/security-config",
        json={
            "admin_max_attempts": 0,
        },
        headers={"X-CSRF-Token": client.headers.get("X-CSRF-Token", "")},
    )
    assert resp.status_code == 400
