from pathlib import Path

import pytest


@pytest.fixture
def tmp_settings_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    return str(settings_path)


def test_init_settings_from_file(tmp_settings_file):
    """从 settings.json 加载配置"""
    import json

    data = {"request_timeout": 600, "max_fail_count": 10, "cooldown_seconds": 60}
    with open(tmp_settings_file, "w") as f:
        json.dump(data, f)
    import config

    original = config._SETTINGS_FILE
    try:
        config._SETTINGS_FILE = tmp_settings_file
        config._settings = {}
        config._init_settings_sync()
        assert config._settings["request_timeout"] == 600
        assert config._settings["max_fail_count"] == 10
        assert config._settings["cooldown_seconds"] == 60
    finally:
        config._SETTINGS_FILE = original


def test_init_settings_ignores_environment_fallback(tmp_settings_file, monkeypatch):
    """settings.json 无对应项时使用默认值，不从环境变量读取业务配置"""
    import json

    with open(tmp_settings_file, "w") as f:
        json.dump({}, f)
    monkeypatch.setenv("REQUEST_TIMEOUT", "500")
    import config

    original = config._SETTINGS_FILE
    try:
        config._SETTINGS_FILE = tmp_settings_file
        config._settings = {}
        config._init_settings_sync()
        assert config._settings["request_timeout"] == 300
    finally:
        config._SETTINGS_FILE = original


def test_init_settings_defaults(tmp_settings_file):
    """settings.json 不存在时使用默认值"""
    import config

    original = config._SETTINGS_FILE
    try:
        config._SETTINGS_FILE = tmp_settings_file
        config._settings = {}
        config._init_settings_sync()
        assert config._settings["request_timeout"] == 300
        assert config._settings["max_fail_count"] == 5
    finally:
        config._SETTINGS_FILE = original


def test_get_setting():
    """get_setting 返回内存缓存中的值"""
    import config

    config._settings = {"request_timeout": 600}
    assert config.get_setting("request_timeout") == 600


def test_get_setting_default():
    """get_setting 对不存在的键返回默认值"""
    import config

    config._settings = {}
    assert config.get_setting("max_fail_count") == 5


def test_get_settings_masks_db_url():
    """get_settings 脱敏 request_log_database_url"""
    import config

    config._settings = {
        "request_log_database_url": "postgres://user:secret@host:5432/db",
        "host": "0.0.0.0",
        "port": 55555,
    }
    all_settings = config.get_settings()
    assert "secret" not in all_settings["request_log_database_url"]
    assert "***" in all_settings["request_log_database_url"]
    assert all_settings["request_log_database_url_masked"] == all_settings["request_log_database_url"]


def test_config_defaults():
    """验证配置项默认值"""
    import os

    from config import _CONFIG_SCHEMA

    assert "debug" not in _CONFIG_SCHEMA
    assert _CONFIG_SCHEMA["host"]["default"] == "0.0.0.0"
    assert _CONFIG_SCHEMA["port"]["default"] == 55555
    assert _CONFIG_SCHEMA["request_timeout"]["default"] == 300
    assert _CONFIG_SCHEMA["max_body_size"]["default"] == 10485760
    assert _CONFIG_SCHEMA["log_level"]["default"] == "info"
    assert "database_url" not in _CONFIG_SCHEMA
    assert os.path.basename(_CONFIG_SCHEMA["stats_sqlite_path"]["default"]) == "stats.db"
    assert _CONFIG_SCHEMA["request_log_db_type"]["default"] == "sqlite"
    assert os.path.basename(_CONFIG_SCHEMA["request_log_sqlite_path"]["default"]) == "request_logs.db"
    assert _CONFIG_SCHEMA["request_log_database_url"]["default"] == ""
    assert _CONFIG_SCHEMA["save_request_headers"]["default"] is False
    assert _CONFIG_SCHEMA["save_response_headers"]["default"] is False
    assert _CONFIG_SCHEMA["save_request_body"]["default"] is False
    assert _CONFIG_SCHEMA["save_response_body"]["default"] is False
    assert _CONFIG_SCHEMA["max_fail_count"]["default"] == 5
    assert _CONFIG_SCHEMA["cooldown_seconds"]["default"] == 60
    assert all("env" not in schema for schema in _CONFIG_SCHEMA.values())


def test_config_requires_restart():
    """验证需重启标记"""
    from config import _CONFIG_SCHEMA

    restart_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("requires_restart")]
    assert "host" in restart_keys
    assert "port" in restart_keys
    assert "debug" not in restart_keys
    assert "log_level" in restart_keys
    # 热更新项不在列表中
    assert "request_timeout" not in restart_keys
    assert "max_fail_count" not in restart_keys
    assert "cooldown_seconds" not in restart_keys
    assert "stats_sqlite_path" not in restart_keys
    assert "request_log_db_type" not in restart_keys
    assert "request_log_sqlite_path" not in restart_keys
    assert "request_log_database_url" not in restart_keys
    assert "save_request_headers" not in restart_keys
    assert "save_response_headers" not in restart_keys
    assert "save_request_body" not in restart_keys
    assert "save_response_body" not in restart_keys


def test_config_readonly():
    """验证只读标记"""
    from config import _CONFIG_SCHEMA

    readonly_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("readonly")]
    assert "host" in readonly_keys
    assert "port" in readonly_keys


def test_settings_page_has_no_debug_mode_controls():
    """Settings page must not submit the removed debug config."""
    html = Path("static/index.html").read_text(encoding="utf-8")

    assert "set_debug" not in html
    assert "settings_debug" not in html
    assert "switchSettingsSection('debug')" not in html
    assert 'data-section="debug"' not in html
    assert "data.debug" not in html
    assert "orig.debug" not in html


def test_settings_page_has_request_log_db_controls():
    """Settings page exposes request-log DB switching and lightweight fallback."""
    html = Path("static/index.html").read_text(encoding="utf-8")
    requests_js = Path("static/js/requests.js").read_text(encoding="utf-8")

    assert "set_request_log_db_type" in html
    assert "set_request_log_sqlite_path" in html
    assert 'id="set_request_log_sqlite_path"' in html and "readonly" in html
    assert "set_request_log_database_url" in html
    assert "syncRequestLogDbMode" in html
    assert "set_save_request_headers" in html
    assert "set_save_response_headers" in html
    assert "set_save_request_body" in html
    assert "set_save_response_body" in html
    assert "loadStatsRequestLogs" in requests_js
    assert "params.set('source', 'stats')" in requests_js


def test_settings_page_explains_zero_config_runtime():
    """Settings page documents zero-config startup and storage boundaries."""
    html = Path("static/index.html").read_text(encoding="utf-8")

    assert "零配置启动" in html
    assert "服务不需要 .env" in html
    assert "0.0.0.0:55555" in html
    assert "Docker 端口映射" in html
    assert "data/settings.json" in html
    assert "data/channels.json" in html
    assert "data/api_keys.json" in html
    assert "data/request_logs.db" in html


def test_migrate_lb_config(tmp_path):
    """lb_config 自动迁移到 settings.json"""
    import json

    channels_file = str(tmp_path / "channels.json")
    settings_file = str(tmp_path / "settings.json")

    channels_data = {
        "channels": [],
        "lb_config": {"max_fail_count": 8, "cooldown_seconds": 120},
    }
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)

    import config

    orig_settings = config._SETTINGS_FILE
    config._SETTINGS_FILE = settings_file
    config._settings = {"max_fail_count": 5, "cooldown_seconds": 60}
    try:
        config._migrate_lb_config_sync(channels_file)
        assert config._settings["max_fail_count"] == 8
        assert config._settings["cooldown_seconds"] == 120
        with open(channels_file, "r") as f:
            migrated = json.load(f)
        assert "lb_config" not in migrated
    finally:
        config._SETTINGS_FILE = orig_settings
