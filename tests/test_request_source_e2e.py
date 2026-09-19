"""Ticket 04 — admin_test 端到端（ADR-0009）。

真实的 ``test_channel`` → 完整生产发送栈 → mock 上游回包 → 请求记录经
``/admin/requests?request_source=admin_test`` 可查回该条记录、来源正确。

mock 上游由 ``e2e_mock_server``（127.0.0.1:19999）会话级承载；渠道、请求日志
库与管理员认证均落在独立 tmp 数据目录（ASGITransport 不等价 TestClient 的
lifespan，request_logs 后端显式初始化）。
"""

import json

import httpx
import pytest
import pytest_asyncio

import config
import request_logs
import storage
from main import app
from tests.admin_auth_utils import login_admin

_MOCK_BASE = "http://127.0.0.1:19999"


@pytest_asyncio.fixture
async def admin_test_env(e2e_mock_server, tmp_path, monkeypatch):
    """孤立数据目录 + 指向 mock 上游的渠道 + 就绪的 request_logs 后端。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"

    channels_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "id": "ch_e2e_admin_test",
                        "name": "AdminTest",
                        "api_key": "test-key",
                        "models": ["gpt-4o"],
                        "enabled": True,
                        "weight": 1,
                        "priority": 1,
                        "socks5_proxy": None,
                        "endpoints": [
                            {
                                "api_type": "openai-chat-completions",
                                "base_url": f"{_MOCK_BASE}/openai",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
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

    await request_logs.close_backend()
    result = await request_logs.init_backend({"request_log_sqlite_path": str(data_dir / "request_logs.db")})
    assert result["available"] is True
    yield data_dir
    await request_logs.close_backend()


@pytest.mark.anyio
async def test_admin_test_channel_records_request_source(admin_test_env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_e2e_admin_test/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert len(body["results"]) == 1
        entry = body["results"][0]
        assert entry["api_type"] == "openai-chat-completions"
        assert entry["success"] is True
        assert entry["message"] == "测试通过"
        assert isinstance(entry["latency_ms"], int)
        assert entry["model"] == "gpt-4o"  # 渠道模型列表首个

        # 发送栈入队，drain 后才能查回落库行
        await request_logs.drain_queue()

        listed = await client.get("/admin/requests", params={"request_source": "admin_test"})
        assert listed.status_code == 200
        items = listed.json()["items"]
        matches = [it for it in items if it["channel_id"] == "ch_e2e_admin_test"]
        assert matches, "渠道测试应产生至少一条 admin_test 请求记录"
        row = matches[0]
        assert row["request_source"] == "admin_test"
        assert row["success"] is True
        assert row["api_type"] == "openai-chat-completions"

        # 本测试经 create_client 在 pytest-asyncio 事件循环缓存了 httpx 客户端；
        # 该循环在测试结束后关闭，若残留会让后续 TestClient 的 lifespan
        # close_all_clients 对已关循环的 socket 调 aclose() 抛 RuntimeError，
        # 必须在循环存活期内归还连接池。
        from client import close_all_clients

        await close_all_clients()
