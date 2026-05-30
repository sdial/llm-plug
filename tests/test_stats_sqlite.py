import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

import stats

pytestmark = pytest.mark.asyncio


RAW_FIELDS = {
    "request_headers",
    "response_headers",
    "request_body",
    "response_body",
}


@pytest_asyncio.fixture(autouse=True)
async def sqlite_stats_db(tmp_path):
    db_path = tmp_path / "stats.db"
    await stats.close_pool()
    await stats.init_db(str(db_path))
    yield db_path
    await stats.stop_stats_workers()
    await stats.close_pool()


def _record_sample(**overrides):
    payload = {
        "channel_id": "ch_1",
        "channel_name": "Primary",
        "model": "gpt-4o",
        "is_stream": False,
        "input_tokens": 12,
        "output_tokens": 8,
        "latency_ms": 120,
        "success": True,
        "api_key_id": "key_a",
        "request_headers": {"authorization": "Bearer secret"},
        "response_headers": {"x-request-id": "upstream"},
        "request_body": {"messages": [{"role": "user", "content": "hello"}]},
        "response_body": {"choices": [{"message": {"content": "hi"}}]},
        "lag_ms": 25,
        "finish_reason": "stop",
    }
    payload.update(overrides)
    stats.record_request(**payload)


async def test_init_db_creates_sqlite_tables():
    assert await stats._list_tables_for_test() == {
        "daily_stats",
        "hourly_stats",
        "request_stats_raw",
    }


async def test_record_request_writes_lightweight_row_and_list_requests_omits_raw_fields():
    _record_sample()
    await stats.drain_queue()

    result = await stats.list_requests()

    assert result["total"] == 1
    item = result["items"][0]
    assert item["channel_id"] == "ch_1"
    assert item["channel_name"] == "Primary"
    assert item["model"] == "gpt-4o"
    assert item["api_key_id"] == "key_a"
    assert item["is_stream"] is False
    assert item["input_tokens"] == 12
    assert item["output_tokens"] == 8
    assert item["latency_ms"] == 120
    assert item["lag_ms"] == 25
    assert item["finish_reason"] == "stop"
    assert item["success"] is True
    assert RAW_FIELDS.isdisjoint(item)


async def test_aggregate_daily_stats_refreshes_daily_stats():
    _record_sample(model="gpt-4o-mini", input_tokens=20, output_tokens=5)
    await stats.drain_queue()

    result = await stats.aggregate_daily_stats(date.today(), date.today())
    daily = await stats.get_daily_stats(days=1, model="gpt-4o-mini")

    assert result["updated_rows"] == 1
    assert len(daily) == 1
    assert daily[0]["request_count"] == 1
    assert daily[0]["success_count"] == 1
    assert daily[0]["fail_count"] == 0
    assert daily[0]["input_tokens"] == 20
    assert daily[0]["output_tokens"] == 5


async def test_refresh_missing_daily_stats_uses_timestamp_index_for_date_cutoff(sqlite_stats_db, monkeypatch):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=3)).replace(tzinfo=None)
    conn = sqlite3.connect(str(sqlite_stats_db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO request_stats_raw
        (timestamp, model, channel_id, channel_name, api_key_id, client_ip, is_stream,
         input_tokens, output_tokens, latency_ms, lag_ms, finish_reason, success, error_msg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stats._to_iso(old_ts),
            "gpt-index",
            "ch_index",
            "Index",
            "key_index",
            "127.0.0.1",
            0,
            1,
            1,
            10,
            None,
            "stop",
            1,
            None,
        ),
    )
    conn.commit()

    traced_sql: list[str] = []
    conn.set_trace_callback(
        lambda sql: traced_sql.append(sql)
        if "SELECT DISTINCT" in sql and "FROM request_stats_raw" in sql
        else None
    )
    monkeypatch.setattr(stats, "_connect", lambda: conn)

    stats._refresh_missing_daily_stats_sync()

    assert traced_sql
    plan_rows = conn.execute(f"EXPLAIN QUERY PLAN {traced_sql[0]}").fetchall()
    plan_text = " ".join(row[3] for row in plan_rows)
    assert "SEARCH request_stats_raw USING" in plan_text
    assert "idx_request_stats_raw_timestamp" in plan_text


async def test_get_request_field_returns_none_for_raw_fields():
    _record_sample()
    await stats.drain_queue()
    request_id = (await stats.list_requests())["items"][0]["id"]

    assert await stats.get_request_field(request_id, "request_body") is None


async def test_list_requests_supports_basic_filters():
    _record_sample(
        channel_id="ch_alpha",
        channel_name="Alpha",
        model="gpt-alpha",
        is_stream=True,
        success=True,
        api_key_id="key_alpha",
    )
    _record_sample(
        channel_id="ch_beta",
        channel_name="Beta",
        model="gpt-beta",
        is_stream=False,
        success=False,
        api_key_id="key_beta",
        error_msg="boom",
    )
    await stats.drain_queue()

    model_result = await stats.list_requests(model="alpha")
    assert model_result["total"] == 1
    assert model_result["items"][0]["model"] == "gpt-alpha"

    success_result = await stats.list_requests(success=False)
    assert success_result["total"] == 1
    assert success_result["items"][0]["channel_id"] == "ch_beta"

    channel_result = await stats.list_requests(channel="Alpha")
    assert channel_result["total"] == 1
    assert channel_result["items"][0]["channel_name"] == "Alpha"

    stream_result = await stats.list_requests(is_stream=True)
    assert stream_result["total"] == 1
    assert stream_result["items"][0]["is_stream"] is True

    key_result = await stats.list_requests(api_key_id="key_beta")
    assert key_result["total"] == 1
    assert key_result["items"][0]["api_key_id"] == "key_beta"
