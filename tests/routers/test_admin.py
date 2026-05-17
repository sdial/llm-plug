import pytest
import pytest_asyncio
import httpx

import request_logs
import stats
from main import app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(tmp_path, monkeypatch):
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
