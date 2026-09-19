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


def test_existing_settings_without_retention_keys_keep_legacy_no_cleanup_defaults(
    tmp_settings_file,
):
    """已有 settings.json 升级时，缺失的日志保留期字段保持旧版不清理语义。"""
    import json

    with open(tmp_settings_file, "w") as f:
        json.dump({"request_timeout": 600}, f)
    import config

    original = config._SETTINGS_FILE
    try:
        config._SETTINGS_FILE = tmp_settings_file
        config._settings = {}
        config._init_settings_sync()
        assert config._settings["request_log_retention_days"] == 0
        assert config._settings["request_log_raw_retention_days"] == 0
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
        assert config._settings["request_timeout"] == 120
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
        assert config._settings["request_timeout"] == 120
        assert config._settings["max_fail_count"] == 3
        assert config._settings["cooldown_seconds"] == 120
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
    assert config.get_setting("max_fail_count") == 3
    assert config.get_setting("cooldown_seconds") == 120


def test_config_defaults():
    """验证配置项默认值"""
    import os

    from config import _CONFIG_SCHEMA

    assert "debug" not in _CONFIG_SCHEMA
    assert _CONFIG_SCHEMA["host"]["default"] == "0.0.0.0"
    assert _CONFIG_SCHEMA["port"]["default"] == 55555
    assert _CONFIG_SCHEMA["request_timeout"]["default"] == 120
    assert _CONFIG_SCHEMA["max_body_size"]["default"] == 20971520
    assert "log_level" not in _CONFIG_SCHEMA  # 已移除，改用 --log-level CLI 参数
    assert "database_url" not in _CONFIG_SCHEMA
    assert os.path.basename(_CONFIG_SCHEMA["stats_sqlite_path"]["default"]) == "stats.db"
    assert os.path.basename(_CONFIG_SCHEMA["request_log_sqlite_path"]["default"]) == "request_logs.db"
    assert "request_log_db_type" not in _CONFIG_SCHEMA
    assert "request_log_database_url" not in _CONFIG_SCHEMA
    assert _CONFIG_SCHEMA["save_request_headers"]["default"] is True
    assert _CONFIG_SCHEMA["save_response_headers"]["default"] is True
    assert _CONFIG_SCHEMA["save_request_body"]["default"] is True
    assert _CONFIG_SCHEMA["save_response_body"]["default"] is True
    assert _CONFIG_SCHEMA["max_fail_count"]["default"] == 3
    assert _CONFIG_SCHEMA["cooldown_seconds"]["default"] == 120
    assert _CONFIG_SCHEMA["context_shaping_strip_ansi"]["default"] is True
    assert _CONFIG_SCHEMA["context_shaping_trim_trailing_whitespace"]["default"] is False
    assert _CONFIG_SCHEMA["context_shaping_caveman_enabled"]["default"] is False
    assert "context_shaping_claude_code_enabled" not in _CONFIG_SCHEMA
    assert "context_shaping_claude_code_core_metadata" not in _CONFIG_SCHEMA
    assert all("env" not in schema for schema in _CONFIG_SCHEMA.values())


def test_context_shaping_default_ignores_legacy_context_optimizer_values(monkeypatch, tmp_path):
    import json

    import config

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"ctx_optimize_enabled": False, "ctx_optimize_strip_ansi": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))

    config._init_settings_sync()

    assert config.get_setting("context_shaping_strip_ansi") is True


def test_config_requires_restart():
    """验证需重启标记"""
    from config import _CONFIG_SCHEMA

    restart_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("requires_restart")]
    assert "host" in restart_keys
    assert "port" in restart_keys
    assert "debug" not in restart_keys
    assert "log_level" not in restart_keys  # 已移除，改用 --log-level CLI 参数
    # 热更新项不在列表中
    assert "request_timeout" not in restart_keys
    assert "max_fail_count" not in restart_keys
    assert "cooldown_seconds" not in restart_keys
    assert "stats_sqlite_path" not in restart_keys
    assert "request_log_sqlite_path" not in restart_keys
    assert "save_request_headers" not in restart_keys
    assert "save_response_headers" not in restart_keys
    assert "save_request_body" not in restart_keys
    assert "save_response_body" not in restart_keys
    assert "context_shaping_strip_ansi" not in restart_keys


def test_config_readonly():
    """验证只读标记"""
    from config import _CONFIG_SCHEMA

    readonly_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("readonly")]
    assert "host" in readonly_keys
    assert "port" in readonly_keys


def test_settings_page_has_no_debug_mode_controls():
    """Settings page must not submit the removed debug config."""
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")

    assert "set_debug" not in html
    assert "settings_debug" not in html
    assert "switchSettingsSection('debug')" not in html
    assert 'data-section="debug"' not in html
    assert "data.debug" not in html
    assert "orig.debug" not in html


def test_settings_page_database_section_uses_schema_mounts():
    """票03（ADR-0018 D2）：数据库分区控件由 schema 渲染——SQLite 路径与原始信息
    开关经 database:0/database:1 挂载点渲染（readonly 样式与开关结构由渲染器按
    描述产出），已删除的手写库类型/连接串控件不得回流。"""
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")

    assert 'data-schema-group="database:0"' in html
    assert 'data-schema-group="database:1"' in html
    assert 'data-schema-group="database:2"' in html
    assert 'data-schema-group="database:3"' in html
    # 挂载分区的手写控件清零（含旧 KB 后缀 id）
    for key in (
        "set_request_log_sqlite_path",
        "set_save_request_headers",
        "set_save_response_headers",
        "set_save_request_body",
        "set_save_response_body",
        "set_save_files",
        "set_save_images",
        "set_save_audios",
        "set_max_log_body_size",
        "set_max_log_body_size_kb",
        "set_request_log_retention_days",
        "set_request_log_raw_retention_days",
    ):
        assert key not in html, f"database 分区手写控件残留: {key}"
    # 只读输入由描述 readonly 标记驱动（渲染器产出）；仅 pii 实测输出框保留手写只读
    assert all("pii_test_output" in ln for ln in html.splitlines() if "readonly" in ln)
    assert "set_request_log_db_type" not in html
    assert "set_request_log_database_url" not in html
    assert "syncRequestLogDbMode" not in html


def test_settings_page_explains_zero_config_runtime():
    """Settings page documents zero-config startup and storage boundaries."""
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")

    assert "零配置启动" in html
    assert "服务不需要 .env" in html
    assert "0.0.0.0:55555" in html
    assert "Docker 端口映射" in html
    assert "data/settings.json" in html
    assert "data/channels.json" in html
    assert "data/api_keys.json" in html
    assert "data/request_logs.db" in html


def test_settings_js_binds_security_keys_from_main_settings_get():
    """票02（行为变化④）：安全分区两键从主 GET /admin/settings 读取、由描述循环
    绑定参与保存与脏检测；对 security-config 的冗余读取与逐字段枚举删除。"""
    js = Path("static/js/settings.js").read_text(encoding="utf-8")

    # 描述键集合驱动的通用回填（安全键与其他键同一加载路径）
    assert "Object.keys(schema).forEach((key) => _writeValueToDom(key, schema[key], data[key]))" in js
    # 不再逐字段枚举安全键，也不再单独请求 security-config
    assert "set_admin_max_attempts" not in js
    assert "set_admin_lockout_base_seconds" not in js
    assert "_settingsDirtySections.add('security')" not in js
    assert "security-config" not in js


def test_init_settings_explicit_5_60_preserved(tmp_settings_file):
    """显式保存 5/60 的部署保留原值，不被新默认 3/120 覆盖（D2 兼容）。"""
    import json

    data = {"max_fail_count": 5, "cooldown_seconds": 60}
    with open(tmp_settings_file, "w") as f:
        json.dump(data, f)
    import config

    original = config._SETTINGS_FILE
    try:
        config._SETTINGS_FILE = tmp_settings_file
        config._settings = {}
        config._init_settings_sync()
        assert config._settings["max_fail_count"] == 5
        assert config._settings["cooldown_seconds"] == 60
    finally:
        config._SETTINGS_FILE = original


@pytest.mark.anyio
async def test_migrate_lb_config_preserves_explicit_5_60(tmp_path, monkeypatch):
    """显式保存 5/60 + channels.json 带 lb_config → 5/60 保留，迁移哨兵不误覆盖。

    D2 哨兵已从 5/60 收紧到 3/120：若仍用旧哨兵 5，
    ``_settings.get("max_fail_count", 5) == 5`` 会把"显式保存旧默认"误判为"未自定义"，
    从而用 lb_config 覆盖用户的 5/60。本测试钉住哨兵收紧后的行为。
    """
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
    from channel_catalog import catalog

    orig_settings = config._SETTINGS_FILE
    config._SETTINGS_FILE = settings_file
    config._settings = {"max_fail_count": 5, "cooldown_seconds": 60}
    monkeypatch.setattr(config, "CHANNELS_FILE", channels_file)
    catalog.reset()
    try:
        await config._migrate_lb_config()
        assert config._settings["max_fail_count"] == 5
        assert config._settings["cooldown_seconds"] == 60
    finally:
        config._SETTINGS_FILE = orig_settings


@pytest.mark.anyio
async def test_migrate_lb_config(tmp_path, monkeypatch):
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
    from channel_catalog import catalog

    orig_settings = config._SETTINGS_FILE
    config._SETTINGS_FILE = settings_file
    config._settings = {"max_fail_count": 3, "cooldown_seconds": 120}
    monkeypatch.setattr(config, "CHANNELS_FILE", channels_file)
    catalog.reset()
    try:
        await config._migrate_lb_config()
        assert config._settings["max_fail_count"] == 8
        assert config._settings["cooldown_seconds"] == 120
        with open(channels_file) as f:
            migrated = json.load(f)
        assert "lb_config" not in migrated
    finally:
        config._SETTINGS_FILE = orig_settings


@pytest.mark.anyio
async def test_init_settings_persists_migrated_lb_config(tmp_path, monkeypatch):
    """init_settings 迁移 lb_config 后必须写入 settings.json，避免重启后丢失。"""
    import json

    channels_file = tmp_path / "channels.json"
    settings_file = tmp_path / "settings.json"
    channels_file.write_text(
        json.dumps(
            {
                "channels": [],
                "lb_config": {"max_fail_count": 8, "cooldown_seconds": 120},
            }
        ),
        encoding="utf-8",
    )
    settings_file.write_text(json.dumps({}), encoding="utf-8")

    import config

    async def noop_apply_lb_settings():
        return None

    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "_apply_lb_settings", noop_apply_lb_settings)
    config._settings = {}

    await config.init_settings()

    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted["max_fail_count"] == 8
    assert persisted["cooldown_seconds"] == 120
    migrated = json.loads(channels_file.read_text(encoding="utf-8"))
    assert "lb_config" not in migrated


@pytest.mark.anyio
async def test_context_shaping_settings_hot_update(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    import config

    monkeypatch.setattr(config, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "_settings", {})
    monkeypatch.setattr(config, "_save_settings_to_disk", AsyncMock())
    monkeypatch.setattr(config, "_apply_lb_settings", AsyncMock())

    result = await config.update_settings({"context_shaping_strip_ansi": False})
    assert result["updated"] == ["context_shaping_strip_ansi"]
    assert result["needs_restart"] is False
    assert config.get_setting("context_shaping_strip_ansi") is False
    assert config.get_setting("context_shaping_trim_trailing_whitespace") is False


@pytest.mark.anyio
async def test_update_settings_rolls_back_memory_when_persist_fails(monkeypatch):
    import config

    async def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(config, "_settings", {"max_body_size": 2048})
    monkeypatch.setattr(config, "_save_settings_to_disk", fail_save)

    with pytest.raises(OSError, match="disk full"):
        await config.update_settings({"max_body_size": 4096})

    assert config.get_setting("max_body_size") == 2048
