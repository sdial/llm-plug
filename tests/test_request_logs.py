import os
from uuid import uuid4

import pytest
import pytest_asyncio

import request_logs

pytestmark = pytest.mark.asyncio


RAW_FIELDS = {
    "request_headers",
    "response_headers",
    "request_body",
    "response_body",
}


def _sample_record(**overrides):
    payload = {
        "channel_id": "ch_1",
        "channel_name": "Primary",
        "model": "gpt-4o",
        "is_stream": False,
        "input_tokens": 11,
        "output_tokens": 7,
        "latency_ms": 123,
        "success": True,
        "api_key_id": "key_a",
        "request_headers": {"x-app": "test"},
        "response_headers": {"x-request-id": "upstream"},
        "request_body": {"messages": [{"role": "user", "content": "hello"}]},
        "response_body": {"choices": [{"message": {"content": "hi"}}]},
        "lag_ms": 21,
        "finish_reason": "stop",
    }
    payload.update(overrides)
    request_logs.record_request(**payload)


@pytest_asyncio.fixture
async def sqlite_request_logs(tmp_path, monkeypatch):
    await request_logs.close_backend()
    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": False,
            "save_response_headers": False,
            "save_request_body": False,
            "save_response_body": False,
        },
    )
    db_path = tmp_path / "request_logs.db"
    result = await request_logs.init_backend(
        {
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(db_path),
        }
    )
    assert result["available"] is True
    yield db_path
    await request_logs.close_backend()


async def test_sqlite_backend_initializes_writes_and_lists_paginated(sqlite_request_logs):
    _sample_record(channel_id="ch_old", channel_name="Old", model="gpt-old")
    _sample_record(channel_id="ch_new", channel_name="New", model="gpt-new")
    await request_logs.drain_queue()

    page_1 = await request_logs.list_requests(page=1, page_size=1)
    page_2 = await request_logs.list_requests(page=2, page_size=1)

    assert page_1["available"] is True
    assert page_1["total"] == 2
    assert page_1["page"] == 1
    assert page_1["page_size"] == 1
    assert len(page_1["items"]) == 1
    assert page_1["items"][0]["channel_id"] == "ch_new"
    assert page_1["items"][0]["success"] is True
    assert page_1["items"][0]["is_stream"] is False
    assert RAW_FIELDS.isdisjoint(page_1["items"][0])
    assert page_2["items"][0]["channel_id"] == "ch_old"


async def test_start_workers_persist_queued_request_logs(sqlite_request_logs):
    request_logs.start_request_log_workers(worker_count=1)
    try:
        _sample_record(channel_id="ch_worker", channel_name="Worker")
        await request_logs.wait_for_queue()

        result = await request_logs.list_requests()

        assert result["available"] is True
        assert result["total"] == 1
        assert result["items"][0]["channel_id"] == "ch_worker"
    finally:
        await request_logs.stop_request_log_workers()


async def test_save_flags_control_raw_fields(sqlite_request_logs, monkeypatch):
    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": True,
            "save_response_headers": False,
            "save_request_body": True,
            "save_response_body": False,
        },
    )

    _sample_record()
    await request_logs.drain_queue()
    request_id = (await request_logs.list_requests())["items"][0]["id"]

    assert await request_logs.get_request_field(request_id, "request_headers") == {
        "data": {"x-app": "test"}
    }
    assert await request_logs.get_request_field(request_id, "request_body") == {
        "data": {"messages": [{"role": "user", "content": "hello"}]}
    }
    assert await request_logs.get_request_field(request_id, "response_headers") == {"data": None}
    assert await request_logs.get_request_field(request_id, "response_body") == {"data": None}


async def test_list_requests_returns_unavailable_when_backend_is_unavailable():
    await request_logs.close_backend()

    result = await request_logs.list_requests(page=3, page_size=5)

    assert result == {
        "available": False,
        "error": "request log backend is not initialized",
        "items": [],
        "total": 0,
        "page": 3,
        "page_size": 5,
    }


async def test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails(sqlite_request_logs):
    _sample_record(channel_id="ch_keep", channel_name="Keep")
    await request_logs.drain_queue()

    result = await request_logs.reload_backend(
        {
            "request_log_db_type": "postgres",
            "request_log_database_url": "",
        }
    )
    listed = await request_logs.list_requests()

    assert result["available"] is False
    assert "error" in result
    assert listed["available"] is True
    assert listed["total"] == 1
    assert listed["items"][0]["channel_id"] == "ch_keep"


async def test_filters_by_model_channel_time_success_api_key_and_stream(sqlite_request_logs):
    _sample_record(
        channel_id="ch_alpha",
        channel_name="Alpha",
        model="gpt-alpha",
        is_stream=True,
        success=True,
        api_key_id="key_alpha",
    )
    _sample_record(
        channel_id="ch_beta",
        channel_name="Beta",
        model="gpt-beta",
        is_stream=False,
        success=False,
        api_key_id="key_beta",
        error_msg="boom",
    )
    await request_logs.drain_queue()

    assert (await request_logs.list_requests(model="alpha"))["total"] == 1
    assert (await request_logs.list_requests(channel="Beta"))["items"][0]["channel_id"] == "ch_beta"
    assert (await request_logs.list_requests(success=False))["items"][0]["api_key_id"] == "key_beta"
    assert (await request_logs.list_requests(api_key_id="key_alpha"))["items"][0]["model"] == "gpt-alpha"
    assert (await request_logs.list_requests(is_stream=True))["items"][0]["channel_name"] == "Alpha"


async def test_invalid_request_field_returns_none(sqlite_request_logs):
    _sample_record()
    await request_logs.drain_queue()
    request_id = (await request_logs.list_requests())["items"][0]["id"]

    assert await request_logs.get_request_field(request_id, "not_allowed") is None
    assert await request_logs.get_request_field(999999, "request_body") is None


async def test_postgres_backend_smoke(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set")

    await request_logs.close_backend()
    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": True,
            "save_response_headers": False,
            "save_request_body": True,
            "save_response_body": False,
        },
    )
    result = await request_logs.init_backend(
        {
            "request_log_db_type": "postgres",
            "request_log_database_url": database_url,
        }
    )
    if not result["available"]:
        pytest.skip(f"TEST_DATABASE_URL is not reachable: {result.get('error')}")
    assert result["available"] is True
    try:
        model = f"pg-model-{uuid4().hex}"
        _sample_record(channel_id="pg_ch", channel_name="Postgres", model=model)
        await request_logs.drain_queue()
        listed = await request_logs.list_requests(model=model)
        assert listed["available"] is True
        assert listed["total"] == 1
        request_id = listed["items"][0]["id"]
        assert await request_logs.get_request_field(request_id, "request_headers") == {
            "data": {"x-app": "test"}
        }
    finally:
        await request_logs.close_backend()


async def test_cleanup_nullifies_raw_fields_after_raw_retention(sqlite_request_logs, monkeypatch):
    """raw_retention_days=1, retention_days=365: old row's BLOB columns become NULL but row survives."""
    from datetime import datetime, timedelta, timezone

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

    # Write a record with BLOBs, then backdated 8 days ago
    _sample_record(channel_id="ch_x", channel_name="X", model="gpt-x")
    await request_logs.drain_queue()

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(str(sqlite_request_logs))
    conn.execute("UPDATE request_logs SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    result = await request_logs.cleanup_old_records(retention_days=365, raw_retention_days=1)
    assert result["raw_fields_cleared"] == 1
    assert result["rows_deleted"] == 0

    # Row still exists but BLOB columns are NULL
    field = await request_logs.get_request_field(1, "request_body")
    assert field == {"data": None}


async def test_cleanup_deletes_rows_after_retention(sqlite_request_logs):
    """retention_days=1: rows older than 1 day are deleted."""
    from datetime import datetime, timedelta, timezone

    _sample_record(channel_id="ch_old", channel_name="Old", model="gpt-old")
    await request_logs.drain_queue()

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(str(sqlite_request_logs))
    conn.execute("UPDATE request_logs SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    result = await request_logs.cleanup_old_records(retention_days=1, raw_retention_days=0)
    assert result["rows_deleted"] == 1
    assert result["raw_fields_cleared"] == 0

    listing = await request_logs.list_requests()
    assert listing["total"] == 0


async def test_cleanup_skips_raw_nullification_when_raw_days_ge_retention_days(sqlite_request_logs, monkeypatch):
    """When raw_retention_days >= retention_days, Phase 1 is skipped (rows just get deleted)."""
    from datetime import datetime, timedelta, timezone

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

    _sample_record(channel_id="ch_x", channel_name="X", model="gpt-x")
    await request_logs.drain_queue()

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(str(sqlite_request_logs))
    conn.execute("UPDATE request_logs SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    # raw_days == retention_days → Phase 1 skipped, Phase 2 deletes
    result = await request_logs.cleanup_old_records(retention_days=7, raw_retention_days=7)
    assert result["raw_fields_cleared"] == 0
    assert result["rows_deleted"] == 1


async def test_cleanup_zero_days_is_noop(sqlite_request_logs):
    """Both days=0 means no cleanup."""
    _sample_record(channel_id="ch_x", channel_name="X", model="gpt-x")
    await request_logs.drain_queue()

    result = await request_logs.cleanup_old_records(retention_days=0, raw_retention_days=0)
    assert result["raw_fields_cleared"] == 0
    assert result["rows_deleted"] == 0

    listing = await request_logs.list_requests()
    assert listing["total"] == 1
