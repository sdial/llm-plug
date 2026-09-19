"""字段描述端点契约（ADR-0018 D0，本 spec 唯一新接缝）。

描述是 config 侧 schema ∪ 约束 ∪ UI 元数据的只读投影：键集与 schema 严格相等、
每键 section/label_key 齐备、min/max 保持 wire 刻度、换算键带 unit。漏配立即红。
"""

import httpx
import pytest
import pytest_asyncio

import config
from main import app
from tests.admin_auth_utils import login_admin

pytestmark = pytest.mark.asyncio

KNOWN_SECTIONS = {
    "server",
    "request",
    "format_conversion",
    "lb",
    "timezone",
    "database",
    "security",
    "pii-filter",
    "context_shaping",
    "hidden",
}


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "channels.json").write_text('{"channels": []}', encoding="utf-8")
    (data_dir / "api_keys.json").write_text('{"api_keys": []}', encoding="utf-8")
    (data_dir / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(data_dir / "settings.json"))
    config._init_settings_sync()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await login_admin(c)
        yield c


async def test_schema_key_set_equals_config_schema_keys(client):
    resp = await client.get("/admin/settings/schema")
    assert resp.status_code == 200
    schema = resp.json()
    assert set(schema) == set(config._CONFIG_SCHEMA)


async def test_every_key_has_section_and_label_key(client):
    schema = (await client.get("/admin/settings/schema")).json()
    for key, desc in schema.items():
        assert desc.get("section") in KNOWN_SECTIONS, f"{key} section 非法: {desc.get('section')}"
        assert desc.get("label_key"), f"{key} 缺 label_key"
        assert desc.get("group") is not None, f"{key} 缺 group"


async def test_int_key_with_min_max_and_section(client):
    schema = (await client.get("/admin/settings/schema")).json()
    desc = schema["request_timeout"]
    assert desc["type"] == "int"
    assert desc["default"] == 120
    assert desc["min"] == 1
    assert desc["max"] == 3600
    assert desc["requires_restart"] is False
    assert desc["section"] == "request"
    assert "readonly" not in desc


async def test_conversion_keys_carry_unit_with_wire_scale_bounds(client):
    schema = (await client.get("/admin/settings/schema")).json()
    body = schema["max_body_size"]
    assert body["unit"] == "MB"
    # wire 刻度原值，不做显示刻度拷贝
    assert body["min"] == 1024
    assert body["max"] == 1024 * 1024 * 1024
    assert body["default"] == 20 * 1024 * 1024
    log_body = schema["max_log_body_size"]
    assert log_body["unit"] == "KB"
    assert log_body["min"] == 0
    assert log_body["max"] == 256 * 1024 * 1024
    # 未换算键无 unit
    assert "unit" not in schema["request_timeout"]
    assert "unit" not in schema["host"]


async def test_str_choices_key_with_choice_label_keys(client):
    schema = (await client.get("/admin/settings/schema")).json()
    desc = schema["lb_strategy"]
    assert desc["type"] == "str"
    assert desc["choices"] == ["round_robin", "backup", "sticky"]
    assert desc["choice_label_keys"]["round_robin"] == "settings.lbStrategyRoundRobin"
    assert desc["choice_help_keys"]["backup"] == "settings.lbStrategyHelpBackup"
    assert set(desc["choice_help_keys"]) == {"round_robin", "backup", "sticky"}
    assert desc["default"] == "round_robin"
    assert desc["section"] == "lb"


async def test_bool_and_readonly_and_requires_restart(client):
    schema = (await client.get("/admin/settings/schema")).json()
    assert schema["save_files"]["type"] == "bool"
    assert schema["save_files"]["default"] is True
    host = schema["host"]
    assert host["readonly"] is True
    assert host["requires_restart"] is True
    assert schema["port"]["readonly"] is True
    # 请求记录库路径在设置页一直只读展示（票03 渲染等价基线，漂移修复）
    assert schema["request_log_sqlite_path"]["readonly"] is True
    # 非只读键不出现 readonly 字段
    assert "readonly" not in schema["request_timeout"]


async def test_unrendered_keys_have_explicit_hidden_or_page_sections(client):
    schema = (await client.get("/admin/settings/schema")).json()
    assert schema["stats_sqlite_path"]["section"] == "hidden"
    assert schema["pii_exempt_channels"]["section"] == "hidden"
    assert schema["context_shaping_strip_ansi"]["section"] == "context_shaping"
    assert schema["allow_format_conversion"]["section"] == "format_conversion"


async def test_group_probe_keys_carry_wire_scale_bounds(client):
    """票03：设置页由 schema 渲染后，原 HTML min/max 指纹改由本契约守护——
    探活三键的约束在描述端点保持 wire 刻度登记，前端 displayBounds 换算显示刻度。"""
    schema = (await client.get("/admin/settings/schema")).json()
    interval = schema["group_probe_interval_seconds"]
    assert interval["min"] == 1
    assert interval["max"] == 86400
    assert schema["group_probe_concurrency"]["min"] == 1
    assert schema["group_probe_concurrency"]["max"] == 100
    assert schema["group_probe_timeout"]["min"] == 1
    assert schema["group_probe_timeout"]["max"] == 300


async def test_schema_covers_every_mounted_group_in_settings_fragment(client):
    """票03：渲染分区（server/request/lb/timezone/database/security）的每个
    section:group 分组在描述里都有键——settings.html 挂载点不会落空。"""
    schema = (await client.get("/admin/settings/schema")).json()
    mounted_sections = {"server", "request", "lb", "timezone", "database", "security"}
    covered = {(d["section"], d["group"]) for d in schema.values() if d["section"] in mounted_sections}
    html_groups = {
        ("server", 0),
        ("request", 0),
        ("request", 1),
        ("lb", 0),
        ("lb", 1),
        ("lb", 2),
        ("lb", 3),
        ("timezone", 0),
        ("database", 0),
        ("database", 1),
        ("database", 2),
        ("database", 3),
        ("security", 0),
    }
    assert html_groups <= covered, f"挂载点缺少描述覆盖: {html_groups - covered}"


async def test_schema_endpoint_requires_admin_auth():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/admin/settings/schema")
    assert resp.status_code in (401, 403)
