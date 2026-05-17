from datetime import date

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
