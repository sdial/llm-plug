import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from loguru import logger

import config
import db_write_behind
import request_logs
import stats


@pytest.fixture
def captured_logs():
    records = []
    handler_id = logger.add(records.append, format="{message}")
    yield records
    logger.remove(handler_id)


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
        "client_ip": "203.0.113.10",
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
        "request_headers": {"x-app": "test"},
        "response_headers": {"x-request-id": "upstream"},
        "request_body": {"messages": [{"role": "user", "content": "hello"}]},
        "response_body": {"choices": [{"message": {"content": "hi"}}]},
        "lag_ms": 21,
        "finish_reason": "stop",
    }
    payload.update(overrides)
    request_logs.record_request(**payload)


# ─── 查询 SQL 语义直测缝（ADR-0017 D3 / 工单04）───
# 后端直实例化（tmp 目录）直测 list_requests / get_request_field 的查询语义：
# 种子经公开 write_record 直写（不入队），摆脱「init 全局 + drain 异步等待」仪式。
# 只断言外部可观察行为（结果集 / 汇总数字 / 分页边界 / 复合 id 形态），不断言 SQL 字符串本身。


def _query_backend(tmp_path) -> request_logs.SQLiteRequestLogBackend:
    """后端直实例化（tmp 目录）：查询 SQL 语义测试的预约定接缝（ADR-0017 D3）。"""
    return request_logs.SQLiteRequestLogBackend(str(tmp_path / "request_logs.db"))


def _backend_record(**overrides) -> dict:
    """后端直写记录（与模块入队负载同字段集，不含 save-flags 过滤的 raw 字段）。"""
    record = {
        "timestamp": datetime.now(UTC),
        "channel_id": "ch_1",
        "channel_name": "Primary",
        "model": "gpt-4o",
        "is_stream": False,
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
        "latency_ms": 123,
        "lag_ms": 21,
        "finish_reason": "stop",
        "success": True,
        "api_key_id": "key_a",
        "client_ip": "203.0.113.10",
    }
    record.update(overrides)
    return record


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
            "request_log_sqlite_path": str(db_path),
        }
    )
    assert result["available"] is True
    yield db_path
    await request_logs.close_backend()


@pytest.mark.asyncio
async def test_sqlite_backend_initializes_writes_and_lists_paginated(
    sqlite_request_logs,
):
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
    assert page_1["items"][0]["client_ip"] == "203.0.113.10"
    assert page_1["items"][0]["cache_read_input_tokens"] == 3
    assert page_1["items"][0]["cache_creation_input_tokens"] == 2
    assert RAW_FIELDS.isdisjoint(page_1["items"][0])
    assert page_2["items"][0]["channel_id"] == "ch_old"


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_request_log_queue_wraps_shared_write_behind_queue():
    """日志侧经共享接线句柄（WriteBehindWiring）持有队列：ensure_queue 返回 WriteBehindQueue 实例且同 loop 复用。"""
    queue = request_logs._wiring.ensure_queue()
    try:
        assert isinstance(queue, db_write_behind.WriteBehindQueue)
        assert request_logs._wiring.ensure_queue() is queue
    finally:
        await request_logs.close_backend()


@pytest.mark.asyncio
async def test_request_log_write_callback_binds_backend_late(sqlite_request_logs, captured_logs):
    """写回调在消费时晚绑定 _backend：backend 中途不可用 → 丢弃该 record + 告警，不抛错。"""
    writes = []

    class FakeBackend:
        async def write_record(self, record):
            writes.append(record)

    original_backend = request_logs._backend
    original_error = request_logs._backend_error
    try:
        request_logs._backend = FakeBackend()
        request_logs._backend_error = ""
        request_logs.record_request(
            channel_id="ch_alive",
            channel_name="Alive",
            model="gpt-4o",
            is_stream=False,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            success=True,
        )
        await request_logs.drain_queue()
        assert [w["channel_id"] for w in writes] == ["ch_alive"]

        request_logs.record_request(
            channel_id="ch_gone",
            channel_name="Gone",
            model="gpt-4o",
            is_stream=False,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            success=True,
        )
        request_logs._backend = None
        request_logs._backend_error = "shutdown"
        await request_logs.drain_queue()
    finally:
        request_logs._backend = original_backend
        request_logs._backend_error = original_error

    assert [w["channel_id"] for w in writes] == ["ch_alive"]
    assert any("discarding" in m.lower() for m in captured_logs)


@pytest.mark.asyncio
async def test_overflow_file_timestamp_is_isoform(tmp_path, monkeypatch):
    """溢出文件 timestamp 保持 isoformat 归一化形态（记录经 record_request 真实溢出）。"""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(request_logs, "_REQUEST_QUEUE_MAX_SIZE", 1)
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
    await request_logs.close_backend()
    result = await request_logs.init_backend({"request_log_sqlite_path": str(tmp_path / "request_logs.db")})
    assert result["available"] is True
    try:
        for i in range(4):
            request_logs.record_request(
                channel_id=f"ch_{i}",
                channel_name="Ch",
                model="gpt-4o",
                is_stream=False,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                success=True,
            )
        overflow_path = os.path.join(str(tmp_path), "request_logs_overflow.jsonl")
        assert os.path.exists(overflow_path)
        rows = [json.loads(line) for line in Path(overflow_path).read_text().splitlines()]
        assert len(rows) == 3
        assert all(
            isinstance(r["timestamp"], str) and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}(?:\+\d{2}:\d{2})?$", r["timestamp"])
            for r in rows
        )
        await request_logs.drain_queue()
        assert (await request_logs.list_requests())["total"] == 1
    finally:
        await request_logs.close_backend()


@pytest.mark.asyncio
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

    assert await request_logs.get_request_field(request_id, "request_headers") == {"data": {"x-app": "test"}}
    assert await request_logs.get_request_field(request_id, "request_body") == {"data": {"messages": [{"role": "user", "content": "hello"}]}}
    assert await request_logs.get_request_field(request_id, "response_headers") == {"data": None}
    assert await request_logs.get_request_field(request_id, "response_body") == {"data": None}


@pytest.mark.asyncio
async def test_list_requests_returns_filterscope_summary(tmp_path):
    """汇总条口径数学（后端直实例化缝，ADR-0017 D3）：种子经 write_record 直写，不经入队。"""
    backend = _query_backend(tmp_path)
    await backend.write_record(
        _backend_record(
            channel_id="ch_ok1",
            channel_name="Ok1",
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=50,
            latency_ms=2000,
            lag_ms=300,
        )
    )
    await backend.write_record(
        _backend_record(
            channel_id="ch_ok2",
            channel_name="Ok2",
            input_tokens=50,
            output_tokens=10,
            cache_read_input_tokens=25,
            latency_ms=1000,
            lag_ms=None,
        )
    )
    await backend.write_record(
        _backend_record(
            channel_id="ch_fail",
            channel_name="Fail",
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            latency_ms=500,
            lag_ms=100,
            success=False,
            error_msg="boom",
        )
    )

    result = await backend.list_requests()

    assert result["summary"] == {
        "total_requests": 3,
        "success_count": 2,
        "input_tokens": 160,
        "output_tokens": 35,
        "cache_read_input_tokens": 75,
        "avg_latency_ms": 1500.0,
        "avg_lag_ms": 300.0,
    }

    failed = await backend.list_requests(success=False)
    assert failed["summary"]["total_requests"] == 1
    assert failed["summary"]["success_count"] == 0
    assert failed["summary"]["avg_latency_ms"] is None
    assert failed["summary"]["avg_lag_ms"] is None


@pytest.mark.asyncio
async def test_list_requests_aggregates_summary_across_month_databases(tmp_path):
    """跨月汇总数学（后端直实例化缝，ADR-0017 D3）：两个月库的明细汇总跨库聚合。"""
    backend = _query_backend(tmp_path)
    for month in ("202503", "202504"):
        await backend.write_record(
            _backend_record(
                timestamp=datetime(int(month[:4]), int(month[4:]), 10),
                channel_id=f"ch_{month}",
                channel_name=month,
                input_tokens=30,
                output_tokens=6,
                cache_read_input_tokens=12,
                cache_creation_input_tokens=0,
                latency_ms=1200,
                lag_ms=150,
                api_key_id=None,
                client_ip="203.0.113.20",
            )
        )

    result = await backend.list_requests()

    assert result["summary"]["total_requests"] == 2
    assert result["summary"]["input_tokens"] == 60
    assert result["summary"]["cache_read_input_tokens"] == 24
    assert result["summary"]["avg_latency_ms"] == 1200.0


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails(sqlite_request_logs, tmp_path):
    _sample_record(channel_id="ch_keep", channel_name="Keep")
    await request_logs.drain_queue()

    not_dir = tmp_path / "not_dir"
    not_dir.write_text("not a directory", encoding="utf-8")
    result = await request_logs.reload_backend({"request_log_sqlite_path": str(not_dir / "request_logs.db")})
    listed = await request_logs.list_requests()

    assert result["available"] is False
    assert "error" in result
    assert listed["available"] is True
    assert listed["total"] == 1
    assert listed["items"][0]["channel_id"] == "ch_keep"


@pytest.mark.asyncio
async def test_filters_by_model_channel_time_success_api_key_client_ip_and_stream(
    tmp_path,
):
    """九条件过滤语义（后端直实例化缝，ADR-0017 D3）：种子经 write_record 直写，不经入队。"""
    backend = _query_backend(tmp_path)
    await backend.write_record(
        _backend_record(
            channel_id="ch_alpha",
            channel_name="Alpha",
            model="gpt-alpha",
            is_stream=True,
            success=True,
            api_key_id="key_alpha",
            client_ip="203.0.113.10",
        )
    )
    await backend.write_record(
        _backend_record(
            channel_id="ch_beta",
            channel_name="Beta",
            model="gpt-beta",
            is_stream=False,
            success=False,
            api_key_id="key_beta",
            client_ip="198.51.100.20",
            error_msg="boom",
        )
    )

    assert (await backend.list_requests(model="alpha"))["total"] == 1
    assert (await backend.list_requests(channel="Beta"))["items"][0]["channel_id"] == "ch_beta"
    assert (await backend.list_requests(success=False))["items"][0]["api_key_id"] == "key_beta"
    assert (await backend.list_requests(api_key_id="key_alpha"))["items"][0]["model"] == "gpt-alpha"
    assert (await backend.list_requests(client_ip="198.51.100"))["items"][0]["model"] == "gpt-beta"
    assert (await backend.list_requests(is_stream=True))["items"][0]["channel_name"] == "Alpha"


@pytest.mark.asyncio
async def test_list_requests_uses_database_pagination_instead_of_full_month_load(tmp_path, monkeypatch):
    """分页下推（后端直实例化缝，ADR-0017 D3）：种子直写后按页查询，月内 LIMIT 不超过页大小。"""
    backend = _query_backend(tmp_path)
    now = datetime.now(UTC)
    await backend.write_record(_backend_record(timestamp=now - timedelta(seconds=2), channel_id="ch_1", channel_name="First"))
    await backend.write_record(_backend_record(timestamp=now - timedelta(seconds=1), channel_id="ch_2", channel_name="Second"))

    calls = []
    original = backend._query_single_month_page

    def spy_page_query(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(backend, "_query_single_month_page", spy_page_query)

    result = await backend.list_requests(page=1, page_size=1)

    assert result["available"] is True
    assert result["total"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["channel_id"] == "ch_2"
    assert calls
    assert all(call[-2] <= 1 for call in calls)


@pytest.mark.asyncio
async def test_list_requests_counts_later_months_without_select_after_page_is_full(tmp_path, monkeypatch):
    """跨月分页跳页（后端直实例化缝，ADR-0017 D3）：页已集满的更旧月份不再发起 SELECT，只补 COUNT。"""
    backend = _query_backend(tmp_path)

    for month in ("202603", "202602", "202601"):
        await backend.write_record(
            _backend_record(
                timestamp=datetime(int(month[:4]), int(month[4:]), 2),
                channel_id=f"ch_{month}",
                channel_name=month,
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                latency_ms=10,
                lag_ms=None,
                success=True,
                api_key_id=None,
                client_ip="203.0.113.20",
            )
        )

    calls = []
    original = backend._query_single_month_page

    def spy_page_query(*args, **kwargs):
        calls.append((args[0], args[-2], args[-1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(backend, "_query_single_month_page", spy_page_query)

    result = await backend.list_requests(page=2, page_size=1)

    assert result["total"] == 3
    assert [item["channel_id"] for item in result["items"]] == ["ch_202602"]
    january_call = [call for call in calls if call[0] == "202601"]
    assert january_call == [("202601", 0, 0)]


@pytest.mark.asyncio
async def test_remove_sqlite_files_ignores_files_locked_by_another_connection(tmp_path, monkeypatch):
    db_path = tmp_path / "request_logs_2026_01.sqlite3"
    db_path.write_text("locked", encoding="utf-8")

    def raise_permission_error(path):
        raise PermissionError("file is locked")

    monkeypatch.setattr(request_logs.os, "remove", raise_permission_error)

    backend = request_logs.SQLiteRequestLogBackend(str(tmp_path / "request_logs.db"))
    assert backend.remove_month_db_files(str(db_path)) == []


@pytest.mark.asyncio
async def test_list_requests_skips_month_removed_between_discovery_and_query(sqlite_request_logs, monkeypatch):
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    await backend.write_record(
        {
            "timestamp": datetime(2026, 1, 2),
            "channel_id": "ch_removed",
            "channel_name": "Removed",
            "model": "gpt-4o",
            "is_stream": False,
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "latency_ms": 10,
            "success": True,
            "api_key_id": None,
            "client_ip": "203.0.113.20",
            "request_headers": None,
            "response_headers": None,
            "request_body": None,
            "response_body": None,
        }
    )

    def raise_operational_error(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(backend, "_query_single_month_page", raise_operational_error)

    result = await request_logs.list_requests(page=1, page_size=10)

    assert result["available"] is True
    assert result["items"] == []
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_cleanup_old_records_clears_raw_fields_and_deletes_rows(sqlite_request_logs, monkeypatch):
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
    old_ts = request_logs._utc_now().replace(year=2000)
    record = {
        "timestamp": old_ts,
        "channel_id": "ch_old",
        "channel_name": "Primary",
        "model": "gpt-4o",
        "is_stream": False,
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
        "latency_ms": 123,
        "success": True,
        "api_key_id": "key_a",
        "client_ip": "203.0.113.10",
        "request_headers": {"x-app": "test"},
        "response_headers": {"x-request-id": "upstream"},
        "request_body": {"messages": [{"role": "user", "content": "hello"}]},
        "response_body": {"choices": [{"message": {"content": "hi"}}]},
        "lag_ms": 21,
        "finish_reason": "stop",
    }
    await request_logs._backend.write_record(record)

    cleared = await request_logs.cleanup_old_records(retention_days=0, raw_retention_days=1)
    request_id = (await request_logs.list_requests())["items"][0]["id"]

    assert cleared["raw_fields_cleared"] == 1
    assert cleared["rows_deleted"] == 0
    assert await request_logs.get_request_field(request_id, "request_body") == {"data": None}

    deleted = await request_logs.cleanup_old_records(retention_days=1, raw_retention_days=0)

    assert deleted["rows_deleted"] == 0
    assert deleted["month_dbs_deleted"] == 1
    assert (await request_logs.list_requests())["total"] == 0


@pytest.mark.asyncio
async def test_cleanup_old_records_removes_fully_expired_month_database(
    sqlite_request_logs,
):
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    old_db = backend._ensure_month_db("200001")
    old_record = {
        "timestamp": request_logs._utc_now().replace(year=2000, month=1, day=2),
        "channel_id": "ch_old",
        "channel_name": "Old",
        "model": "gpt-old",
        "is_stream": False,
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "latency_ms": 10,
        "success": True,
        "api_key_id": None,
        "client_ip": "203.0.113.20",
        "request_headers": None,
        "response_headers": None,
        "request_body": None,
        "response_body": None,
    }
    await backend.write_record(old_record)
    assert request_logs.os.path.exists(old_db)

    result = await request_logs.cleanup_old_records(retention_days=30, raw_retention_days=0)

    assert result["month_dbs_deleted"] == 1
    assert not request_logs.os.path.exists(old_db)


@pytest.mark.asyncio
async def test_invalid_request_field_returns_none(tmp_path):
    """raw 字段读取白名单与不存在 id（后端直实例化缝，ADR-0017 D3）。"""
    backend = _query_backend(tmp_path)
    await backend.write_record(_backend_record())
    request_id = (await backend.list_requests())["items"][0]["id"]

    assert await backend.get_request_field(request_id, "not_allowed") is None
    assert await backend.get_request_field(999999, "request_body") is None


@pytest.mark.asyncio
async def test_sensitivity_info_is_persisted_and_returned(sqlite_request_logs):
    info = {
        "enabled": True,
        "action": "mask",
        "rules_triggered": ["CN_PHONE_NUMBER"],
        "target_api_type": "openai-chat",
    }
    _sample_record(sensitivity_info=info)
    await request_logs.drain_queue()

    result = await request_logs.list_requests()
    assert result["total"] == 1
    assert result["items"][0]["sensitivity_info"] == info


@pytest.mark.asyncio
async def test_conversion_info_is_persisted_and_returned(sqlite_request_logs):
    info = {
        "result": "rejected_response",
        "catalog_revision": "catalog-123",
        "response_diagnostic": {"code": "response_unknown_output", "path": "$.output[0]"},
    }
    _sample_record(conversion_info=info)
    await request_logs.drain_queue()

    result = await request_logs.list_requests()
    assert result["total"] == 1
    assert result["items"][0]["conversion_info"] == info


@pytest.mark.asyncio
async def test_shaping_info_is_persisted_and_returned(sqlite_request_logs):
    info = {
        "schema_version": 1,
        "enabled_features": ["strip_ansi"],
        "actions": [{"feature": "strip_ansi", "field_path": "$.messages[*].content"}],
    }
    _sample_record(shaping_info=info)
    await request_logs.drain_queue()

    result = await request_logs.list_requests()
    assert result["total"] == 1
    assert result["items"][0]["shaping_info"] == info


@pytest.mark.asyncio
async def test_api_type_is_persisted_and_returned(sqlite_request_logs):
    _sample_record(api_type="anthropic")
    _sample_record()
    await request_logs.drain_queue()

    result = await request_logs.list_requests()
    assert result["total"] == 2
    api_types = {item["api_type"] for item in result["items"]}
    assert api_types == {"anthropic", None}


@pytest.mark.asyncio
async def test_legacy_row_without_api_type_reads_as_none(sqlite_request_logs):
    """旧行：手工 INSERT 不含 api_type 列，读出应为 None（装饰层再回退渠道静态配置）。"""
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    ym = backend._current_year_month()
    db_path = backend.month_db_path(ym)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO request_logs (timestamp, model, channel_id, channel_name, is_stream,
                                      input_tokens, output_tokens, latency_ms, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{ym[:4]}-{ym[4:6]}-15 12:00:00",
                "gpt-4o",
                "ch_1",
                "Primary",
                0,
                1,
                1,
                5,
                1,
            ),
        )

    result = await request_logs.list_requests()
    matching = [item for item in result["items"] if item["channel_id"] == "ch_1"]
    assert len(matching) == 1
    assert matching[0]["api_type"] is None


@pytest.mark.asyncio
async def test_api_type_column_migrated_in_existing_database(sqlite_request_logs):
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    # 模拟旧库：创建不带 api_type 的表

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        old_db_path = os.path.join(tmpdir, "old_logs.db")
        os.makedirs(os.path.dirname(old_db_path) or ".", exist_ok=True)
        old_db = request_logs.SQLiteRequestLogBackend(old_db_path)
        old_ym = old_db._current_year_month()
        old_path = old_db.month_db_path(old_ym)
        os.makedirs(os.path.dirname(old_path) or ".", exist_ok=True)
        with sqlite3.connect(old_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requested_model TEXT,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    api_key_id TEXT,
                    client_ip TEXT,
                    is_stream INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL,
                    lag_ms INTEGER,
                    finish_reason TEXT,
                    success INTEGER NOT NULL,
                    error_msg TEXT,
                    request_headers TEXT,
                    response_headers TEXT,
                    request_body TEXT,
                    response_body TEXT
                )
                """
            )
        # 再次 ensure 应补齐列
        old_db._ensure_month_db(old_ym)
        with sqlite3.connect(old_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)").fetchall()}
        assert "api_type" in cols


@pytest.mark.asyncio
async def test_decorate_request_items_prefers_row_api_type(monkeypatch):
    from channel_catalog import CatalogSnapshot
    from models.channel import Channel, Endpoint
    from routers.admin import common

    async def fake_snapshot():
        channel = Channel(
            id="ch_1",
            name="Anthropic CH",
            api_key="key",
            endpoints=[Endpoint(api_type="anthropic", base_url="https://a.example.com")],
        )
        return CatalogSnapshot((channel,), ())

    async def fake_get_api_keys():
        return []

    monkeypatch.setattr(common.catalog, "snapshot", fake_snapshot)
    monkeypatch.setattr(common, "_get_api_keys", fake_get_api_keys)

    result = await common._decorate_request_items(
        {
            "items": [
                {
                    "id": "r_1",
                    "channel_id": "ch_1",
                    "channel_name": "Anthropic CH",
                    # 行值来自本次尝试实际服务的格式，须压过渠道静态 anthropic 配置
                    "api_type": "openai-chat-completions",
                },
                {"id": "r_2", "channel_id": "ch_1", "channel_name": "Anthropic CH"},
            ]
        }
    )

    items = result["items"]
    assert items[0]["api_type"] == "openai-chat-completions"
    assert items[1]["api_type"] == "anthropic"


@pytest.mark.asyncio
async def test_attach_channel_api_types_reads_nested_endpoints(monkeypatch):
    """磁盘渠道已是嵌套 endpoints 形态：静态回退取首个启用接入点的格式，无启用则首接入点"""
    from channel_catalog import CatalogSnapshot
    from models.channel import Channel, Endpoint
    from routers.admin import common

    async def fake_snapshot():
        channels = (
            Channel(
                id="ch_1",
                name="Multi EP",
                api_key="key",
                endpoints=[
                    Endpoint(api_type="anthropic", base_url="https://a.example.com", enabled=False),
                    Endpoint(api_type="openai-chat-completions", base_url="https://b.example.com", enabled=True),
                ],
            ),
            Channel(
                id="ch_2",
                name="All Disabled",
                api_key="key",
                endpoints=[
                    Endpoint(api_type="openai-chat-completions", base_url="https://c.example.com", enabled=False),
                    Endpoint(api_type="openai-response", base_url="https://d.example.com", enabled=False),
                ],
            ),
        )
        return CatalogSnapshot(channels, ())

    async def fake_get_api_keys():
        return []

    monkeypatch.setattr(common.catalog, "snapshot", fake_snapshot)
    monkeypatch.setattr(common, "_get_api_keys", fake_get_api_keys)

    result = await common._decorate_request_items(
        {
            "items": [
                {"id": "r_1", "channel_id": "ch_1", "channel_name": "Multi EP"},
                {"id": "r_2", "channel_name": "All Disabled"},
                {"id": "r_3", "channel_id": "missing", "channel_name": "Unknown"},
            ]
        }
    )

    items = result["items"]
    assert items[0]["api_type"] == "openai-chat-completions"  # 首个启用接入点，跳过停用的 anthropic
    assert items[1]["api_type"] == "openai-chat-completions"  # 无启用接入点 → 回退首个接入点（按名称匹配）
    assert items[2]["api_type"] is None  # 渠道不存在 → 保持 None


@pytest.mark.asyncio
async def test_record_request_wrapper_keeps_stats_free_of_api_type(monkeypatch):
    captured = {}

    def fake_stats_record(**kwargs):
        captured["stats_kwargs"] = kwargs

    def fake_logs_record(**kwargs):
        captured["logs_kwargs"] = kwargs

    monkeypatch.setattr(stats, "record_request", fake_stats_record)
    monkeypatch.setattr(request_logs, "record_request", fake_logs_record)

    from proxy.endpoint_execution import _record_request

    _record_request(
        channel_id="ch_1",
        channel_name="C",
        model="gpt-4o",
        is_stream=False,
        input_tokens=0,
        output_tokens=0,
        latency_ms=1,
        success=True,
        requested_model="gpt-4o-2024-08-06",
        api_type="anthropic",
    )

    assert "api_type" not in captured["stats_kwargs"]
    assert "requested_model" not in captured["stats_kwargs"]
    assert captured["logs_kwargs"]["api_type"] == "anthropic"
    assert captured["logs_kwargs"]["requested_model"] == "gpt-4o-2024-08-06"


@pytest.mark.asyncio
async def test_new_month_db_schema_includes_request_source_column_and_index(sqlite_request_logs):
    """新月度库建表即含 request_source 列（NOT NULL DEFAULT 'client'）+ (request_source, timestamp DESC) 索引。"""
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    db_path = backend.month_db_path(backend._current_year_month())
    with sqlite3.connect(db_path) as conn:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(request_logs)").fetchall()}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('request_logs')").fetchall()}
        source_index_cols = [row[2] for row in conn.execute("PRAGMA index_info('idx_request_logs_source')").fetchall()]

    assert "request_source" in cols
    assert cols["request_source"][3] == 1  # NOT NULL
    assert "'client'" in str(cols["request_source"][4])  # DEFAULT 'client'
    assert "idx_request_logs_source" in indexes
    assert source_index_cols == ["request_source", "timestamp"]


_LEGACY_REQUEST_LOGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    requested_model TEXT,
    channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    api_key_id TEXT,
    client_ip TEXT,
    is_stream INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL,
    lag_ms INTEGER,
    finish_reason TEXT,
    success INTEGER NOT NULL,
    error_msg TEXT,
    request_headers TEXT,
    response_headers TEXT,
    request_body TEXT,
    response_body TEXT
)
"""


def _create_legacy_month_db(data_dir: str, year_month: str, channel_name: str) -> None:
    """手工铸造缺失 request_source/sensitivity_info/api_type 的历史月度库文件，各存一行记录。"""
    month_dir = os.path.join(data_dir, "request_raw_logs")
    os.makedirs(month_dir, exist_ok=True)
    path = os.path.join(month_dir, f"request_logs_{year_month[:4]}_{year_month[4:]}.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.executescript(_LEGACY_REQUEST_LOGS_SCHEMA)
        conn.execute(
            """
            INSERT INTO request_logs (timestamp, model, channel_id, channel_name, is_stream,
                                      input_tokens, output_tokens, latency_ms, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"{year_month[:4]}-{year_month[4:6]}-15 12:00:00", "gpt-legacy", f"ch_{year_month}", channel_name, 0, 1, 5, 42, 1),
        )


@pytest.mark.asyncio
async def test_migration_covers_all_historical_month_databases(tmp_path):
    """User Story 2：启动迁移必须覆盖全部历史月度库。

    任一旧月份缺列都会让跨月 SELECT 抛 OperationalError、被 list 的 except 静默吞掉，
    该月历史从列表消失——断言 total==2 即是防丢月红线。
    """
    data_root = tmp_path / "logs_root"
    _create_legacy_month_db(str(data_root), "202501", "JanChannel")
    _create_legacy_month_db(str(data_root), "202502", "FebChannel")

    result = await request_logs.init_backend({"request_log_sqlite_path": str(data_root / "request_logs.db")})
    try:
        assert result["available"] is True
        listed = await request_logs.list_requests()

        assert listed["available"] is True
        assert listed["total"] == 2
        assert {item["channel_name"] for item in listed["items"]} == {"JanChannel", "FebChannel"}
        assert all(item["request_source"] == "client" for item in listed["items"])
    finally:
        await request_logs.close_backend()


@pytest.mark.asyncio
async def test_request_source_write_default_explicit_and_filtering(sqlite_request_logs):
    """不传来源落 'client'；显式合法值原样落库；读侧支持单值 / IN 多值 / None 不过滤。"""
    _sample_record(channel_id="ch_client")
    _sample_record(channel_id="ch_probe", request_source="group_probe")
    _sample_record(channel_id="ch_admin", request_source="admin_test")
    await request_logs.drain_queue()

    everything = await request_logs.list_requests()
    assert everything["total"] == 3

    client_only = await request_logs.list_requests(request_source="client")
    assert client_only["total"] == 1
    assert client_only["items"][0]["channel_id"] == "ch_client"
    assert client_only["items"][0]["request_source"] == "client"

    non_client = await request_logs.list_requests(request_source=("group_probe", "admin_test"))
    assert non_client["total"] == 2
    assert {item["channel_id"] for item in non_client["items"]} == {"ch_probe", "ch_admin"}
    assert {item["request_source"] for item in non_client["items"]} == {"group_probe", "admin_test"}


@pytest.mark.asyncio
async def test_request_sources_constant_is_single_source_of_truth():
    assert request_logs.REQUEST_SOURCES == ("client", "group_probe", "admin_test")


@pytest.mark.asyncio
async def test_sensitivity_info_column_migrated_in_existing_database(
    sqlite_request_logs,
):
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    # 模拟旧库：创建不带 sensitivity_info 的表

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        old_db_path = os.path.join(tmpdir, "old_logs.db")
        os.makedirs(os.path.dirname(old_db_path) or ".", exist_ok=True)
        old_db = request_logs.SQLiteRequestLogBackend(old_db_path)
        old_ym = old_db._current_year_month()
        old_path = old_db.month_db_path(old_ym)
        os.makedirs(os.path.dirname(old_path) or ".", exist_ok=True)
        with sqlite3.connect(old_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requested_model TEXT,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    api_key_id TEXT,
                    client_ip TEXT,
                    is_stream INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL,
                    lag_ms INTEGER,
                    finish_reason TEXT,
                    success INTEGER NOT NULL,
                    error_msg TEXT,
                    request_headers TEXT,
                    response_headers TEXT,
                    request_body TEXT,
                    response_body TEXT
                )
                """
            )
        # 再次 ensure 应补齐列
        old_db._ensure_month_db(old_ym)
        with sqlite3.connect(old_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)").fetchall()}
        assert "sensitivity_info" in cols
        assert "conversion_info" in cols
        assert "shaping_info" in cols


# ─── 月度分库 on-disk 布局公开接口（ADR-0017 D1 工单02）───
# 后端直实例化（tmp 目录）：文件名格式 / 目录发现 / 伴随文件三件套 / 复合 id 前缀。
# 只断言外部可观察行为，不断言 SQL 字符串。


def _layout_backend(tmp_path) -> tuple[request_logs.SQLiteRequestLogBackend, Path]:
    backend = request_logs.SQLiteRequestLogBackend(str(tmp_path / "request_logs.db"))
    logs_dir = Path(backend.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return backend, logs_dir


def test_month_db_path_builds_filename_from_month_key(tmp_path):
    """文件名格式（request_logs_{y}_{m}.sqlite3）的单一属主：月份键 → 月库路径。"""
    backend, logs_dir = _layout_backend(tmp_path)

    path = backend.month_db_path("202605")

    assert path == str(logs_dir / "request_logs_2026_05.sqlite3")


def test_discover_month_dbs_ignores_non_numeric_and_malformed_files(tmp_path):
    """目录发现容差归一（isdigit）：非数字命名的杂散文件与形态不符文件一律忽略，只返回合法月份键。"""
    backend, logs_dir = _layout_backend(tmp_path)
    for name in (
        "request_logs_2026_05.sqlite3",
        "request_logs_2026_06.sqlite3",
        # 杂散：命中文件名形态但含非数字字符
        "request_logs_20x6_05.sqlite3",
        "request_logs_2026_0x.sqlite3",
        # 形态不符：无月份分段 / 伴随文件 / 其他文件
        "request_logs_202606.sqlite3",
        "request_logs_2026_06.sqlite3-wal",
        "request_logs_2026_06.sqlite3-shm",
        "other.txt",
    ):
        (logs_dir / name).write_bytes(b"")

    assert backend.discover_month_dbs() == ["202605", "202606"]


def test_month_db_sibling_files_lists_trio_without_deleting(tmp_path):
    """伴随文件三件套（db/-wal/-shm）清单唯一属主：列出现存文件及大小，不产生删除。"""
    backend, logs_dir = _layout_backend(tmp_path)
    db_path = logs_dir / "request_logs_2026_05.sqlite3"
    db_path.write_bytes(b"x" * 1000)
    wal_path = logs_dir / "request_logs_2026_05.sqlite3-wal"
    wal_path.write_bytes(b"x" * 100)
    shm_path = logs_dir / "request_logs_2026_05.sqlite3-shm"
    shm_path.write_bytes(b"x" * 50)

    siblings = backend.month_db_sibling_files(str(db_path))

    assert [(os.path.basename(p), size) for p, size in siblings] == [
        ("request_logs_2026_05.sqlite3", 1000),
        ("request_logs_2026_05.sqlite3-wal", 100),
        ("request_logs_2026_05.sqlite3-shm", 50),
    ]
    assert db_path.exists() and wal_path.exists() and shm_path.exists()


def test_remove_month_db_files_deletes_sidecar_trio(tmp_path):
    """单月删除连带清掉三件套：返回 (文件名, 大小) 且三个文件都不复存在。"""
    backend, logs_dir = _layout_backend(tmp_path)
    db_path = logs_dir / "request_logs_2026_05.sqlite3"
    db_path.write_bytes(b"x" * 1000)
    wal_path = logs_dir / "request_logs_2026_05.sqlite3-wal"
    wal_path.write_bytes(b"x" * 100)
    shm_path = logs_dir / "request_logs_2026_05.sqlite3-shm"
    shm_path.write_bytes(b"x" * 50)

    removed = backend.remove_month_db_files(str(db_path))

    assert sorted(name for name, _ in removed) == sorted(
        [
            "request_logs_2026_05.sqlite3",
            "request_logs_2026_05.sqlite3-wal",
            "request_logs_2026_05.sqlite3-shm",
        ]
    )
    assert sum(size for _, size in removed) == 1150
    assert not db_path.exists() and not wal_path.exists() and not shm_path.exists()


@pytest.mark.asyncio
async def test_composite_id_prefix_is_generated_from_month_key_across_months(tmp_path):
    """复合 id 月份前缀由后端按月字符串生成：跨月分页下前缀与记录所在月一致，且可解析回原库取原始字段。"""
    backend, _logs_dir = _layout_backend(tmp_path)

    def _record(month: str) -> dict:
        return {
            "timestamp": datetime(int(month[:4]), int(month[4:]), 10),
            "channel_id": f"ch_{month}",
            "channel_name": month,
            "model": "gpt-4o",
            "is_stream": False,
            "input_tokens": 1,
            "output_tokens": 1,
            "latency_ms": 10,
            "success": True,
            "request_body": {"month": month},
        }

    await backend.write_record(_record("202503"))
    await backend.write_record(_record("202504"))

    page1 = await backend.list_requests(page=1, page_size=1)
    page2 = await backend.list_requests(page=2, page_size=1)

    # 排序 timestamp DESC：page1 落在 202504 库，page2 落在 202503 库
    assert page1["items"][0]["channel_id"] == "ch_202504"
    assert re.fullmatch(r"202504_\d+", page1["items"][0]["id"])
    assert page2["items"][0]["channel_id"] == "ch_202503"
    assert re.fullmatch(r"202503_\d+", page2["items"][0]["id"])

    field = await backend.get_request_field(page1["items"][0]["id"], "request_body")
    assert field == {"data": {"month": "202504"}}
