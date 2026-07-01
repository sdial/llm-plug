import time

import pytest

from admin_auth import change_admin_password, setup_admin_password, get_admin_auth_state
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
