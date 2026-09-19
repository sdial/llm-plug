import json

import httpx
import pytest
import pytest_asyncio

import config
import request_logs
import stats
import storage
from channel_catalog import catalog
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
    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None

    import middleware.whitelist_middleware as wmod
    import whitelist as _whitelist_mod

    monkeypatch.setattr(
        wmod,
        "_whitelist_cache",
        _whitelist_mod.WhitelistCache(str(data_dir / "whitelist.csv")),
    )

    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": True,
            "save_response_headers": True,
            "save_request_body": True,
            "save_response_body": True,
        },
    )
    await stats.init_db(str(tmp_path / "stats.db"))
    await request_logs.init_backend(
        {
            "request_log_sqlite_path": str(tmp_path / "request_logs.db"),
        }
    )
    yield
    await stats.close_pool()
    await request_logs.close_backend()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await login_admin(c)
        yield c


class TestListRequestsEndpoint:
    async def test_returns_empty_list(self, client):
        async def fake_list_requests(**kwargs):
            return {
                "available": True,
                "items": [],
                "total": 0,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
            }

        from routers import admin

        original = admin.request_log_list_requests
        admin.request_log_list_requests = fake_list_requests
        try:
            resp = await client.get("/admin/requests")
        finally:
            admin.request_log_list_requests = original
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_source_stats_returns_lightweight_records(self, client):
        async def fake_stats_list_requests(**kwargs):
            return {
                "items": [{"id": 1, "model": "gpt-4o"}],
                "total": 1,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
            }

        from routers import admin

        original = admin.stats_list_requests
        admin.stats_list_requests = fake_stats_list_requests
        try:
            resp = await client.get("/admin/requests?source=stats")
        finally:
            admin.stats_list_requests = original
        assert resp.status_code == 200
        assert resp.json()["source"] == "stats"
        assert resp.json()["items"][0]["model"] == "gpt-4o"

    async def test_request_items_include_channel_api_type(self, client):
        from models.channel import Channel, Endpoint

        async def fake_list_requests(**kwargs):
            return {
                "items": [
                    {
                        "id": 1,
                        "model": "claude",
                        "channel_id": "anth",
                        "channel_name": "Anthropic",
                    }
                ],
                "total": 1,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
            }

        from routers import admin

        await catalog.add_channel(
            Channel(
                id="anth",
                name="Anthropic",
                api_key="sk-test",
                models=["claude"],
                endpoints=[Endpoint(api_type="anthropic", base_url="https://api.anthropic.com")],
            )
        )
        original_list = admin.request_log_list_requests
        admin.request_log_list_requests = fake_list_requests
        try:
            resp = await client.get("/admin/requests")
        finally:
            admin.request_log_list_requests = original_list
        assert resp.status_code == 200
        assert resp.json()["items"][0]["api_type"] == "anthropic"

    async def test_request_log_backend_unavailable_returns_503(self, client):
        async def fake_list_requests(**kwargs):
            return {
                "available": False,
                "error": "request log backend unavailable",
                "items": [],
                "total": 0,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
            }

        from routers import admin

        original = admin.request_log_list_requests
        admin.request_log_list_requests = fake_list_requests
        try:
            resp = await client.get("/admin/requests")
        finally:
            admin.request_log_list_requests = original
        assert resp.status_code == 503
        assert "request log backend unavailable" in resp.json()["detail"]

    async def test_pagination(self, client):
        for i in range(15):
            request_logs.record_request(
                channel_id=f"ch_{i}",
                channel_name=f"Channel {i}",
                model="gpt-4",
                is_stream=False,
                input_tokens=10,
                output_tokens=5,
                latency_ms=100,
                success=True,
            )
        await request_logs.drain_queue()
        resp = await client.get("/admin/requests?page=1&page_size=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 10
        assert data["total"] == 15

        resp = await client.get("/admin/requests?page=2&page_size=10")
        data = resp.json()
        assert len(data["items"]) == 5

    async def test_filter_by_model(self, client):
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-3.5",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        await request_logs.drain_queue()
        resp = await client.get("/admin/requests?model=gpt-4")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["model"] == "gpt-4"

    async def test_requests_include_api_key_name_and_filter_by_api_key_id(self, client):
        await storage.save_api_keys(
            {
                "api_keys": [
                    {"id": "key_alpha", "name": "Alpha Key", "key": "sk-alpha"},
                    {"id": "key_beta", "name": "Beta Key", "key": "sk-beta"},
                ]
            }
        )
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            api_key_id="key_alpha",
        )
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            api_key_id="key_beta",
        )
        await request_logs.drain_queue()

        resp = await client.get("/admin/requests?api_key_id=key_beta")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["api_key_id"] == "key_beta"
        assert data["items"][0]["api_key_name"] == "Beta Key"


class TestRequestSourceFiltering:
    """GET /admin/requests 与 /admin/stats 的 request_source 来源过滤（ADR-0009 Ticket 02）。

    全部走真实后端写入（仅 source=stats 分支的透传断言用 fake 捕获），
    验证的是 HTTP 参数归一化 + 存储层过滤的外部行为。
    """

    async def _seed_mixed_sources_in_request_logs(self):
        request_logs.record_request(
            channel_id="ch_client",
            channel_name="Channel Client",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        request_logs.record_request(
            channel_id="ch_admin",
            channel_name="Channel Admin",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            request_source="admin_test",
        )
        request_logs.record_request(
            channel_id="ch_probe",
            channel_name="Channel Probe",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            request_source="group_probe",
        )
        await request_logs.drain_queue()

    async def test_single_value_filters_to_matching_source(self, client):
        await self._seed_mixed_sources_in_request_logs()

        resp = await client.get("/admin/requests?request_source=admin_test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["request_source"] == "admin_test"
        assert data["items"][0]["channel_id"] == "ch_admin"

    async def test_comma_separated_values_select_multiple_sources(self, client):
        await self._seed_mixed_sources_in_request_logs()

        resp = await client.get("/admin/requests?request_source=admin_test,group_probe")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert {item["request_source"] for item in data["items"]} == {"admin_test", "group_probe"}

    async def test_repeated_query_keys_select_multiple_sources(self, client):
        await self._seed_mixed_sources_in_request_logs()

        resp = await client.get("/admin/requests?request_source=admin_test&request_source=group_probe")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert {item["request_source"] for item in data["items"]} == {"admin_test", "group_probe"}

    async def test_no_param_keeps_legacy_unfiltered_behavior(self, client):
        await self._seed_mixed_sources_in_request_logs()

        resp = await client.get("/admin/requests")

        assert resp.status_code == 200
        data = resp.json()
        # 回归保护：未传参不过滤，三种来源（含非 client）全部可见
        assert data["total"] == 3
        assert {item["request_source"] for item in data["items"]} == {"client", "admin_test", "group_probe"}

    async def test_items_carry_request_source_field_without_extra_wrapping(self, client):
        await self._seed_mixed_sources_in_request_logs()

        resp = await client.get("/admin/requests?request_source=client")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["request_source"] == "client"

    async def test_invalid_value_returns_400_listing_legal_values(self, client):
        await self._seed_mixed_sources_in_request_logs()

        resp = await client.get("/admin/requests?request_source=bogus")

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "bogus" in detail
        for legal in request_logs.REQUEST_SOURCES:
            assert legal in detail

    async def test_invalid_token_among_valid_ones_still_rejected(self, client):
        resp = await client.get("/admin/requests?request_source=client,bogus")

        assert resp.status_code == 400
        assert "bogus" in resp.json()["detail"]

    async def test_stats_branch_passes_normalized_request_source_through(self, client):
        captured: dict = {}

        async def fake_stats_list_requests(**kwargs):
            captured.update(kwargs)
            return {"items": [], "total": 0, "page": kwargs["page"], "page_size": kwargs["page_size"]}

        from routers import admin

        original = admin.stats_list_requests
        admin.stats_list_requests = fake_stats_list_requests
        try:
            resp_comma = await client.get("/admin/requests?source=stats&request_source=admin_test,group_probe")
            assert resp_comma.status_code == 200
            assert captured["request_source"] == ("admin_test", "group_probe")

            resp_repeated = await client.get("/admin/requests?source=stats&request_source=admin_test&request_source=client")
            assert resp_repeated.status_code == 200
            assert captured["request_source"] == ("admin_test", "client")

            resp_none = await client.get("/admin/requests?source=stats")
            assert resp_none.status_code == 200
            assert captured["request_source"] is None
        finally:
            admin.stats_list_requests = original

    async def test_stats_branch_filters_with_real_backend(self, client):
        stats.record_request(
            channel_id="ch_1",
            channel_name="Channel One",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        stats.record_request(
            channel_id="ch_2",
            channel_name="Channel Two",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            request_source="admin_test",
        )
        await stats.drain_queue()

        resp = await client.get("/admin/requests?source=stats&request_source=admin_test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["request_source"] == "admin_test"

    async def test_admin_stats_forwards_request_source_to_daily_queries_and_keeps_shape(self, client):
        for source in ("client", "admin_test", "admin_test"):
            stats.record_request(
                channel_id=f"ch_{source}",
                channel_name="Channel Stats",
                model="gpt-4",
                is_stream=False,
                input_tokens=10,
                output_tokens=5,
                latency_ms=100,
                success=True,
                request_source=source,
            )
        await stats.drain_queue()

        unfiltered = (await client.get("/admin/stats?days=1")).json()
        filtered = (await client.get("/admin/stats?days=1&request_source=admin_test")).json()

        assert sum(rec["total_requests"] for rec in unfiltered["daily"]) == 3
        assert len(filtered["daily"]) == 1
        assert filtered["daily"][0]["total_requests"] == 2
        # 响应结构不新增键：过滤与否键集合完全一致
        assert set(filtered.keys()) == set(unfiltered.keys())
        assert set(filtered["daily"][0].keys()) == set(unfiltered["daily"][0].keys())

    async def test_admin_stats_rejects_invalid_request_source_with_400(self, client):
        resp = await client.get("/admin/stats?days=1&request_source=bogus")

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "bogus" in detail
        for legal in request_logs.REQUEST_SOURCES:
            assert legal in detail


class TestRequestFieldEndpoints:
    async def test_get_request_headers(self, client):
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            request_headers={"X-App-Name": "TestApp"},
        )
        await request_logs.drain_queue()
        all_reqs = await request_logs.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/request-headers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["X-App-Name"] == "TestApp"

    async def test_get_request_body(self, client):
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            request_body={"messages": [{"role": "user", "content": "hi"}]},
        )
        await request_logs.drain_queue()
        all_reqs = await request_logs.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/request-body")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["messages"][0]["content"] == "hi"

    async def test_get_response_headers(self, client):
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            response_headers={"X-RateLimit": "100"},
        )
        await request_logs.drain_queue()
        all_reqs = await request_logs.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-headers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["X-RateLimit"] == "100"

    async def test_get_response_body(self, client):
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            response_body={"choices": [{"message": {"content": "hello"}}]},
        )
        await request_logs.drain_queue()
        all_reqs = await request_logs.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-body")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["choices"][0]["message"]["content"] == "hello"

    async def test_nonexistent_id_returns_404(self, client):
        resp = await client.get("/admin/requests/999999/request-headers")
        assert resp.status_code == 404

    async def test_invalid_field_returns_400(self, client):
        resp = await client.get("/admin/requests/1/invalid-field")
        assert resp.status_code == 400

    async def test_null_field_returns_null_data(self, client):
        request_logs.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        await request_logs.drain_queue()
        all_reqs = await request_logs.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-body")
        assert resp.status_code == 200
        assert resp.json()["data"] is None


async def _fake_reload_backend_ok():
    return {"available": True}


class TestSettingsEndpoint:
    async def test_update_settings_reloads_request_log_backend(self, client, monkeypatch):
        calls = []

        async def fake_reload_backend():
            calls.append("reload")
            return {"available": True}

        from routers import admin

        monkeypatch.setattr(admin.request_logs, "reload_backend", fake_reload_backend)

        resp = await client.put(
            "/admin/settings",
            json={"request_timeout": 600},
        )

        assert resp.status_code == 200
        assert calls == ["reload"]

    async def test_get_settings_returns_bare_wire_values(self, client):
        """票02：GET 收缩为裸 wire 值——派生 *_mb/*_kb 键删除，换算归前端绑定器。"""
        resp = await client.get("/admin/settings")
        assert resp.status_code == 200
        data = resp.json()
        # 键集与 schema 严格相等（无路由层追加的派生键）
        assert set(data) == set(config._CONFIG_SCHEMA)
        assert "max_body_size_mb" not in data
        assert "max_log_body_size_kb" not in data
        # 换算键为 wire 字节刻度原值
        assert data["max_body_size"] == config._CONFIG_SCHEMA["max_body_size"]["default"]
        assert data["max_body_size"] == 20 * 1024 * 1024
        assert data["max_log_body_size"] == 0

    async def test_put_unknown_key_returns_400(self, client):
        resp = await client.put("/admin/settings", json={"no_such_key": 1})
        assert resp.status_code == 400
        assert "未知配置项" in resp.json()["detail"]

    async def test_put_soft_constraint_warnings_passthrough(self, client, monkeypatch):
        """探活间隔 > 冷却期的软约束告警随 200 响应透传（前端以 info 呈现）。"""
        monkeypatch.setattr(request_logs, "reload_backend", _fake_reload_backend_ok)
        resp = await client.put(
            "/admin/settings",
            json={"group_probe_interval_seconds": 200, "cooldown_seconds": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["warnings"], "软约束告警应随响应返回"
        assert "group_probe_interval_seconds(200) > cooldown_seconds(100)" in body["warnings"][0]

    async def test_put_structured_400_when_log_backend_reload_fails(self, client, monkeypatch):
        """请求日志新 backend 初始化失败 → 结构化 400（detail.message 文本）。"""

        async def fake_reload_backend():
            return {"available": False, "error": "init failed"}

        monkeypatch.setattr(request_logs, "reload_backend", fake_reload_backend)
        resp = await client.put("/admin/settings", json={"request_timeout": 600})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "请求记录库配置已保存" in detail["message"]
        assert detail["request_log_backend"]["available"] is False
        assert detail["settings"]["updated"] == ["request_timeout"]


async def test_cleanup_request_logs_endpoint_returns_zero_when_nothing_old(client):
    """POST /admin/request-logs/cleanup returns 200 with stats dict when nothing to clean."""
    resp = await client.post("/admin/request-logs/cleanup")
    assert resp.status_code == 200
    body = resp.json()
    assert "raw_fields_cleared" in body
    assert "rows_deleted" in body
    assert body["raw_fields_cleared"] == 0
    assert body["rows_deleted"] == 0


async def test_fetch_models_uses_advanced_models_url(client, monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "mimo-v2.5-pro"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    resp = await client.post(
        "/admin/channels/fetch-models",
        json={
            "base_url": "https://api.example.com",
            "models_url": "https://gateway.example.com/custom/models",
            "api_key": "sk-test",
            "api_type": "openai-chat-completions",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"models": ["mimo-v2.5-pro"]}
    assert captured["url"] == "https://gateway.example.com/custom/models"


async def test_fetch_models_falls_back_to_base_url_when_advanced_models_url_missing(client, monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"models": [{"name": "claude-3"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers, **kwargs):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    resp = await client.post(
        "/admin/channels/fetch-models",
        json={
            "base_url": "https://api.example.com/v1",
            "models_url": "",
            "api_key": "sk-test",
            "api_type": "anthropic",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"models": ["claude-3"]}
    assert captured["url"] == "https://api.example.com/v1/models"


async def test_create_channel_accepts_public_base_url(client):
    resp = await client.post(
        "/admin/channels",
        json={
            "name": "public",
            "api_key": "sk-test",
            "models": ["gpt-4o"],
            "endpoints": [{"api_type": "openai-chat-completions", "base_url": "https://8.8.8.8"}],
        },
    )

    assert resp.status_code == 200
    created = resp.json()
    assert created["endpoints"][0]["base_url"] == "https://8.8.8.8"
    assert len((await catalog.snapshot()).channels) == 1


async def test_create_channel_accepts_private_base_url(client):
    """LAN 内网 base_url 允许创建渠道（已取消 SSRF 限制，LAN->LAN 合法）"""
    resp = await client.post(
        "/admin/channels",
        json={
            "name": "lan",
            "api_key": "sk-test",
            "models": ["gpt-4o"],
            "endpoints": [
                {
                    "api_type": "openai-chat-completions",
                    "base_url": "http://192.168.1.100:8000",
                }
            ],
        },
    )

    assert resp.status_code == 200
    created = resp.json()
    assert created["endpoints"][0]["base_url"] == "http://192.168.1.100:8000"
    assert len((await catalog.snapshot()).channels) == 1


async def test_get_log_rejects_non_jsonl_filename(client):
    resp = await client.get("/admin/logs/admin_auth.py")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "日志文件名不合法"


async def test_get_log_rejects_path_traversal_filename(client):
    resp = await client.get("/admin/logs/../admin_auth.py")

    assert resp.status_code == 404

    resp = await client.get("/admin/logs/..%5Csecret.jsonl")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "日志文件名不合法"


class TestApiKeyExplicitNullUpdate:
    """H9: PUT 显式 null 不得绕过 pydantic 校验写坏持久化数据。"""

    async def test_put_null_key_does_not_corrupt_data(self, client):
        resp = await client.post("/admin/api-keys", json={"name": "k1"})
        assert resp.status_code == 200
        key_id = resp.json()["id"]
        original_key = resp.json()["key"]

        resp = await client.put(f"/admin/api-keys/{key_id}", json={"key": None})
        assert resp.status_code in (400, 500)

        # 数据未被写坏：列表接口仍可用，key 未被替换为 null
        resp = await client.get("/admin/api-keys")
        assert resp.status_code == 200
        item = next(k for k in resp.json() if k["id"] == key_id)
        assert item["key"] == original_key[:8] + "***"

    async def test_put_null_name_rejected_and_list_still_works(self, client):
        resp = await client.post("/admin/api-keys", json={"name": "k2"})
        assert resp.status_code == 200
        key_id = resp.json()["id"]

        resp = await client.put(f"/admin/api-keys/{key_id}", json={"name": None})
        assert resp.status_code in (400, 500)

        resp = await client.get("/admin/api-keys")
        assert resp.status_code == 200
        assert any(k["id"] == key_id and k["name"] == "k2" for k in resp.json())


class TestApiKeyNameUniqueness:
    async def test_post_rejects_duplicate_name(self, client):
        assert (await client.post("/admin/api-keys", json={"name": "production"})).status_code == 200

        resp = await client.post("/admin/api-keys", json={"name": "production"})

        assert resp.status_code == 409
        assert resp.json()["detail"] == "API Key 名称已存在"
        assert len((await client.get("/admin/api-keys")).json()) == 1

    async def test_put_rejects_name_used_by_another_key_case_insensitively(self, client):
        first = await client.post("/admin/api-keys", json={"name": "production"})
        second = await client.post("/admin/api-keys", json={"name": "staging"})

        resp = await client.put(f"/admin/api-keys/{second.json()['id']}", json={"name": " Production "})

        assert first.status_code == 200
        assert second.status_code == 200
        assert resp.status_code == 409
        keys = (await client.get("/admin/api-keys")).json()
        assert next(key for key in keys if key["id"] == second.json()["id"])["name"] == "staging"
