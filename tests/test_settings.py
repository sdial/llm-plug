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


def test_init_settings_env_fallback(tmp_settings_file, monkeypatch):
    """settings.json 无对应项时回退到环境变量"""
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
        assert config._settings["request_timeout"] == 500
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
    """get_settings 脱敏 database_url"""
    import config
    config._settings = {"database_url": "postgres://user:secret@host:5432/db", "host": "0.0.0.0", "port": 55555}
    all_settings = config.get_settings()
    assert "secret" not in all_settings["database_url"]
    assert "***" in all_settings["database_url"]


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


def test_migrate_lb_config(tmp_path):
    """lb_config 自动迁移到 settings.json"""
    import json
    channels_file = str(tmp_path / "channels.json")
    settings_file = str(tmp_path / "settings.json")

    channels_data = {
        "channels": [],
        "lb_config": {"max_fail_count": 8, "cooldown_seconds": 120}
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
