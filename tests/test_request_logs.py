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
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
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
    assert page_1["items"][0]["cache_read_input_tokens"] == 0
    assert page_1["items"][0]["cache_creation_input_tokens"] == 0
    assert RAW_FIELDS.isdisjoint(page_1["items"][0])
    assert page_2["items"][0]["channel_id"] == "ch_old"


async def test_sqlite_backend_records_cache_token_details(sqlite_request_logs):
    _sample_record(
        input_tokens=1200,
        output_tokens=80,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=40,
    )
    await request_logs.drain_queue()

    result = await request_logs.list_requests()

    assert result["total"] == 1
    item = result["items"][0]
    assert item["input_tokens"] == 1200
    assert item["output_tokens"] == 80
    assert item["cache_read_input_tokens"] == 900
    assert item["cache_creation_input_tokens"] == 40


async def test_sqlite_backend_migrates_existing_db_for_cache_token_columns(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "request_logs_old.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            model TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            api_key_id TEXT,
            client_ip TEXT,
            is_stream INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL,
            lag_ms INTEGER,
            finish_reason TEXT,
            success INTEGER NOT NULL,
            error_msg TEXT,
            request_headers TEXT,
            response_headers TEXT,
            request_body TEXT,
            response_body TEXT
        );
        """
    )
    conn.close()

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
    result = await request_logs.init_backend(
        {
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(db_path),
        }
    )
    assert result["available"] is True

    _sample_record(cache_read_input_tokens=12, cache_creation_input_tokens=3)
    await request_logs.drain_queue()
    listed = await request_logs.list_requests()

    assert listed["items"][0]["cache_read_input_tokens"] == 12
    assert listed["items"][0]["cache_creation_input_tokens"] == 3


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

    import request_logs as _rl

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

    # Find the monthly db file that actually holds the data
    backend = _rl._backend
    current_ym = backend._current_year_month()
    month_db = backend._month_db_path(current_ym)

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(month_db)
    conn.execute("UPDATE request_logs SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    result = await request_logs.cleanup_old_records(retention_days=365, raw_retention_days=1)
    assert result["raw_fields_cleared"] == 1
    assert result["rows_deleted"] == 0

    # Row still exists but BLOB columns are NULL
    listed = await request_logs.list_requests()
    request_id = listed["items"][0]["id"]
    field = await request_logs.get_request_field(request_id, "request_body")
    assert field == {"data": None}


async def test_cleanup_deletes_rows_after_retention(sqlite_request_logs):
    """retention_days=1: rows older than 1 day are deleted."""
    from datetime import datetime, timedelta, timezone

    import request_logs as _rl

    _sample_record(channel_id="ch_old", channel_name="Old", model="gpt-old")
    await request_logs.drain_queue()

    backend = _rl._backend
    current_ym = backend._current_year_month()
    month_db = backend._month_db_path(current_ym)

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(month_db)
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

    import request_logs as _rl

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

    backend = _rl._backend
    current_ym = backend._current_year_month()
    month_db = backend._month_db_path(current_ym)

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(month_db)
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


# ---------------------------------------------------------------------------
# Rotating monthly tests
# ---------------------------------------------------------------------------


def test_encode_rotating_id():
    assert request_logs._encode_rotating_id("202606", 12345) == "202606_12345"
    assert request_logs._encode_rotating_id("202601", 1) == "202601_1"


def test_decode_rotating_id():
    assert request_logs._decode_rotating_id("202606_12345") == ("202606", 12345)
    assert request_logs._decode_rotating_id("202601_1") == ("202601", 1)

    import pytest as _pytest
    with _pytest.raises(ValueError):
        request_logs._decode_rotating_id("bad")
    with _pytest.raises(ValueError):
        request_logs._decode_rotating_id("12345_1")  # not 6 digits


def test_parse_id_compound():
    ym, lid = request_logs._parse_id("202606_12345")
    assert ym == "202606"
    assert lid == 12345


def test_parse_id_plain_integer():
    ym, lid = request_logs._parse_id("999")
    assert ym is None
    assert lid == 999


async def test_monthly_files_created_on_init(sqlite_request_logs):
    """After init, a monthly file for the current month should exist."""
    import glob

    backend = request_logs._backend
    pattern = os.path.join(backend.data_dir, "request_logs_????_??.db")
    files = glob.glob(pattern)
    assert len(files) >= 1


async def test_write_routes_to_current_month(sqlite_request_logs):
    """Records are written to the current month's .db file."""
    _sample_record(channel_id="ch_month", channel_name="Month")
    await request_logs.drain_queue()

    result = await request_logs.list_requests()
    assert result["total"] == 1

    # ID should be a compound rotating ID
    rid = result["items"][0]["id"]
    ym, lid = request_logs._decode_rotating_id(rid)
    assert len(ym) == 6
    assert lid >= 1


async def test_get_request_field_with_compound_id(sqlite_request_logs):
    """get_request_field works with compound rotating IDs."""
    import request_logs as _rl

    monkeypatch_set = _rl._get_save_flags  # noqa: F841

    # Enable saving request_body
    import request_logs as _rl_mod
    original = _rl_mod._get_save_flags
    _rl_mod._get_save_flags = lambda: {
        "save_request_headers": False,
        "save_response_headers": False,
        "save_request_body": True,
        "save_response_body": False,
    }
    try:
        _sample_record(request_body={"test": "value"})
        await request_logs.drain_queue()

        result = await request_logs.list_requests()
        rid = result["items"][0]["id"]

        field = await request_logs.get_request_field(rid, "request_body")
        assert field == {"data": {"test": "value"}}
    finally:
        _rl_mod._get_save_flags = original


async def test_cross_month_query(sqlite_request_logs):
    """Querying across months merges results correctly."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    import request_logs as _rl

    backend = _rl._backend

    # Write a record for current month
    _sample_record(channel_id="ch_current", channel_name="Current", model="gpt-now")
    await request_logs.drain_queue()

    # Write a record and backdate it to last month
    _sample_record(channel_id="ch_old", channel_name="Old", model="gpt-old")
    await request_logs.drain_queue()

    # Find current month db and backdate one record
    current_ym = backend._current_year_month()
    month_db = backend._month_db_path(current_ym)

    # Backdate the second record to appear as last month
    now = datetime.now(timezone.utc)
    if now.month == 1:
        last_month_ym = f"{now.year - 1}12"
    else:
        last_month_ym = f"{now.year}{now.month - 1:02d}"

    last_month_db = backend._month_db_path(last_month_ym)

    # Move the "old" record to last month's db
    conn = sqlite3.connect(month_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM request_logs WHERE channel_id = 'ch_old'").fetchone()
    if row:
        old_data = dict(row)
        old_ts = f"{last_month_ym[:4]}-{last_month_ym[4:]}-15 10:00:00.000000"
        old_data["timestamp"] = old_ts
        conn.execute("DELETE FROM request_logs WHERE id = ?", (old_data["id"],))
        conn.commit()
    conn.close()

    # Insert into last month's db
    if old_data:
        last_month_path = backend._ensure_month_db(last_month_ym)
        conn2 = sqlite3.connect(last_month_path)
        conn2.execute(
            """INSERT INTO request_logs
            (id, timestamp, model, channel_id, channel_name, api_key_id, client_ip,
             is_stream, input_tokens, output_tokens, cache_read_input_tokens,
             cache_creation_input_tokens, latency_ms, lag_ms, finish_reason,
             success, error_msg, request_headers, response_headers,
             request_body, response_body)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                old_data["id"], old_ts, old_data["model"], old_data["channel_id"],
                old_data["channel_name"], old_data.get("api_key_id"), old_data.get("client_ip"),
                old_data["is_stream"], old_data["input_tokens"], old_data["output_tokens"],
                old_data.get("cache_read_input_tokens", 0), old_data.get("cache_creation_input_tokens", 0),
                old_data["latency_ms"], old_data.get("lag_ms"), old_data.get("finish_reason"),
                old_data["success"], old_data.get("error_msg"),
                old_data.get("request_headers"), old_data.get("response_headers"),
                old_data.get("request_body"), old_data.get("response_body"),
            ),
        )
        conn2.commit()
        conn2.close()

    # Query all - should get both records
    result = await request_logs.list_requests(page_size=100)
    assert result["total"] == 2

    # Query with model filter
    result = await request_logs.list_requests(model="gpt-now")
    assert result["total"] == 1
    assert result["items"][0]["channel_id"] == "ch_current"


async def test_cleanup_deletes_old_monthly_files(sqlite_request_logs):
    """cleanup_old_records deletes entire monthly db files older than retention."""
    import glob

    import request_logs as _rl

    backend = _rl._backend

    # Create a fake old monthly db
    old_ym = "202001"
    old_path = backend._ensure_month_db(old_ym)

    # Insert a dummy record
    import sqlite3
    conn = sqlite3.connect(old_path)
    conn.execute(
        """INSERT INTO request_logs (timestamp, model, channel_id, channel_name, is_stream,
        input_tokens, output_tokens, latency_ms, success)
        VALUES ('2020-01-15 00:00:00', 'old-model', 'ch', 'Old', 0, 0, 0, 0, 1)""")
    conn.commit()
    conn.close()

    # Verify file exists
    assert os.path.exists(old_path)

    # Cleanup with 365 days retention should delete the 2020-01 file
    result = await request_logs.cleanup_old_records(retention_days=365, raw_retention_days=0)
    assert result["files_deleted"] >= 1
    assert not os.path.exists(old_path)
