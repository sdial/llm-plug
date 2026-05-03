import pytest


def test_config_defaults():
    """验证配置项默认值"""
    from config import _CONFIG_SCHEMA

    assert _CONFIG_SCHEMA["host"]["default"] == "0.0.0.0"
    assert _CONFIG_SCHEMA["port"]["default"] == 55555
    assert _CONFIG_SCHEMA["request_timeout"]["default"] == 300
    assert _CONFIG_SCHEMA["max_body_size"]["default"] == 10485760
    assert _CONFIG_SCHEMA["debug"]["default"] is False
    assert _CONFIG_SCHEMA["log_level"]["default"] == "info"
    assert _CONFIG_SCHEMA["stats_tracked_headers"]["default"] == ""
    assert _CONFIG_SCHEMA["database_url"]["default"] == ""
    assert _CONFIG_SCHEMA["max_fail_count"]["default"] == 5
    assert _CONFIG_SCHEMA["cooldown_seconds"]["default"] == 60


def test_config_requires_restart():
    """验证需重启标记"""
    from config import _CONFIG_SCHEMA

    restart_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("requires_restart")]
    assert "host" in restart_keys
    assert "port" in restart_keys
    assert "debug" in restart_keys
    assert "log_level" in restart_keys
    assert "database_url" in restart_keys
    # 热更新项不在列表中
    assert "request_timeout" not in restart_keys
    assert "max_fail_count" not in restart_keys
    assert "cooldown_seconds" not in restart_keys


def test_config_readonly():
    """验证只读标记"""
    from config import _CONFIG_SCHEMA

    readonly_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("readonly")]
    assert "host" in readonly_keys
    assert "port" in readonly_keys
