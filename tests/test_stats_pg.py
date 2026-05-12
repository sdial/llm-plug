import asyncio
import json
import os
import pytest
import pytest_asyncio
import asyncpg
from datetime import date, datetime, timedelta

import stats

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.asyncio


async def _create_pool():
    """创建带 JSONB 编解码器的连接池"""
    pool = await asyncpg.create_pool(TEST_DB_URL)
    async with pool.acquire() as conn:
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )
    return pool


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(stats, "DATABASE_URL", TEST_DB_URL)
    # 清理并重新初始化
    pool = await _create_pool()
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
    await pool.close()
    await stats.init_db()
    yield
    await stats.close_pool()


class TestInitDb:
    async def test_creates_tables(self):
        pool = await _create_pool()
        async with pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
            names = {r["table_name"] for r in tables}
            assert "requests" in names
            assert "daily_stats" in names
        await pool.close()


class TestRecordRequest:
    async def test_inserts_request_row(self):
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Test Channel",
            model="gpt-4",
            is_stream=False,
            input_tokens=100,
            output_tokens=50,
            latency_ms=200,
            success=True,
            api_key_id="key_1",
            request_headers={"X-App-Name": "TestApp", "User-Agent": "test-agent"},
            lag_ms=50,
            finish_reason="stop",
        )
        pool = await _create_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM requests")
            assert row["channel_id"] == "ch_1"
            assert row["model"] == "gpt-4"
            assert row["request_headers"]["X-App-Name"] == "TestApp"
            assert row["lag_ms"] == 50
            assert row["finish_reason"] == "stop"
        await pool.close()

    async def test_headers_case_insensitive(self):
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            request_headers={"x-app-name": "LowerCaseApp"},
        )
        pool = await _create_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT request_headers FROM requests")
            assert row["request_headers"]["x-app-name"] == "LowerCaseApp"
        await pool.close()


class TestListRequests:
    async def test_empty_result(self):
        result = await stats.list_requests(page=1, page_size=10)
        assert result["items"] == []
        assert result["total"] == 0
        assert result["page"] == 1
        assert result["page_size"] == 10

    async def test_pagination(self):
        for i in range(15):
            await asyncio.sleep(0.01)
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
        result = await stats.list_requests(page=1, page_size=10)
        assert len(result["items"]) == 10
        assert result["total"] == 15
        # 验证按 timestamp DESC 排序：第一条应该是最新插入的（ch_14）
        assert result["items"][0]["channel_id"] == "ch_14"

        result = await stats.list_requests(page=2, page_size=10)
        assert len(result["items"]) == 5
        assert result["total"] == 15
        assert result["items"][0]["channel_id"] == "ch_4"

    async def test_filter_by_model(self):
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
        result = await stats.list_requests(model="gpt-4")
        assert result["total"] == 1
        assert result["items"][0]["model"] == "gpt-4"

    async def test_filter_by_success(self):
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
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=False,
        )
        result = await stats.list_requests(success=True)
        assert result["total"] == 1
        assert result["items"][0]["success"] is True

        result = await stats.list_requests(success=False)
        assert result["total"] == 1
        assert result["items"][0]["success"] is False

    async def test_filter_by_channel(self):
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Alpha",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        await stats.record_request(
            channel_id="ch_2",
            channel_name="Beta",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
        result = await stats.list_requests(channel="Alpha")
        assert result["total"] == 1
        assert result["items"][0]["channel_name"] == "Alpha"

    async def test_filter_by_is_stream(self):
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=True,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
        )
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
        result = await stats.list_requests(is_stream=True)
        assert result["total"] == 1
        assert result["items"][0]["is_stream"] is True

    async def test_combined_filters(self):
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
        result = await stats.list_requests(model="gpt-4", success=True)
        assert result["total"] == 1
        assert result["items"][0]["model"] == "gpt-4"

    async def test_filter_by_time_range(self):
        now = datetime.now()
        old_time = now - timedelta(hours=2)
        recent_time = now - timedelta(minutes=5)

        pool = await _create_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers, is_stream,
                 input_tokens, output_tokens, latency_ms, lag_ms, finish_reason, success, error_msg)
                VALUES ($1, 'gpt-4', 'ch_old', 'Old', NULL, '{}', false, 10, 5, 100, NULL, 'stop', true, NULL)
                """,
                old_time,
            )
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers, is_stream,
                 input_tokens, output_tokens, latency_ms, lag_ms, finish_reason, success, error_msg)
                VALUES ($1, 'gpt-4', 'ch_recent', 'Recent', NULL, '{}', false, 10, 5, 100, NULL, 'stop', true, NULL)
                """,
                recent_time,
            )
        await pool.close()

        result = await stats.list_requests(start=now - timedelta(hours=1))
        assert result["total"] == 1
        assert result["items"][0]["channel_id"] == "ch_recent"

        result = await stats.list_requests(end=now - timedelta(hours=1))
        assert result["total"] == 1
        assert result["items"][0]["channel_id"] == "ch_old"

    async def test_filter_by_api_key_id(self):
        await stats.record_request(
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
        await stats.record_request(
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
        result = await stats.list_requests(api_key_id="key_alpha")
        assert result["total"] == 1
        assert result["items"][0]["api_key_id"] == "key_alpha"

    async def test_list_requests_no_jsonb_fields(self):
        """list_requests 不应返回 request_headers, response_headers, request_body, response_body"""
        await stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=100,
            success=True,
            request_headers={"X-App": "Test"},
            request_body={"messages": []},
            response_headers={"X-Resp": "Test"},
            response_body={"choices": []},
        )
        result = await stats.list_requests(page=1, page_size=10)
        assert result["total"] == 1
        item = result["items"][0]
        assert "request_headers" not in item
        assert "response_headers" not in item
        assert "request_body" not in item
        assert "response_body" not in item
        assert "model" in item
        assert "channel_id" in item


class TestGetRequestField:
    async def test_get_request_headers(self):
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
        result = await stats.get_request_field(req_id, "request_headers")
        assert result["data"]["X-App-Name"] == "TestApp"

    async def test_get_request_body(self):
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
        result = await stats.get_request_field(req_id, "request_body")
        assert result["data"]["messages"][0]["content"] == "hi"

    async def test_get_null_field(self):
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
        result = await stats.get_request_field(req_id, "response_body")
        assert result["data"] is None

    async def test_nonexistent_id(self):
        result = await stats.get_request_field(999999, "request_headers")
        assert result is None

    async def test_invalid_field(self):
        result = await stats.get_request_field(1, "invalid_field")
        assert result is None


class TestTimezoneOffset:
    async def test_daily_aggregation_respects_utc8_boundary(self):
        """验证日聚合按东8区日期归类：UTC 15:00 = 东8区 23:00 归入同一天，UTC 01:00 = 东8区 09:00"""
        pool = await _create_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers,
                 is_stream, input_tokens, output_tokens, latency_ms, success, error_msg)
                VALUES
                ($1, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL),
                ($2, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL)
                """,
                datetime(2026, 5, 2, 15, 0, 0),
                datetime(2026, 5, 2, 1, 0, 0),
            )
        await pool.close()

        result = await stats.aggregate_daily_stats(date(2026, 5, 2), date(2026, 5, 2))
        assert result["updated_rows"] >= 1

        raw_daily = await stats.get_daily_stats(days=1)
        assert len(raw_daily) >= 1
        day_data = next(
            (d for d in raw_daily if str(d.get("date")) == "2026-05-02"), None
        )
        assert day_data is not None
        assert day_data["request_count"] == 2

    async def test_daily_aggregation_cross_day_boundary(self):
        """UTC 16:00 = 东8区 00:00 次日，应归入次日的聚合行"""
        pool = await _create_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers,
                 is_stream, input_tokens, output_tokens, latency_ms, success, error_msg)
                VALUES
                ($1, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL)
                """,
                datetime(2026, 5, 2, 16, 0, 0),
            )
        await pool.close()

        result = await stats.aggregate_daily_stats(date(2026, 5, 2), date(2026, 5, 3))
        assert result["updated_rows"] >= 1

        raw_daily = await stats.get_daily_stats(days=2)
        may3_data = next(
            (d for d in raw_daily if str(d.get("date")) == "2026-05-03"), None
        )
        assert may3_data is not None
        assert may3_data["request_count"] == 1


class TestRefreshStats:
    async def test_refresh_stats_backfills_and_refreshes_recent(self):
        """验证 refresh_stats 补全历史 + 强制刷新近3天"""
        pool = await _create_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers,
                 is_stream, input_tokens, output_tokens, latency_ms, success, error_msg)
                VALUES
                ($1, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL),
                ($2, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 200, 100, 300, true, NULL),
                ($3, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 50, 25, 100, true, NULL)
                """,
                datetime(2026, 4, 28, 12, 0, 0),
                datetime(2026, 5, 1, 12, 0, 0),
                datetime.utcnow(),
            )
        await pool.close()

        result = await stats.refresh_stats()
        assert result["recent_refreshed_days"] == 3

        raw_daily = await stats.get_daily_stats(days=7)
        assert len(raw_daily) >= 1
