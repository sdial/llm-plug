import os
import pytest
import pytest_asyncio
import asyncpg
import httpx

import stats
from main import app

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(stats, "DATABASE_URL", TEST_DB_URL)
    # Directly clean tables using a short-lived pool
    pool = await asyncpg.create_pool(TEST_DB_URL)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
    await pool.close()
    # Reset stats global state so init_db() runs fresh inside the test loop
    monkeypatch.setattr(stats, "_pool", None)
    monkeypatch.setattr(stats, "_db_available", False)
    await stats.init_db()
    yield
    await stats.close_pool()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestListRequestsEndpoint:
    async def test_returns_empty_list(self, client):
        resp = await client.get("/admin/requests")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_pagination(self, client):
        for i in range(15):
            await stats.record_request(
                channel_id=f"ch_{i}",
                channel_name=f"Channel {i}",
                model="gpt-4",
                is_stream=False,
                input_tokens=10,
                output_tokens=5,
                latency_ms=100,
                success=True,
            )
        resp = await client.get("/admin/requests?page=1&page_size=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 10
        assert data["total"] == 15

        resp = await client.get("/admin/requests?page=2&page_size=10")
        data = resp.json()
        assert len(data["items"]) == 5

    async def test_filter_by_model(self, client):
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-3.5",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        resp = await client.get("/admin/requests?model=gpt-4")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["model"] == "gpt-4"


class TestRequestFieldEndpoints:
    async def test_get_request_headers(self, client):
        await stats.record_request(
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
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/request-headers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["X-App-Name"] == "TestApp"

    async def test_get_request_body(self, client):
        await stats.record_request(
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
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/request-body")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["messages"][0]["content"] == "hi"

    async def test_get_response_headers(self, client):
        await stats.record_request(
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
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-headers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["X-RateLimit"] == "100"

    async def test_get_response_body(self, client):
        await stats.record_request(
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
        all_reqs = await stats.list_requests(page=1, page_size=1)
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
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-body")
        assert resp.status_code == 200
        assert resp.json()["data"] is None
