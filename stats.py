"""SQLite 聚合统计后端：请求记账入队 + 日聚合作业 + 管理端查询 API。

按 ADR-0002"文件名即职责"内部分区（ADR-0017 D2，纯搬家不改 schema/语义）：

- 共享基建：`_DB_AVAILABLE` 单一守卫点（`_require_db`）+ 连接配方 + 聚合时区/时间换算
- 建库迁移：schema DDL 与旧库迁移
- 写入与队列生命周期：记账入队 + WriteBehindWiring 接线 + 启停
- 聚合作业：日聚合重算与缺失回补
- 查询 API：日聚合 / 总体 / 今日 / api_key / 请求列表
"""

import asyncio
import contextlib
import functools
import inspect
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

import config
from db_write_behind import (
    _VALID_JOURNAL_MODE,
    WriteBehindParams,
    WriteBehindWiring,
    _sanitize_int_env,
    _sanitize_pragma_env,
    create_connection,
)
from request_record_query import (
    append_request_source_condition,
    build_summary_sql,
    build_where_clause,
    normalize_bool_fields,
    normalize_pagination,
    record_timestamp_to_iso,
)

_DB_PATH: str | None = None
_DB_AVAILABLE = False
_DB_INIT_LOCK = asyncio.Lock()

_STATS_QUEUE_MAX_SIZE = 1000
_STATS_WRITE_TIMEOUT = 60

_STATS_OVERFLOW_FILENAME = "stats_overflow.jsonl"

# 32 MB mmap;走 OS page cache 共享内存,替代每连接私有 cache。
# 统计库数据量小,32 MB 足够覆盖热数据。
_MMAP_SIZE_BYTES = 32 * 1024 * 1024

_STATS_WORKER_COUNT = _sanitize_int_env("STATS_WORKER_COUNT", 4)

_RAW_FIELDS = {
    "request_headers",
    "response_headers",
    "request_body",
    "response_body",
}


# ─── 共享基建：`_DB_AVAILABLE` 单一守卫点 + 连接配方 ───


def _require_db(empty: Callable[..., Any] | None = None):
    """`_DB_AVAILABLE` 全文件唯一守卫点（ADR-0017 D2）。

    装饰 *_sync 查询/聚合实现与记账入口：库未初始化时不触库、不抛异常，
    直接返回声明的空形。empty 声明了哪些形参名，就从被装饰函数的绑定实参
    （含默认值）里挑同名实参传入——空形可从入参推导（如归一后的分页值）；
    无参 empty 直接调用。新增查询族只需声明 empty，不再复制可用性 if 块；
    空形/零值、不抛的外部契约由 tests/test_stats_unavailable_contract.py 守门。
    """

    def decorator(fn):
        fn_signature = inspect.signature(fn)
        empty_params = frozenset(inspect.signature(empty).parameters) if empty is not None else frozenset()

        def empty_value(args, kwargs):
            if empty is None:
                return None
            if not empty_params:
                return empty()
            bound = fn_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return empty(**{name: value for name, value in bound.arguments.items() if name in empty_params})

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                if _DB_AVAILABLE:
                    return await fn(*args, **kwargs)
                return empty_value(args, kwargs)

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if _DB_AVAILABLE:
                return fn(*args, **kwargs)
            return empty_value(args, kwargs)

        return wrapper

    return decorator


def _resolve_db_path(db_path: str | None = None) -> str:
    if db_path:
        return db_path
    configured = config.get_setting("stats_sqlite_path")
    if configured:
        return configured
    return os.path.join(config.DATA_DIR, "stats.db")


def _connect() -> sqlite3.Connection:
    if not _DB_PATH:
        raise RuntimeError("stats database is not initialized")
    return create_connection(_DB_PATH, mmap_size=("SQLITE_MMAP_SIZE_STATS", _MMAP_SIZE_BYTES))


@contextlib.contextmanager
def _open_conn():
    """打开统计 DB 连接:保证 fd 立即释放 + 隐式事务的 commit/rollback。

    短连接配方的统一入口。直接 with _connect() 只会 commit/rollback,不会关闭 fd;
    本 helper 用 try/finally 兜底 close,异常路径同样可靠。
    """
    conn = _connect()
    try:
        with conn:  # 正常退出 commit;异常 rollback;只读路径下是 no-op
            yield conn
    finally:
        conn.close()


# ─── 共享基建：聚合时区与时间换算 ───


def _agg_tz() -> tzinfo:
    """返回聚合时区：优先 settings.aggregation_timezone，否则系统本地时区。"""
    name = (config.get_setting("aggregation_timezone") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(f"Invalid aggregation_timezone {name!r}, falling back to system local")
    return datetime.now().astimezone().tzinfo or UTC


def _agg_offset_seconds(at: datetime | None = None) -> int:
    """返回聚合时区相对 UTC 的偏移秒数（按指定时刻，处理 DST）。"""
    tz = _agg_tz()
    if at is None:
        at = datetime.now(UTC)
    elif at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    offset = tz.utcoffset(at.astimezone(tz).replace(tzinfo=None))
    return int((offset or timedelta(0)).total_seconds())


def _agg_offset_sql(at: datetime | None = None) -> str:
    """SQLite datetime modifier，例如 '+28800 seconds'。"""
    seconds = _agg_offset_seconds(at)
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{abs(seconds)} seconds"


def agg_now() -> datetime:
    """返回聚合时区的当前时间（naive，仅用于日聚合切日与同时区运算）。"""
    return datetime.now(UTC).astimezone(_agg_tz()).replace(tzinfo=None)


def local_date_to_utc_iso(local_date: date) -> str:
    """将聚合时区某日 0 点转为 naive UTC 的 ISO 字符串（DB timestamp 用）。"""
    tz = _agg_tz()
    local = datetime.combine(local_date, datetime.min.time()).replace(tzinfo=tz)
    return record_timestamp_to_iso(local.astimezone(UTC).replace(tzinfo=None))


# ─── 建库迁移：schema DDL 与旧库迁移（除搬家外零变更，ADR-0017 D2） ───


def _ensure_sqlite_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _aggregate_table_ddl(table: str, period_column: str) -> str:
    """日/时聚合表统一 DDL：schema 由日/时两处与 rename-copy-rebuild 迁移共享，防漂移。"""
    return f"""
            CREATE TABLE IF NOT EXISTS {table} (
                {period_column} TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_id TEXT NOT NULL,
                request_source TEXT NOT NULL DEFAULT 'client',
                request_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                avg_latency_ms INTEGER,
                avg_lag_ms INTEGER,
                updated_at TEXT NOT NULL,
                PRIMARY KEY ({period_column}, channel_id, model, api_key_id, request_source)
            );
            """


def _init_db_sync(db_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"PRAGMA journal_mode={_sanitize_pragma_env('SQLITE_JOURNAL_MODE', 'WAL', _VALID_JOURNAL_MODE)}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(
            _aggregate_table_ddl("daily_stats", "date")
            + _aggregate_table_ddl("hourly_stats", "hour")
            + """
            CREATE TABLE IF NOT EXISTS request_stats_raw (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                write_id TEXT UNIQUE,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
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
                error_msg TEXT
            );

            CREATE TABLE IF NOT EXISTS context_shaping_daily_stats (
                date TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                model TEXT NOT NULL,
                feature TEXT NOT NULL,
                action TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                action_count INTEGER NOT NULL DEFAULT 0,
                before_chars INTEGER NOT NULL DEFAULT 0,
                after_chars INTEGER NOT NULL DEFAULT 0,
                char_change INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date, channel_id, model, feature, action)
            );

            CREATE TABLE IF NOT EXISTS write_behind_receipts (
                write_id TEXT PRIMARY KEY
            );

            CREATE INDEX IF NOT EXISTS idx_request_stats_raw_timestamp ON request_stats_raw(timestamp);
            CREATE INDEX IF NOT EXISTS idx_request_stats_raw_model ON request_stats_raw(model);
            CREATE INDEX IF NOT EXISTS idx_request_stats_raw_channel ON request_stats_raw(channel_id, channel_name);
            CREATE INDEX IF NOT EXISTS idx_request_stats_raw_api_key ON request_stats_raw(api_key_id);
            CREATE INDEX IF NOT EXISTS idx_request_stats_raw_client_ip ON request_stats_raw(client_ip);
            CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);
            CREATE INDEX IF NOT EXISTS idx_hourly_stats_hour ON hourly_stats(hour);
            """
        )
        token_detail_columns = {
            "cache_read_input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "cache_creation_input_tokens": "INTEGER NOT NULL DEFAULT 0",
        }
        for table in ("request_stats_raw", "daily_stats", "hourly_stats"):
            _ensure_sqlite_columns(conn, table, token_detail_columns)
        # 明细表补来源列即可；聚合表主键含 request_source，SQLite 无法 ALTER 主键，须整体 rebuild
        _ensure_sqlite_columns(conn, "request_stats_raw", {"request_source": "TEXT NOT NULL DEFAULT 'client'"})
        _ensure_sqlite_columns(conn, "request_stats_raw", {"write_id": "TEXT"})
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_request_stats_raw_write_id ON request_stats_raw(write_id) WHERE write_id IS NOT NULL")
        for table, period_column, index_name in (
            ("daily_stats", "date", "idx_daily_stats_date"),
            ("hourly_stats", "hour", "idx_hourly_stats_hour"),
        ):
            _rebuild_aggregate_table_if_missing_request_source(conn, table, period_column, index_name)


def _rebuild_aggregate_table_if_missing_request_source(conn: sqlite3.Connection, table: str, period_column: str, period_index_name: str) -> None:
    """旧日/时聚合表缺 request_source 时，rename-copy-rebuild 换成含该列的五段主键。

    Why 不是加列了事：聚合 INSERT..SELECT 在同 (date/hour, channel, model, api_key)
    出现第二种 source 时会撞旧四段主键的 UNIQUE 约束（admin_test 明细出现当期即触发的硬需求）。
    旧行 copy 统一回填 'client'——历史数据全部来自客户端流量。前置的 token 补列步骤保证
    legacy 列集完整；DDL 与新建表共用工厂，避免两份 schema 漂移。
    """
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "request_source" in columns:
        return
    legacy_table = f"{table}_rs_legacy"
    create_sql = _aggregate_table_ddl(table, period_column).replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)
    conn.execute(f"ALTER TABLE {table} RENAME TO {legacy_table}")
    conn.execute(create_sql)
    # 目标列第 5 位是 request_source；SELECT 同位置填字面量 'client'，两侧逐列一一对应
    inserted_columns = (
        f"{period_column}, channel_id, model, api_key_id, request_source, request_count, "
        "success_count, fail_count, input_tokens, output_tokens, cache_read_input_tokens, "
        "cache_creation_input_tokens, avg_latency_ms, avg_lag_ms, updated_at"
    )
    selected_values = (
        f"{period_column}, channel_id, model, api_key_id, 'client', request_count, "
        "success_count, fail_count, input_tokens, output_tokens, cache_read_input_tokens, "
        "cache_creation_input_tokens, avg_latency_ms, avg_lag_ms, updated_at"
    )
    conn.execute(f"INSERT INTO {table} ({inserted_columns}) SELECT {selected_values} FROM {legacy_table}")
    conn.execute(f"DROP TABLE {legacy_table}")
    conn.execute(f"CREATE INDEX IF NOT EXISTS {period_index_name} ON {table}({period_column})")


async def init_db(db_path: str | None = None) -> None:
    """初始化 SQLite 统计库。"""
    global _DB_PATH, _DB_AVAILABLE
    resolved_path = _resolve_db_path(db_path)
    async with _DB_INIT_LOCK:
        await asyncio.to_thread(_init_db_sync, resolved_path)
        _DB_PATH = resolved_path
        _DB_AVAILABLE = True


# ─── 写入与队列生命周期：记账入队 + WriteBehindWiring 接线 ───


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in _RAW_FIELDS}


@_require_db()
def _write_record_sync(record: dict[str, Any]) -> None:
    lightweight = _normalize_record(record)
    timestamp = lightweight.get("timestamp") or datetime.now(UTC).replace(tzinfo=None)
    if isinstance(timestamp, datetime):
        timestamp = record_timestamp_to_iso(timestamp)
    with _open_conn() as conn:
        write_id = lightweight.get("_write_id")
        conn.execute(
            """
            INSERT OR IGNORE INTO request_stats_raw
            (write_id, timestamp, model, channel_id, channel_name, api_key_id, client_ip, is_stream,
             input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
             latency_ms, lag_ms, finish_reason,
             success, error_msg, request_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                write_id,
                timestamp,
                lightweight["model"],
                lightweight["channel_id"],
                lightweight["channel_name"],
                lightweight.get("api_key_id"),
                lightweight.get("client_ip"),
                1 if lightweight["is_stream"] else 0,
                int(lightweight.get("input_tokens") or 0),
                int(lightweight.get("output_tokens") or 0),
                int(lightweight.get("cache_read_input_tokens") or 0),
                int(lightweight.get("cache_creation_input_tokens") or 0),
                int(lightweight["latency_ms"]),
                lightweight.get("lag_ms"),
                lightweight.get("finish_reason"),
                1 if lightweight["success"] else 0,
                lightweight.get("error_msg"),
                lightweight.get("request_source") or "client",
            ),
        )


async def _write_record(record: dict[str, Any]) -> None:
    await asyncio.to_thread(_write_record_sync, record)


@_require_db()
def _write_context_shaping_sync(record: dict[str, Any]) -> None:
    """把实际 Context Shaping 动作写入日聚合。"""
    now = record_timestamp_to_iso(agg_now())
    offset_modifier = _agg_offset_sql()
    with _open_conn() as conn:
        write_id = record.get("_write_id")
        if write_id:
            inserted = conn.execute("INSERT OR IGNORE INTO write_behind_receipts (write_id) VALUES (?)", (write_id,))
            if inserted.rowcount == 0:
                return
        conn.execute(
            f"""
            INSERT INTO context_shaping_daily_stats
            (date, channel_id, model, feature, action, request_count, action_count,
             before_chars, after_chars, char_change, updated_at)
            VALUES (
                date(datetime('now', '{offset_modifier}')),
                ?, ?, ?, ?, 1, ?, ?, ?, ?, ?
            )
            ON CONFLICT(date, channel_id, model, feature, action) DO UPDATE SET
                request_count = request_count + 1,
                action_count = action_count + excluded.action_count,
                before_chars = before_chars + excluded.before_chars,
                after_chars = after_chars + excluded.after_chars,
                char_change = char_change + excluded.char_change,
                updated_at = excluded.updated_at
            """,
            (
                record["channel_id"],
                record["model"],
                record["feature"],
                record["action"],
                record["action_count"],
                record["before_chars"],
                record["after_chars"],
                record["char_change"],
                now,
            ),
        )


async def _write_context_shaping_record(record: dict[str, Any]) -> None:
    await asyncio.to_thread(_write_context_shaping_sync, record)


async def _stats_write(record: dict[str, Any]) -> None:
    """stats 写回调：分发 Context Shaping 动作或普通请求事实。"""
    if record.get("_type") == "context_shaping":
        await _write_context_shaping_record(record)
    else:
        await _write_record(record)


def _queue_params() -> WriteBehindParams:
    """队列参数快照：每次（重）建时重新读取模块全局（测试 monkeypatch 后立即生效）。"""
    return WriteBehindParams(
        worker_count=_STATS_WORKER_COUNT,
        overflow_path=os.path.join(config.DATA_DIR, _STATS_OVERFLOW_FILENAME),
        # 溢出落盘与 SQLite 写侧同口径剥离 _RAW_FIELDS——统计链路任何落盘形态都不保存请求原文
        overflow_serialize=_normalize_record,
        maxsize=_STATS_QUEUE_MAX_SIZE,
        write_timeout=_STATS_WRITE_TIMEOUT,
        name="stats",
    )


# 统计队列接线句柄：loop 检查/重建、启停、排空、重置钩子的实现全在共享模块（ADR-0013/D0），
# 差异（写回调/溢出路径与序列化/worker 数/maxsize/写超时）经 _queue_params 注入。
_wiring = WriteBehindWiring(write=_stats_write, params=_queue_params)


def _record_request_discard_warning(model: str) -> None:
    """库不可用空形回调：记账入口告警 + 丢弃（契约由 test_stats_workers 的 discarding 回归守门）。"""
    logger.warning(f"Stats database unavailable (not initialized); discarding record for model={model}")


@_require_db(empty=_record_request_discard_warning)
def record_request(
    channel_id: str,
    channel_name: str,
    model: str,
    is_stream: bool,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    success: bool,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    error_msg: str | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    lag_ms: int | None = None,
    finish_reason: str | None = None,
    request_source: str = "client",
) -> None:
    """将请求记录入队，由后台 worker 或 drain_queue 写入 SQLite。

    统计口径只含聚合字段（ADR-0014 D2）：headers/body 等原始字段与
    requested_model / api_type / sensitivity_info 仅日志侧维度由落库组装
    helper（proxy.request_record）直接投递 request_logs，不入本队列。
    库不可用时经单一守卫点（`_require_db`）告警并丢弃。
    """
    queue = _wiring.ensure_queue()
    if queue is None:
        return
    record = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "model": model,
        "is_stream": is_stream,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "latency_ms": latency_ms,
        "success": success,
        "error_msg": error_msg,
        "api_key_id": api_key_id,
        "client_ip": client_ip,
        "lag_ms": lag_ms,
        "finish_reason": finish_reason,
        "request_source": request_source,
    }
    queue.enqueue(record)


def _record_context_shaping_discard_warning(channel_id: str, model: str, **_: Any) -> None:
    logger.warning(f"Stats database unavailable (not initialized); discarding Context Shaping aggregate for channel={channel_id} model={model}")


@_require_db(empty=_record_context_shaping_discard_warning)
def record_context_shaping_action(
    *,
    channel_id: str,
    model: str,
    feature: str,
    action: str,
    action_count: int,
    before_chars: int,
    after_chars: int,
) -> None:
    """把同一请求内聚合后的一个 feature/action 动作入队。"""
    queue = _wiring.ensure_queue()
    if queue is None:
        return
    queue.enqueue(
        {
            "_type": "context_shaping",
            "channel_id": channel_id,
            "model": model,
            "feature": feature,
            "action": action,
            "action_count": action_count,
            "before_chars": before_chars,
            "after_chars": after_chars,
            "char_change": after_chars - before_chars,
        }
    )


def record_context_shaping_receipt(channel_id: str, model: str, receipt: dict[str, Any] | None) -> None:
    """把一次实际发网请求的 Receipt 收敛为 feature/action 日聚合。"""
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for row in receipt.get("actions", []) if receipt else []:
        key = (str(row["feature"]), str(row["action"]))
        values = grouped.setdefault(key, {"action_count": 0, "before_chars": 0, "after_chars": 0})
        values["action_count"] += int(row.get("hit_count") or 0)
        values["before_chars"] += int(row.get("before_chars") or 0)
        values["after_chars"] += int(row.get("after_chars") or 0)
    for (feature, action), values in grouped.items():
        record_context_shaping_action(
            channel_id=channel_id,
            model=model,
            feature=feature,
            action=action,
            **values,
        )


def start_stats_workers(worker_count: int | None = None) -> None:
    """启动统计写入后台 worker（per-call worker_count 覆盖，缺省用 STATS_WORKER_COUNT）。"""
    _wiring.start(worker_count)


async def stop_stats_workers():
    """停止统计写入后台 worker 并消费队列残留记录。"""
    await _wiring.stop()


async def close_pool():
    """保留旧名称；SQLite 版关闭/重置模块状态。"""
    global _DB_PATH, _DB_AVAILABLE
    await stop_stats_workers()
    _DB_PATH = None
    _DB_AVAILABLE = False
    # 重置队列句柄：下一次使用按当前参数重建全新队列（close-后-重建语义）
    _wiring.reset()


async def drain_queue() -> None:
    """消费当前队列中已入队的统计记录，主要供测试和优雅停机使用。"""
    await _wiring.drain()


async def wait_for_queue() -> None:
    """等待队列排空：与 drain_queue 排空等价；无 worker / 无句柄时安全返回。"""
    await _wiring.wait()


# ─── 聚合作业：日聚合重算与缺失回补 ───


def _daily_bounds(start_date: date, end_date: date) -> tuple[str, str]:
    tz = _agg_tz()
    start_local = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=tz)
    end_local = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).replace(tzinfo=tz)
    start_utc = start_local.astimezone(UTC).replace(tzinfo=None)
    end_utc = end_local.astimezone(UTC).replace(tzinfo=None)
    return record_timestamp_to_iso(start_utc), record_timestamp_to_iso(end_utc)


@_require_db(empty=lambda: {"updated_rows": 0})
def _aggregate_daily_stats_sync(start_date: date, end_date: date) -> dict[str, Any]:
    start_iso, end_iso = _daily_bounds(start_date, end_date)
    updated_at = record_timestamp_to_iso(agg_now())
    offset_modifier = _agg_offset_sql()
    with _open_conn() as conn:
        conn.execute(
            """
            DELETE FROM daily_stats
            WHERE date >= ? AND date <= ?
            """,
            (start_date.isoformat(), end_date.isoformat()),
        )
        conn.execute(
            f"""
            INSERT INTO daily_stats
            (date, channel_id, model, api_key_id, request_source, request_count, success_count, fail_count,
             input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
             avg_latency_ms, avg_lag_ms, updated_at)
            SELECT
                date(datetime(timestamp, '{offset_modifier}')) AS date,
                channel_id,
                model,
                COALESCE(api_key_id, '') AS api_key_id,
                COALESCE(request_source, 'client') AS request_source,
                COUNT(*) AS request_count,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                CAST(ROUND(AVG(latency_ms)) AS INTEGER) AS avg_latency_ms,
                CAST(ROUND(AVG(lag_ms)) AS INTEGER) AS avg_lag_ms,
                ? AS updated_at
            FROM request_stats_raw
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY date(datetime(timestamp, '{offset_modifier}')), channel_id, model, COALESCE(api_key_id, ''),
                     COALESCE(request_source, 'client')
            """,
            (updated_at, start_iso, end_iso),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM daily_stats WHERE date >= ? AND date <= ?",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchone()[0]
        return {"updated_rows": count or 0}


async def aggregate_daily_stats(start_date: date, end_date: date) -> dict[str, Any]:
    """手动触发指定日期范围的日聚合（按聚合时区切日）。"""
    return await asyncio.to_thread(_aggregate_daily_stats_sync, start_date, end_date)


def _refresh_missing_unavailable() -> dict[str, Any]:
    """库不可用空形：与可用路径的 debug 形态对齐，显式标记 db_available=False。"""
    return {"refreshed_dates": [], "count": 0, "debug": {"db_available": False}}


@_require_db(empty=_refresh_missing_unavailable)
def _refresh_missing_daily_stats_sync() -> dict[str, Any]:
    today = agg_now().date()
    offset_modifier = _agg_offset_sql()
    today_start_utc = local_date_to_utc_iso(today)
    with _open_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT date(datetime(timestamp, '{offset_modifier}')) AS d
            FROM request_stats_raw
            WHERE timestamp < ?
            ORDER BY d
            """,
            (today_start_utc,),
        ).fetchall()
        request_dates = {date.fromisoformat(row["d"]) for row in rows if row["d"]}
        if not request_dates:
            return {
                "refreshed_dates": [],
                "count": 0,
                "debug": {
                    "today": str(today),
                    "request_dates": [],
                    "missing_dates": [],
                },
            }
        existing_rows = conn.execute(
            """
            SELECT DISTINCT date FROM daily_stats
            WHERE date >= ? AND date <= ?
            """,
            (min(request_dates).isoformat(), max(request_dates).isoformat()),
        ).fetchall()
    existing_dates = {date.fromisoformat(row["date"]) for row in existing_rows}
    missing_dates = sorted(request_dates - existing_dates)
    for missing in missing_dates:
        _aggregate_daily_stats_sync(missing, missing)
    return {
        "refreshed_dates": [str(d) for d in missing_dates],
        "count": len(missing_dates),
        "debug": {
            "today": str(today),
            "request_dates": [str(d) for d in sorted(request_dates)],
            "existing_dates": [str(d) for d in sorted(existing_dates)],
            "missing_dates": [str(d) for d in missing_dates],
        },
    }


async def refresh_missing_daily_stats() -> dict[str, Any]:
    """自动补全 daily_stats 中缺失的历史日期（不含当天）。"""
    return await asyncio.to_thread(_refresh_missing_daily_stats_sync)


@_require_db(empty=lambda: {"backfilled_count": 0, "recent_refreshed_days": 0})
async def refresh_stats() -> dict[str, Any]:
    """统一刷新统计：补全缺失历史日聚合 + 强制刷新近3天日聚合。"""
    backfilled = await refresh_missing_daily_stats()
    today = agg_now().date()
    await aggregate_daily_stats(today - timedelta(days=2), today)
    return {
        "backfilled_count": backfilled.get("count", 0),
        "recent_refreshed_days": 3,
    }


# ─── 查询 API：日聚合 / 总体 / 今日 / api_key / 请求列表 ───


def _from_row(row: sqlite3.Row) -> dict[str, Any]:
    return normalize_bool_fields(dict(row))


@_require_db(empty=lambda: [])
def _get_daily_stats_sync(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
    request_source: str | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    start_date = agg_now().date() - timedelta(days=days - 1)
    conditions = ["date >= ?"]
    args: list[Any] = [start_date.isoformat()]
    if channel_id:
        conditions.append("channel_id = ?")
        args.append(channel_id)
    if model:
        conditions.append("model = ?")
        args.append(model)
    if api_key_id:
        conditions.append("api_key_id = ?")
        args.append(api_key_id)
    append_request_source_condition(conditions, args, request_source)
    with _open_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT date, channel_id, model, api_key_id, request_source, request_count, success_count,
                   fail_count, input_tokens, output_tokens,
                   cache_read_input_tokens, cache_creation_input_tokens,
                   avg_latency_ms, avg_lag_ms
            FROM daily_stats
            WHERE {" AND ".join(conditions)}
            ORDER BY date ASC
            """,
            args,
        ).fetchall()
        return [_from_row(row) for row in rows]


async def get_daily_stats(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
    request_source: str | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """查询日聚合统计。request_source 为单值或元组时按来源过滤；None 不过滤（保留全量）。"""
    return await asyncio.to_thread(_get_daily_stats_sync, days, channel_id, model, api_key_id, request_source)


@_require_db(empty=lambda: [])
def _get_context_shaping_daily_stats_sync(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    feature: str | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    start_date = agg_now().date() - timedelta(days=days - 1)
    conditions = ["date >= ?"]
    args: list[Any] = [start_date.isoformat()]
    for column, value in (("channel_id", channel_id), ("model", model), ("feature", feature), ("action", action)):
        if value:
            conditions.append(f"{column} = ?")
            args.append(value)
    with _open_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT date, channel_id, model, feature, action, request_count,
                   action_count, before_chars, after_chars, char_change
            FROM context_shaping_daily_stats
            WHERE {" AND ".join(conditions)}
            ORDER BY date ASC, feature ASC, action ASC
            """,
            args,
        ).fetchall()
        return [_from_row(row) for row in rows]


async def get_context_shaping_daily_stats(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    feature: str | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_get_context_shaping_daily_stats_sync, days, channel_id, model, feature, action)


def _sum_shaping_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        name: sum(int(row.get(name) or 0) for row in rows) for name in ("request_count", "action_count", "before_chars", "after_chars", "char_change")
    }


async def get_context_shaping_view(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    feature: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    """返回实际动作视图，不推导 Token 节省或因果。"""
    rows = await get_context_shaping_daily_stats(days, channel_id, model, feature, action)

    def grouped(column_names: tuple[str, ...]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in rows:
            key = tuple(str(row[name]) for name in column_names)
            buckets.setdefault(key, []).append(row)
        return [{**dict(zip(column_names, key, strict=True)), **_sum_shaping_rows(bucket)} for key, bucket in sorted(buckets.items())]

    return {
        "overall": _sum_shaping_rows(rows),
        "by_feature": grouped(("feature",)),
        "by_action": grouped(("feature", "action")),
        "daily": grouped(("date",)),
        "detail": rows,
        "meta": {
            "channels": sorted({row["channel_id"] for row in rows}),
            "models": sorted({row["model"] for row in rows}),
            "features": sorted({row["feature"] for row in rows}),
            "actions": sorted({row["action"] for row in rows}),
        },
    }


@_require_db(empty=lambda: [])
def _get_daily_stats_from_requests_sync(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
    request_source: str | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """从明细表实时聚合日统计。

    Why 仅在显式传 request_source 时才按来源拆分：未传参时保持旧行为形态
    （每日一行、无 source 键），get_today_stats 等调用方以取末行方式消费本函数，
    无条件拆分会在多源并存后取错行。
    """
    start_date = agg_now().date() - timedelta(days=days - 1)
    offset_modifier = _agg_offset_sql()
    day_expr = f"date(datetime(timestamp, '{offset_modifier}'))"
    conditions = ["timestamp >= ?"]
    args: list[Any] = [local_date_to_utc_iso(start_date)]
    if channel_id:
        conditions.append("channel_id = ?")
        args.append(channel_id)
    if model:
        conditions.append("model = ?")
        args.append(model)
    if api_key_id:
        conditions.append("api_key_id = ?")
        args.append(api_key_id)
    source_select = ""
    group_clause = day_expr
    if request_source:
        sources = (request_source,) if isinstance(request_source, str) else tuple(request_source)
        if sources:
            # 明细行经 COALESCE 归一后与聚合写入口径一致（写入侧从不落 NULL）
            placeholders = ", ".join("?" for _ in sources)
            conditions.append(f"COALESCE(request_source, 'client') IN ({placeholders})")
            args.extend(sources)
            source_select = ", COALESCE(request_source, 'client') AS request_source"
            group_clause = f"{day_expr}, COALESCE(request_source, 'client')"
    with _open_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                {day_expr} AS date{source_select},
                COUNT(*) AS request_count,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                CAST(ROUND(AVG(latency_ms)) AS INTEGER) AS avg_latency_ms,
                CAST(ROUND(AVG(lag_ms)) AS INTEGER) AS avg_lag_ms
            FROM request_stats_raw
            WHERE {" AND ".join(conditions)}
            GROUP BY {group_clause}
            ORDER BY date ASC
            """,
            args,
        ).fetchall()
        return [_from_row(row) for row in rows]


async def get_daily_stats_from_requests(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
    request_source: str | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """从明细表实时聚合日统计（request_source 过滤语义见 sync 层 docstring）。"""
    return await asyncio.to_thread(_get_daily_stats_from_requests_sync, days, channel_id, model, api_key_id, request_source)


def _merge_daily_slices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """切片行 → 按天一行的日合并（ADR-0024 D0 单一住所）。

    延迟均值沿用 avg×count 还原再加权数学（每切片 ROUND 舍入 ≤0.5ms 可忽略）；
    输出键序与原路由层日合并输出逐字段一致。
    """
    daily_by_date: dict[str, dict] = {}
    for row in rows:
        d = str(row["date"])
        if d not in daily_by_date:
            daily_by_date[d] = {
                "date": d,
                "total_requests": 0,
                "success_count": 0,
                "fail_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_read_input_tokens": 0,
                "total_cache_creation_input_tokens": 0,
                "total_latency_ms": 0,
                "total_lag_ms": 0,
                "latency_count": 0,
                "lag_count": 0,
            }
        rec = daily_by_date[d]
        rec["total_requests"] += row["request_count"] or 0
        rec["success_count"] += row["success_count"] or 0
        rec["fail_count"] += row["fail_count"] or 0
        rec["total_input_tokens"] += row["input_tokens"] or 0
        rec["total_output_tokens"] += row["output_tokens"] or 0
        rec["total_cache_read_input_tokens"] += row.get("cache_read_input_tokens") or 0
        rec["total_cache_creation_input_tokens"] += row.get("cache_creation_input_tokens") or 0
        if row.get("avg_latency_ms") is not None:
            rec["total_latency_ms"] += row["avg_latency_ms"] * (row["request_count"] or 1)
            rec["latency_count"] += row["request_count"] or 1
        if row.get("avg_lag_ms") is not None:
            rec["total_lag_ms"] += row["avg_lag_ms"] * (row["request_count"] or 1)
            rec["lag_count"] += row["request_count"] or 1
    daily = []
    for rec in daily_by_date.values():
        avg_latency = round(rec.pop("total_latency_ms") / rec["latency_count"]) if rec["latency_count"] else 0
        avg_lag = round(rec.pop("total_lag_ms") / rec["lag_count"]) if rec["lag_count"] else 0
        rec.pop("latency_count")
        rec.pop("lag_count")
        rec["avg_latency_ms"] = avg_latency
        rec["avg_lag_ms"] = avg_lag
        daily.append(rec)
    daily.sort(key=lambda r: r["date"])
    return daily


async def get_daily_rollup(
    days: int,
    request_source: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """日合并单一住所（ADR-0024 D0/D2）。

    切片→按天合并、延迟均值按 request_count 加权（沿用 avg×count 还原数学）；
    数据源优先 daily_stats，空则兜底明细实时聚合（fallback 语义与调试标记随迁）。
    today 实时行覆盖 daily_stats 当天行（补全机制明确排除当天，today 由实时路径
    提供——覆盖优先级与时区切日规则唯一住所本层；daily_stats 缺当天行时实时行补位）。

    返回 {"daily": [...], "fallback_used": bool, "mode": str, "raw_daily_count": int}；
    daily 形态与原路由层日合并输出逐字段一致，router 只透传。
    """
    raw_daily = await get_daily_stats(days=days, request_source=request_source)
    fallback_used = not bool(raw_daily)
    mode = "daily_stats"
    if fallback_used:
        mode = "realtime_fallback"
        raw_daily = await get_daily_stats_from_requests(days=days, request_source=request_source)
    daily = _merge_daily_slices(raw_daily)
    if not fallback_used:
        # 兜底路径本身即实时数据（已含当天），仅聚合表路径需要 today 实时覆盖
        today_rows = await get_daily_stats_from_requests(days=1, request_source=request_source)
        today_merged = _merge_daily_slices(today_rows)
        if today_merged:
            today_iso = agg_now().date().isoformat()
            daily = [rec for rec in daily if rec["date"] != today_iso]
            daily.extend(today_merged)
            daily.sort(key=lambda r: r["date"])
    return {"daily": daily, "fallback_used": fallback_used, "mode": mode, "raw_daily_count": len(raw_daily)}


def _overall_zero() -> dict[str, Any]:
    """总体统计零值空形（库不可用 / 无数据同形）。"""
    return {
        "total_requests": 0,
        "success_count": 0,
        "fail_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_input_tokens": 0,
        "total_cache_creation_input_tokens": 0,
        "avg_latency_ms": 0,
        "avg_lag_ms": 0,
        "channels": [],
        "models": [],
        "api_keys": [],
    }


@_require_db(empty=_overall_zero)
def _get_overall_stats_since_sync(since: str) -> dict[str, Any]:
    """总体统计唯一实现：since 为时间起点（naive UTC ISO），days 入口由其推导（ADR-0017 D2 孪生合并）。"""
    with _open_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_requests,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail_count,
                COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(cache_read_input_tokens), 0) AS total_cache_read_input_tokens,
                COALESCE(SUM(cache_creation_input_tokens), 0) AS total_cache_creation_input_tokens,
                COALESCE(CAST(ROUND(AVG(latency_ms)) AS INTEGER), 0) AS avg_latency_ms,
                COALESCE(CAST(ROUND(AVG(lag_ms)) AS INTEGER), 0) AS avg_lag_ms
            FROM request_stats_raw
            WHERE timestamp >= ?
            """,
            (since,),
        ).fetchone()
        channel_rows = conn.execute(
            """
            SELECT channel_name, COUNT(*) AS count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM request_stats_raw
            WHERE timestamp >= ?
            GROUP BY channel_id, channel_name
            ORDER BY count DESC
            """,
            (since,),
        ).fetchall()
        model_rows = conn.execute(
            """
            SELECT model, COUNT(*) AS count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM request_stats_raw
            WHERE timestamp >= ?
            GROUP BY model
            ORDER BY count DESC
            LIMIT 20
            """,
            (since,),
        ).fetchall()
        key_rows = conn.execute(
            """
            SELECT api_key_id, COUNT(*) AS count,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens
            FROM request_stats_raw
            WHERE api_key_id IS NOT NULL AND api_key_id != '' AND timestamp >= ?
            GROUP BY api_key_id
            ORDER BY count DESC
            """,
            (since,),
        ).fetchall()
    return {
        "total_requests": row["total_requests"] or 0,
        "success_count": row["success_count"] or 0,
        "fail_count": row["fail_count"] or 0,
        "total_input_tokens": row["total_input_tokens"] or 0,
        "total_output_tokens": row["total_output_tokens"] or 0,
        "total_cache_read_input_tokens": row["total_cache_read_input_tokens"] or 0,
        "total_cache_creation_input_tokens": row["total_cache_creation_input_tokens"] or 0,
        # D1 后端半（ADR-0024）：区间内按请求数加权的全区间均值（明细 AVG，纯新增向后兼容）
        "avg_latency_ms": row["avg_latency_ms"] or 0,
        "avg_lag_ms": row["avg_lag_ms"] or 0,
        "channels": [
            {
                "name": r["channel_name"],
                "count": r["count"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
            }
            for r in channel_rows
        ],
        "models": [
            {
                "name": r["model"],
                "count": r["count"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
            }
            for r in model_rows
        ],
        "api_keys": [
            {
                "key_id": r["api_key_id"],
                "count": r["count"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cache_read_input_tokens": r["cache_read_input_tokens"],
                "cache_creation_input_tokens": r["cache_creation_input_tokens"],
            }
            for r in key_rows
        ],
    }


def _overall_since_from_days(days: int) -> str:
    """days 参数推导时间起点：now(UTC) 往前推 days 天（naive UTC ISO）。"""
    return record_timestamp_to_iso(datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days))


async def get_overall_stats(days: int = 7) -> dict[str, Any]:
    """总体统计数据。"""
    return await asyncio.to_thread(_get_overall_stats_since_sync, _overall_since_from_days(days))


async def get_overall_stats_since(since: str) -> dict[str, Any]:
    """从指定 UTC 时间戳开始查询总体统计（naive UTC ISO 格式）。"""
    return await asyncio.to_thread(_get_overall_stats_since_sync, since)


async def get_today_stats() -> dict[str, Any]:
    """今天（聚合时区 0 点至今）的实时统计。

    库不可用无需自守卫：overall 与 daily 的 sync 实现均经单一守卫点返回零值/空形。
    """
    today = agg_now().date()
    start_of_today = local_date_to_utc_iso(today)
    overall = await asyncio.to_thread(_get_overall_stats_since_sync, start_of_today)
    daily_rows = await get_daily_stats_from_requests(days=1)
    daily = []
    if daily_rows:
        row = daily_rows[-1]
        daily = [
            {
                "date": str(row["date"]),
                "total_requests": row["request_count"] or 0,
                "success_count": row["success_count"] or 0,
                "fail_count": row["fail_count"] or 0,
                "total_input_tokens": row["input_tokens"] or 0,
                "total_output_tokens": row["output_tokens"] or 0,
                "total_cache_read_input_tokens": row["cache_read_input_tokens"] or 0,
                "total_cache_creation_input_tokens": row["cache_creation_input_tokens"] or 0,
                "avg_latency_ms": row["avg_latency_ms"] or 0,
                "avg_lag_ms": row["avg_lag_ms"] or 0,
            }
        ]
    return {"overall": overall, "daily": daily}


@_require_db(empty=lambda: {})
def _get_api_key_stats_sync() -> dict[str, dict[str, int]]:
    with _open_conn() as conn:
        rows = conn.execute(
            """
            SELECT api_key_id,
                   COUNT(*) AS request_count,
                   COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS total_cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS total_cache_creation_input_tokens
            FROM request_stats_raw
            WHERE api_key_id IS NOT NULL AND api_key_id != ''
            GROUP BY api_key_id
            """
        ).fetchall()
        return {
            row["api_key_id"]: {
                "request_count": row["request_count"],
                "total_input_tokens": row["total_input_tokens"],
                "total_output_tokens": row["total_output_tokens"],
                "total_cache_read_input_tokens": row["total_cache_read_input_tokens"],
                "total_cache_creation_input_tokens": row["total_cache_creation_input_tokens"],
            }
            for row in rows
        }


async def get_api_key_stats() -> dict[str, dict[str, int]]:
    """按 api_key_id 聚合全量统计数据。"""
    return await asyncio.to_thread(_get_api_key_stats_sync)


def _list_requests_empty(page: int = 1, page_size: int = 10) -> dict[str, Any]:
    """库不可用空形：分页值与可用路径同口径归一（page 下限 1、page_size 上限 100）。"""
    page, page_size = normalize_pagination(page, page_size)
    return {"items": [], "total": 0, "page": page, "page_size": page_size}


@_require_db(empty=_list_requests_empty)
def _list_requests_sync(
    model: str | None = None,
    channel: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    success: bool | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    is_stream: bool | None = None,
    page: int = 1,
    page_size: int = 10,
    request_source: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    page, page_size = normalize_pagination(page, page_size)

    # 九条件 WHERE / 汇总聚合 SQL 走共享查询模块（ADR-0017 D0）：与 request_logs 列表查询同一份方言
    where_clause, args = build_where_clause(
        model=model,
        channel=channel,
        start=start,
        end=end,
        success=success,
        api_key_id=api_key_id,
        client_ip=client_ip,
        is_stream=is_stream,
        request_source=request_source,
    )
    with _open_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM request_stats_raw WHERE {where_clause}",
            args,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                   client_ip, is_stream, input_tokens, output_tokens,
                   cache_read_input_tokens, cache_creation_input_tokens,
                   latency_ms, lag_ms, finish_reason, success, error_msg,
                   request_source
            FROM request_stats_raw
            WHERE {where_clause}
            ORDER BY timestamp DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*args, page_size, (page - 1) * page_size],
        ).fetchall()
        agg = conn.execute(build_summary_sql("request_stats_raw", where_clause), args).fetchone()
        summary = {
            "total_requests": total or 0,
            "success_count": agg["success_count"] or 0,
            "input_tokens": agg["input_tokens"],
            "output_tokens": agg["output_tokens"],
            "cache_read_input_tokens": agg["cache_read_input_tokens"],
            "avg_latency_ms": (agg["success_latency_sum"] / agg["success_latency_count"] if agg["success_latency_count"] else None),
            "avg_lag_ms": (agg["success_lag_sum"] / agg["success_lag_count"] if agg["success_lag_count"] else None),
        }
        return {
            "items": [_from_row(row) for row in rows],
            "total": total or 0,
            "page": page,
            "page_size": page_size,
            "summary": summary,
        }


async def list_requests(
    model: str | None = None,
    channel: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    success: bool | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    is_stream: bool | None = None,
    page: int = 1,
    page_size: int = 10,
    request_source: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """查询轻量请求记录（支持分页和过滤）。"""
    return await asyncio.to_thread(
        _list_requests_sync,
        model,
        channel,
        start,
        end,
        success,
        api_key_id,
        client_ip,
        is_stream,
        page,
        page_size,
        request_source,
    )
