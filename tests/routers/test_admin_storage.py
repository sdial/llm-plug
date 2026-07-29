import json
import os

import httpx
import pytest
import pytest_asyncio

import config
import stats
import storage
import storage_stats
from main import app
from tests.admin_auth_utils import login_admin

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_env(tmp_path, monkeypatch):
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

    # 让 storage_stats 的 logs_dir 指向 tmp/logs
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(storage_stats, "_get_logs_dir", lambda: str(logs_dir))

    import main

    monkeypatch.setattr(
        main,
        "_whitelist_cache",
        main._whitelist.WhitelistCache(str(data_dir / "whitelist.csv")),
    )

    await stats.init_db(str(tmp_path / "stats.db"))
    yield
    await stats.close_pool()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await login_admin(c)
        yield c


async def test_get_storage_stats_returns_valid_structure(client):
    # 在 logs 目录创建一个测试文件
    logs_dir = storage_stats._get_logs_dir()
    with open(os.path.join(logs_dir, "test.log"), "w") as f:
        f.write("test content")

    resp = await client.get("/admin/storage/stats")
    assert resp.status_code == 200
    data = resp.json()

    assert "total_size" in data
    assert isinstance(data["total_size"], int)
    assert "logs" in data
    assert "request_raw_logs" in data
    assert "other_data" in data

    assert data["logs"]["path"] == "logs/"
    assert "size" in data["logs"]
    assert "files" in data["logs"]
    assert data["request_raw_logs"]["path"] == "data/request_raw_logs/"
    assert "months" in data["request_raw_logs"]
    assert data["other_data"]["path"] == "data/"
    assert "files" in data["other_data"]


async def test_get_storage_stats_empty(client):
    resp = await client.get("/admin/storage/stats")
    assert resp.status_code == 200
    data = resp.json()
    # logs 与 raw_logs 应为空
    assert data["logs"]["files"] == []
    assert data["request_raw_logs"]["months"] == []
    # other_data 可能有 channels.json 等配置文件
    assert isinstance(data["total_size"], int)


async def test_cleanup_requires_action(client):
    resp = await client.post("/admin/storage/cleanup", json={})
    assert resp.status_code == 422  # Pydantic 缺少必填字段


async def test_cleanup_unknown_action(client):
    resp = await client.post(
        "/admin/storage/cleanup", json={"action": "unknown_action"}
    )
    assert resp.status_code == 422  # Pydantic Literal 校验拒绝


async def test_cleanup_clear_logs_rejected(client):
    resp = await client.post("/admin/storage/cleanup", json={"action": "clear_logs"})
    assert resp.status_code == 422  # Pydantic Literal 校验拒绝


async def test_cleanup_clear_all_rejected(client):
    resp = await client.post("/admin/storage/cleanup", json={"action": "clear_all"})
    assert resp.status_code == 422  # Pydantic Literal 校验拒绝


async def test_cleanup_delete_month_requires_target(client):
    resp = await client.post("/admin/storage/cleanup", json={"action": "delete_month"})
    assert resp.status_code == 422  # Pydantic 缺少必填字段 target
    error_details = resp.json()["detail"]
    assert isinstance(error_details, list)
    # Pydantic 验证错误格式: loc=["body", "target"], type="missing"
    assert any(
        error.get("type") == "missing"
        and error.get("loc", [])[-1:] == ["target"]
        for error in error_details
    )


async def test_cleanup_delete_month_missing_db(client):
    resp = await client.post(
        "/admin/storage/cleanup",
        json={"action": "delete_month", "target": "202601"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False


async def test_cleanup_delete_month_success(client, tmp_path):
    # 创建一个月份数据库
    raw_logs_dir = os.path.join(config.DATA_DIR, "request_raw_logs")
    os.makedirs(raw_logs_dir, exist_ok=True)
    db_path = os.path.join(raw_logs_dir, "request_logs_2026_06.sqlite3")
    with open(db_path, "wb") as f:
        f.write(b"x" * 1000)

    resp = await client.post(
        "/admin/storage/cleanup",
        json={"action": "delete_month", "target": "202606"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["freed_bytes"] >= 1000
    assert not os.path.exists(db_path)


async def test_preview_cleanup_delete_month(client, tmp_path):
    raw_logs_dir = os.path.join(config.DATA_DIR, "request_raw_logs")
    os.makedirs(raw_logs_dir, exist_ok=True)
    db_path = os.path.join(raw_logs_dir, "request_logs_2026_06.sqlite3")
    with open(db_path, "wb") as f:
        f.write(b"x" * 500)

    resp = await client.post(
        "/admin/storage/cleanup/preview",
        json={"action": "delete_month", "target": "202606"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "delete_month"
    assert data["target"] == "2026-06"
    assert data["freed_bytes"] >= 500
    assert len(data["will_delete"]) >= 1
    # 预览不应实际删除
    assert os.path.exists(db_path)


async def test_ui_storage_fragment(client):
    # 需要先创建片段文件，否则会返回 404
    # 这里只验证路由存在
    from routers import admin

    fragment_dir = admin.ADMIN_FRAGMENT_DIR
    storage_fragment = fragment_dir / "storage.html"
    if not storage_fragment.exists():
        pytest.skip("storage.html 片段尚未创建")
    resp = await client.get("/admin/ui/storage")
    assert resp.status_code == 200
