import json

import pytest
import pytest_asyncio
import httpx

import config
import request_logs
from routers import admin
import stats
import storage
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
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None

    import main
    monkeypatch.setattr(main, "_whitelist_cache", main._whitelist.WhitelistCache(str(data_dir / "whitelist.csv")))

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
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(tmp_path / "request_logs.db"),
        }
    )
    yield
    await stats.close_pool()
    await request_logs.close_backend()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
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
        async def fake_list_requests(**kwargs):
            return {
                "items": [{"id": 1, "model": "claude", "channel_id": "anth", "channel_name": "Anthropic"}],
                "total": 1,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
            }

        async def fake_load_data():
            return {
                "channels": [
                    {
                        "id": "anth",
                        "name": "Anthropic",
                        "api_type": "anthropic",
                        "base_url": "https://api.anthropic.com",
                        "api_key": "sk-test",
                        "models": ["claude"],
                    }
                ]
            }

        from routers import admin

        original_list = admin.request_log_list_requests
        original_load_data = admin.load_data
        admin.request_log_list_requests = fake_list_requests
        admin.load_data = fake_load_data
        try:
            resp = await client.get("/admin/requests")
        finally:
            admin.request_log_list_requests = original_list
            admin.load_data = original_load_data
        assert resp.status_code == 200
        assert resp.json()["items"][0]["api_type"] == "anthropic"

    async def test_request_log_backend_unavailable_returns_503(self, client):
        async def fake_list_requests(**kwargs):
            return {
                "available": False,
                "error": "PostgreSQL unavailable",
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
        assert "PostgreSQL unavailable" in resp.json()["detail"]

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
            json={"request_log_db_type": "sqlite"},
        )

        assert resp.status_code == 200
        assert calls == ["reload"]


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
    monkeypatch.setattr(
        admin.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

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


async def test_fetch_models_falls_back_to_base_url_when_advanced_models_url_missing(
    client, monkeypatch
):
    captured = {}
    monkeypatch.setattr(
        admin.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

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


async def test_fetch_models_rejects_private_upstream_url(client, monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SSRF validation should reject before httpx is created")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    resp = await client.post(
        "/admin/channels/fetch-models",
        json={
            "base_url": "http://127.0.0.1:8000",
            "models_url": "",
            "api_key": "sk-test",
            "api_type": "openai-chat-completions",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "不允许访问内网或本机地址"


async def test_fetch_models_rejects_hostname_resolving_to_non_public_ip(client, monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SSRF validation should reject before httpx is created")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    def fake_getaddrinfo(*args, **kwargs):
        return [(None, None, None, "", ("100.64.0.1", 443))]

    monkeypatch.setattr(admin.socket, "getaddrinfo", fake_getaddrinfo)

    resp = await client.post(
        "/admin/channels/fetch-models",
        json={
            "base_url": "https://api.example.com",
            "models_url": "",
            "api_key": "sk-test",
            "api_type": "openai-chat-completions",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "不允许访问内网或本机地址"


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
