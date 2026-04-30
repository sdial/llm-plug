import os
import pytest
import pytest_asyncio
import asyncpg
import httpx

import stats_pg
from main import app

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(stats_pg, "DATABASE_URL", TEST_DB_URL)
    # Directly clean tables using a short-lived pool
    pool = await asyncpg.create_pool(TEST_DB_URL)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS hourly_stats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
    await pool.close()
    # Reset stats_pg global state so init_db() runs fresh inside the test loop
    monkeypatch.setattr(stats_pg, "_pool", None)
    monkeypatch.setattr(stats_pg, "_db_available", False)
    await stats_pg.init_db()
    yield
    await stats_pg.close_pool()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestListRequestsEndpoint:
    async def test_returns_empty_list(self, client):
        resp = await client.get("/admin/requests")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_pagination_and_filtering(self, client):
        for i in range(15):
            await stats_pg.record_request(
                channel_id=f"ch_{i}", channel_name=f"Channel {i}", model="gpt-4",
                is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            )
        resp = await client.get("/admin/requests?page=1&page_size=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 10
        assert data["total"] == 15

        resp = await client.get("/admin/requests?page=2&page_size=10")
        data = resp.json()
        assert len(data["items"]) == 5
