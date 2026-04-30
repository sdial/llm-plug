import os
import pytest
import pytest_asyncio
import asyncpg
from datetime import datetime, timedelta

import stats_pg

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(stats_pg, "DATABASE_URL", TEST_DB_URL)
    # 清理并重新初始化
    pool = await asyncpg.create_pool(TEST_DB_URL)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS hourly_stats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
    await pool.close()
    await stats_pg.init_db()
    yield
    await stats_pg.close_pool()


class TestInitDb:
    async def test_creates_tables(self):
        pool = await asyncpg.create_pool(TEST_DB_URL)
        async with pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
            names = {r["table_name"] for r in tables}
            assert "requests" in names
            assert "hourly_stats" in names
            assert "daily_stats" in names
        await pool.close()


class TestRecordRequest:
    async def test_inserts_request_row(self):
        await stats_pg.record_request(
            channel_id="ch_1",
            channel_name="Test Channel",
            model="gpt-4",
            is_stream=False,
            input_tokens=100,
            output_tokens=50,
            latency_ms=200,
            success=True,
            api_key_id="key_1",
            headers={"X-App-Name": "TestApp", "User-Agent": "test-agent"},
            lag_ms=50,
            finish_reason="stop",
        )
        pool = await asyncpg.create_pool(TEST_DB_URL)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM requests")
            assert row["channel_id"] == "ch_1"
            assert row["model"] == "gpt-4"
            assert row["headers"]["X-App-Name"] == "TestApp"
            assert row["lag_ms"] == 50
            assert row["finish_reason"] == "stop"
        await pool.close()

    async def test_headers_case_insensitive(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5,
            latency_ms=100, success=True,
            headers={"x-app-name": "LowerCaseApp"},
        )
        pool = await asyncpg.create_pool(TEST_DB_URL)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT headers FROM requests")
            assert row["headers"]["X-App-Name"] == "LowerCaseApp"
        await pool.close()


class TestAggregation:
    async def test_hourly_aggregation(self):
        now = datetime.now()
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        await stats_pg.record_request("ch_1", "Test", "gpt-4", False, 100, 50, 200, True)
        await stats_pg.record_request("ch_1", "Test", "gpt-4", False, 200, 100, 300, True)

        result = await stats_pg.aggregate_hourly_stats(hour_start, hour_start + timedelta(hours=1))
        assert result["updated_rows"] >= 1

        stats_result = await stats_pg.get_hourly_stats(start_time=hour_start)
        assert len(stats_result) >= 1
        assert stats_result[0]["request_count"] == 2
        assert stats_result[0]["input_tokens"] == 300
        assert stats_result[0]["output_tokens"] == 150


class TestCleanup:
    async def test_cleanup_old_data(self):
        await stats_pg.record_request("ch_1", "Test", "gpt-4", False, 10, 5, 100, True)
        deleted = await stats_pg.cleanup_old_data(keep_days=0)
        assert deleted >= 1
        pool = await asyncpg.create_pool(TEST_DB_URL)
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM requests")
            assert count == 0
        await pool.close()


class TestListRequests:
    async def test_empty_result(self):
        result = await stats_pg.list_requests(page=1, page_size=10)
        assert result["items"] == []
        assert result["total"] == 0
        assert result["page"] == 1
        assert result["page_size"] == 10

    async def test_pagination(self):
        for i in range(15):
            await stats_pg.record_request(
                channel_id=f"ch_{i}", channel_name=f"Channel {i}", model="gpt-4",
                is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            )
        result = await stats_pg.list_requests(page=1, page_size=10)
        assert len(result["items"]) == 10
        assert result["total"] == 15

        result = await stats_pg.list_requests(page=2, page_size=10)
        assert len(result["items"]) == 5
        assert result["total"] == 15

    async def test_filter_by_model(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-3.5",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(model="gpt-4")
        assert result["total"] == 1
        assert result["items"][0]["model"] == "gpt-4"

    async def test_filter_by_success(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=False,
        )
        result = await stats_pg.list_requests(success=True)
        assert result["total"] == 1
        assert result["items"][0]["success"] is True

        result = await stats_pg.list_requests(success=False)
        assert result["total"] == 1
        assert result["items"][0]["success"] is False

    async def test_filter_by_channel(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Alpha", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_2", channel_name="Beta", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(channel="Alpha")
        assert result["total"] == 1
        assert result["items"][0]["channel_name"] == "Alpha"

    async def test_filter_by_is_stream(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=True, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(is_stream=True)
        assert result["total"] == 1
        assert result["items"][0]["is_stream"] is True

    async def test_combined_filters(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-3.5",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(model="gpt-4", success=True)
        assert result["total"] == 1
        assert result["items"][0]["model"] == "gpt-4"
