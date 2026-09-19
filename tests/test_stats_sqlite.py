import contextlib
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio

import stats
from request_record_query import record_timestamp_to_iso
from routers.admin import get_stats

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


def _sample_payload(**overrides):
    """统计记录种子构造（只含聚合字段，ADR-0014 D2 写入口径）。"""
    payload = {
        "channel_id": "ch_1",
        "channel_name": "Primary",
        "model": "gpt-4o",
        "is_stream": False,
        "input_tokens": 12,
        "output_tokens": 8,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "latency_ms": 120,
        "success": True,
        "api_key_id": "key_a",
        "lag_ms": 25,
        "finish_reason": "stop",
    }
    payload.update(overrides)
    return payload


async def _seed_sample(**overrides):
    """直写一条统计记录（不入队）：与队列写回调同一落库路径，查询语义测试无需 drain 等待（ADR-0017 工单04）。"""
    await stats._write_record(_sample_payload(**overrides))


def _table_pk_columns(db_path, table: str) -> list[str]:
    """按主键序号列出表的 PK 列名（直连 schema 断言用）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = [row for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        return [row[1] for row in sorted((row for row in rows if row[5] > 0), key=lambda row: row[5])]
    finally:
        conn.close()


def table_names(db_path) -> set[str]:
    """测试工具：直连 tmp 库文件读 sqlite_master 表名。

    建表断言不再走生产内测试后门（stats._list_tables_for_test 已删，ADR-0017 工单03）。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


async def test_new_stats_db_declares_request_source_everywhere(sqlite_stats_db):
    """新建库：明细表有列；日/时聚合表列存在且主键扩为五段含 request_source。"""
    assert "request_source" in _raw_table_columns(str(sqlite_stats_db))

    daily_pk = _table_pk_columns(sqlite_stats_db, "daily_stats")
    hourly_pk = _table_pk_columns(sqlite_stats_db, "hourly_stats")
    assert daily_pk == ["date", "channel_id", "model", "api_key_id", "request_source"]
    assert hourly_pk == ["hour", "channel_id", "model", "api_key_id", "request_source"]


def _raw_table_columns(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(request_stats_raw)").fetchall()}
    finally:
        conn.close()


_LEGACY_AGGREGATE_COLUMNS = """
            request_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            avg_latency_ms INTEGER,
            avg_lag_ms INTEGER,
            updated_at TEXT NOT NULL,
"""


async def test_legacy_aggregate_tables_rebuilt_backfilling_client(tmp_path):
    """旧聚合表 rename-copy-rebuild：历史行保留且回填 'client'，重建后可混源聚合不撞 UNIQUE。"""
    old_db = tmp_path / "stats_legacy.db"
    conn = sqlite3.connect(str(old_db))
    conn.executescript(
        f"""
        CREATE TABLE request_stats_raw (
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
            error_msg TEXT
        );

        CREATE TABLE daily_stats (
            date TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key_id TEXT NOT NULL,
            {_LEGACY_AGGREGATE_COLUMNS}
            PRIMARY KEY (date, channel_id, model, api_key_id)
        );

        CREATE TABLE hourly_stats (
            hour TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key_id TEXT NOT NULL,
            {_LEGACY_AGGREGATE_COLUMNS}
            PRIMARY KEY (hour, channel_id, model, api_key_id)
        );

        INSERT INTO daily_stats (date, channel_id, model, api_key_id, request_count,
                                 success_count, fail_count, updated_at)
        VALUES ('2030-01-02', 'ch_hist', 'gpt-old', 'key_old', 7, 6, 1, '2030-01-03 00:00:00');

        INSERT INTO hourly_stats (hour, channel_id, model, api_key_id, request_count,
                                  success_count, fail_count, updated_at)
        VALUES ('2030-01-02T13', 'ch_hist', 'gpt-old', 'key_old', 3, 3, 0, '2030-01-03 00:00:00');
        """
    )
    conn.close()

    await stats.close_pool()
    await stats.init_db(str(old_db))

    daily_pk = _table_pk_columns(old_db, "daily_stats")
    hourly_pk = _table_pk_columns(old_db, "hourly_stats")
    assert daily_pk == ["date", "channel_id", "model", "api_key_id", "request_source"]
    assert hourly_pk == ["hour", "channel_id", "model", "api_key_id", "request_source"]

    conn = sqlite3.connect(str(old_db))
    try:
        cursor_desc = conn.execute("SELECT * FROM daily_stats LIMIT 1").description
        legacy_daily = conn.execute("SELECT * FROM daily_stats WHERE date = '2030-01-02'").fetchone()
        daily_row = dict(zip([col[0] for col in cursor_desc], legacy_daily, strict=True))
        hourly_row = conn.execute("SELECT * FROM hourly_stats WHERE hour = '2030-01-02T13'").fetchone()
    finally:
        conn.close()
    assert daily_row["request_source"] == "client"
    assert daily_row["request_count"] == 7
    assert daily_row["cache_read_input_tokens"] == 0  # token 补列先于 rebuild，copy 未丢默认值
    assert hourly_row is not None  # 时表历史行同样保留（对称 rebuild）

    # 硬需求回归线：admin_test 明细行出现后，下一次日聚合不得撞旧四段主键的 UNIQUE 约束
    await _seed_sample(api_key_id="key_mixed")
    await _seed_sample(api_key_id="key_mixed", request_source="admin_test")
    today = stats.agg_now().date()
    result = await stats.aggregate_daily_stats(today, today)  # 撞约束会在此抛 IntegrityError
    assert result["updated_rows"] >= 1


async def test_daily_aggregation_splits_rows_by_request_source(sqlite_stats_db):
    """同 (date, channel, model, api_key) 下两种 source 各出一行、互不覆盖；重算幂等。"""
    await _seed_sample(api_key_id="key_split", channel_id="ch_same", model="gpt-same")
    await _seed_sample(api_key_id="key_split", channel_id="ch_same", model="gpt-same", latency_ms=180)
    await _seed_sample(api_key_id="key_split", channel_id="ch_same", model="gpt-same", request_source="admin_test")
    today = stats.agg_now().date()
    await stats.aggregate_daily_stats(today, today)
    await stats.aggregate_daily_stats(today, today)  # 前置 DELETE 整段重算：二次执行不叠不炸

    rows = [
        row
        for row in await stats.get_daily_stats(days=1)
        if row["channel_id"] == "ch_same" and row["model"] == "gpt-same" and row["api_key_id"] == "key_split"
    ]
    by_source = {row["request_source"]: row for row in rows}

    assert len(rows) == 2
    assert set(by_source) == {"client", "admin_test"}
    assert by_source["client"]["request_count"] == 2
    assert by_source["admin_test"]["request_count"] == 1


async def test_get_daily_stats_filters_by_request_source(sqlite_stats_db):
    """get_daily_stats 的 request_source 过滤语义（直写种子 + 聚合作业作 setup，ADR-0017 工单04）。"""
    await _seed_sample(api_key_id="key_filter")
    await _seed_sample(api_key_id="key_filter", request_source="admin_test")
    today = stats.agg_now().date()
    await stats.aggregate_daily_stats(today, today)

    client_rows = await stats.get_daily_stats(days=1, request_source="client")
    assert [row["request_source"] for row in client_rows] == ["client"]

    multi_rows = await stats.get_daily_stats(days=1, request_source=("client", "admin_test"))
    assert {row["request_source"] for row in multi_rows} == {"client", "admin_test"}

    unfiltered_rows = await stats.get_daily_stats(days=1)  # None＝不过滤，保留全量
    assert {row["request_source"] for row in unfiltered_rows} == {"client", "admin_test"}


async def test_realtime_daily_query_splits_only_when_source_filter_given(sqlite_stats_db):
    """None 保持旧行为形态（每日一行、不带 source 键），保护 get_today_stats 的取末行消费方式。"""
    await _seed_sample(api_key_id="key_rt")
    await _seed_sample(api_key_id="key_rt", latency_ms=200)
    await _seed_sample(api_key_id="key_rt", request_source="admin_test")

    legacy_shape = await stats.get_daily_stats_from_requests(days=1)
    assert len(legacy_shape) == 1
    assert legacy_shape[0]["request_count"] == 3
    assert "request_source" not in legacy_shape[0]

    client_only = await stats.get_daily_stats_from_requests(days=1, request_source="client")
    assert len(client_only) == 1
    assert client_only[0]["request_count"] == 2
    assert client_only[0]["request_source"] == "client"

    split_multi = await stats.get_daily_stats_from_requests(days=1, request_source=("client", "admin_test"))
    counts = {row["request_source"]: row["request_count"] for row in split_multi}
    assert counts == {"client": 2, "admin_test": 1}


async def test_raw_detail_persists_request_source(sqlite_stats_db):
    """明细透传：不传落 'client'，显式值原样落库（直连数据库验证）。"""
    await _seed_sample(channel_id="ch_client")
    await _seed_sample(channel_id="ch_admin", request_source="admin_test")

    conn = sqlite3.connect(str(sqlite_stats_db))
    try:
        rows = dict(conn.execute("SELECT channel_id, request_source FROM request_stats_raw").fetchall())
    finally:
        conn.close()
    assert rows == {"ch_client": "client", "ch_admin": "admin_test"}


async def test_init_db_creates_sqlite_tables(sqlite_stats_db):
    assert table_names(sqlite_stats_db) == {
        "context_shaping_daily_stats",
        "daily_stats",
        "hourly_stats",
        "request_stats_raw",
        "write_behind_receipts",
    }


async def test_context_shaping_records_actual_actions_and_builds_view(sqlite_stats_db):
    stats.record_context_shaping_action(
        channel_id="ch_1",
        model="gpt-4o",
        feature="strip_ansi",
        action="strip_ansi",
        action_count=2,
        before_chars=20,
        after_chars=12,
    )
    await stats.drain_queue()

    view = await stats.get_context_shaping_view(days=1)

    assert view["overall"] == {
        "request_count": 1,
        "action_count": 2,
        "before_chars": 20,
        "after_chars": 12,
        "char_change": -8,
    }
    assert view["by_feature"] == [{"feature": "strip_ansi", **view["overall"]}]
    assert view["by_action"] == [{"feature": "strip_ansi", "action": "strip_ansi", **view["overall"]}]
    assert view["meta"]["features"] == ["strip_ansi"]


async def test_list_requests_omits_raw_fields():
    """记账口径只含聚合字段（ADR-0014 D2）：list_requests 行永不携带 raw 列（直写种子）。"""
    await _seed_sample()

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
    assert item["cache_read_input_tokens"] == 0
    assert item["cache_creation_input_tokens"] == 0
    assert item["latency_ms"] == 120
    assert item["lag_ms"] == 25
    assert item["finish_reason"] == "stop"
    assert item["success"] is True
    assert RAW_FIELDS.isdisjoint(item)


async def test_aggregate_daily_stats_refreshes_daily_stats():
    await _seed_sample(
        model="gpt-4o-mini",
        input_tokens=20,
        output_tokens=5,
        cache_read_input_tokens=13,
        cache_creation_input_tokens=2,
    )

    result = await stats.aggregate_daily_stats(date.today(), date.today())
    daily = await stats.get_daily_stats(days=1, model="gpt-4o-mini")

    assert result["updated_rows"] == 1
    assert len(daily) == 1
    assert daily[0]["request_count"] == 1
    assert daily[0]["success_count"] == 1
    assert daily[0]["fail_count"] == 0
    assert daily[0]["input_tokens"] == 20
    assert daily[0]["output_tokens"] == 5
    assert daily[0]["cache_read_input_tokens"] == 13
    assert daily[0]["cache_creation_input_tokens"] == 2


async def test_admin_stats_daily_trend_includes_cache_token_totals():
    await _seed_sample(
        channel_id="ch_primary",
        channel_name="Primary",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=10,
        cache_read_input_tokens=70,
        cache_creation_input_tokens=5,
    )
    await _seed_sample(
        channel_id="ch_backup",
        channel_name="Backup",
        model="gpt-4o",
        input_tokens=50,
        output_tokens=5,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=2,
    )
    today = stats.agg_now().date()
    await stats.aggregate_daily_stats(today, today)

    result = await get_stats(days=1)

    assert result["daily"][0]["total_cache_read_input_tokens"] == 100
    assert result["daily"][0]["total_cache_creation_input_tokens"] == 7


async def test_cache_token_details_and_overall_totals_are_queryable():
    """带 cache token 的记录（直写种子）：列表行携带明细、总体与 api_key 聚合含 token 总量。"""
    await _seed_sample(
        input_tokens=1200,
        output_tokens=80,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=40,
    )

    listed = await stats.list_requests()
    overall = await stats.get_overall_stats(days=1)

    item = listed["items"][0]
    assert item["cache_read_input_tokens"] == 900
    assert item["cache_creation_input_tokens"] == 40
    assert overall["total_cache_read_input_tokens"] == 900
    assert overall["total_cache_creation_input_tokens"] == 40
    assert overall["api_keys"][0]["cache_read_input_tokens"] == 900
    assert overall["api_keys"][0]["cache_creation_input_tokens"] == 40


async def test_overall_stats_include_token_totals_for_channel_and_model_distributions():
    await _seed_sample(
        channel_id="ch_primary",
        channel_name="Primary",
        model="gpt-4o",
        input_tokens=1200,
        output_tokens=80,
    )
    await _seed_sample(
        channel_id="ch_primary",
        channel_name="Primary",
        model="gpt-4o",
        input_tokens=300,
        output_tokens=20,
    )

    overall = await stats.get_overall_stats(days=1)

    assert overall["channels"][0]["name"] == "Primary"
    assert overall["channels"][0]["input_tokens"] == 1500
    assert overall["channels"][0]["output_tokens"] == 100
    assert overall["models"][0]["name"] == "gpt-4o"
    assert overall["models"][0]["input_tokens"] == 1500
    assert overall["models"][0]["output_tokens"] == 100


async def test_init_db_migrates_existing_stats_db_for_cache_token_columns(tmp_path):
    old_db = tmp_path / "stats_old.db"
    conn = sqlite3.connect(str(old_db))
    conn.executescript(
        """
        CREATE TABLE request_stats_raw (
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
            error_msg TEXT
        );

        CREATE TABLE daily_stats (
            date TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key_id TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            avg_latency_ms INTEGER,
            avg_lag_ms INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date, channel_id, model, api_key_id)
        );

        CREATE TABLE hourly_stats (
            hour TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key_id TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            avg_latency_ms INTEGER,
            avg_lag_ms INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (hour, channel_id, model, api_key_id)
        );
        """
    )
    conn.close()

    await stats.close_pool()
    await stats.init_db(str(old_db))

    await _seed_sample(cache_read_input_tokens=55, cache_creation_input_tokens=6)
    listed = await stats.list_requests()

    assert listed["items"][0]["cache_read_input_tokens"] == 55
    assert listed["items"][0]["cache_creation_input_tokens"] == 6


async def test_refresh_missing_daily_stats_uses_timestamp_index_for_date_cutoff(sqlite_stats_db, monkeypatch):
    old_ts = (datetime.now(UTC) - timedelta(days=3)).replace(tzinfo=None)
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
            record_timestamp_to_iso(old_ts),
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
    conn.set_trace_callback(lambda sql: traced_sql.append(sql) if "SELECT DISTINCT" in sql and "FROM request_stats_raw" in sql else None)
    # 直接 patch _open_conn 让多次调用复用同一个 conn(测试需要稳定的 trace callback),
    # 而 _open_conn 在生产里会显式 close。用 nullcontext 跳过 close + commit,测试 conn
    # 由外层 sqlite_stats_db fixture 释放。
    monkeypatch.setattr(stats, "_open_conn", lambda: contextlib.nullcontext(conn))

    stats._refresh_missing_daily_stats_sync()

    assert traced_sql
    plan_rows = conn.execute(f"EXPLAIN QUERY PLAN {traced_sql[0]}").fetchall()
    plan_text = " ".join(row[3] for row in plan_rows)
    assert "SEARCH request_stats_raw USING" in plan_text
    assert "idx_request_stats_raw_timestamp" in plan_text


async def test_list_requests_returns_filterscope_summary(sqlite_stats_db):
    """汇总条口径数学（stats 侧，直写种子，ADR-0017 工单04）。"""
    await _seed_sample(
        channel_id="ch_ok1",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=50,
        latency_ms=2000,
        lag_ms=300,
        success=True,
    )
    await _seed_sample(
        channel_id="ch_ok2",
        model="gpt-4o",
        input_tokens=50,
        output_tokens=10,
        cache_read_input_tokens=25,
        latency_ms=1000,
        lag_ms=None,
        success=True,
    )
    await _seed_sample(
        channel_id="ch_fail",
        model="gpt-4o",
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        latency_ms=500,
        lag_ms=100,
        success=False,
        error_msg="boom",
    )

    result = await stats.list_requests()

    assert result["summary"] == {
        "total_requests": 3,
        "success_count": 2,
        "input_tokens": 160,
        "output_tokens": 35,
        "cache_read_input_tokens": 75,
        "avg_latency_ms": 1500.0,
        "avg_lag_ms": 300.0,
    }

    failed = await stats.list_requests(success=False)
    assert failed["summary"]["success_count"] == 0
    assert failed["summary"]["avg_latency_ms"] is None
    assert failed["items"][0]["error_msg"] == "boom"


async def test_list_requests_supports_basic_filters():
    """基础过滤条件语义（stats 侧，直写种子，ADR-0017 工单04）。"""
    await _seed_sample(
        channel_id="ch_alpha",
        channel_name="Alpha",
        model="gpt-alpha",
        is_stream=True,
        success=True,
        api_key_id="key_alpha",
    )
    await _seed_sample(
        channel_id="ch_beta",
        channel_name="Beta",
        model="gpt-beta",
        is_stream=False,
        success=False,
        api_key_id="key_beta",
        error_msg="boom",
    )

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
