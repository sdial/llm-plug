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
