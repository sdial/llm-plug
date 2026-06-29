"""SQLite-backed lightweight request statistics."""

import asyncio
import contextlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

import config

_DB_PATH: str | None = None
_DB_AVAILABLE = False
_DB_INIT_LOCK = asyncio.Lock()

_STATS_QUEUE: asyncio.Queue | None = None
_STATS_QUEUE_LOOP: asyncio.AbstractEventLoop | None = None
_STATS_WORKERS: list[asyncio.Task] = []
_STATS_QUEUE_MAX_SIZE = 1000
_STATS_WRITE_TIMEOUT = 60

_STATS_OVERFLOW_FILENAME = "stats_overflow.jsonl"

# 32 MB mmap;走 OS page cache 共享内存,替代每连接私有 cache。
# 统计库数据量小,32 MB 足够覆盖热数据。
_MMAP_SIZE_BYTES = 32 * 1024 * 1024

# ── PRAGMA 环境变量白名单 ──
_VALID_SYNCHRONOUS = {"OFF", "NORMAL", "FULL", "EXTRA", "0", "1", "2", "3"}
_VALID_TEMP_STORE = {"DEFAULT", "FILE", "MEMORY", "0", "1", "2"}
_VALID_JOURNAL_MODE = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}


def _sanitize_pragma_env(name: str, default: str, valid: set[str]) -> str:
    """读取环境变量并校验是否在白名单内,非法值回退到默认值并告警。"""
    val = os.environ.get(name, default)
    if val.upper() not in valid:
        logger.warning("非法 {}={!r}, 回退默认 {}", name, val, default)
        return default
    return val


def _sanitize_int_env(name: str | None, default: int | None) -> int | None:
    """读取整型环境变量,校验是否为合法整数,非法值回退到默认值并告警。"""
    if name is None:
        return default
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("非法 {}={!r} 不是整数,回退默认 {}", name, val, default)
        return default


_STATS_WORKER_COUNT = _sanitize_int_env("STATS_WORKER_COUNT", 4)


_RAW_FIELDS = {
    "request_headers",
    "response_headers",
    "request_body",
    "response_body",
}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _spill_to_overflow_file(record: dict[str, Any]) -> None:
    try:
        path = os.path.join(config.DATA_DIR, _STATS_OVERFLOW_FILENAME)
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.error(f"Failed to spill stats record to overflow file: {exc}")


def _agg_tz() -> tzinfo:
    """返回聚合时区：优先 settings.aggregation_timezone，否则系统本地时区。"""
    name = (config.get_setting("aggregation_timezone") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(f"Invalid aggregation_timezone {name!r}, falling back to system local")
    return datetime.now().astimezone().tzinfo or timezone.utc


def _agg_offset_seconds(at: datetime | None = None) -> int:
    """返回聚合时区相对 UTC 的偏移秒数（按指定时刻，处理 DST）。"""
    tz = _agg_tz()
    if at is None:
        at = datetime.now(timezone.utc)
    elif at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    offset = tz.utcoffset(at.astimezone(tz).replace(tzinfo=None))
    return int((offset or timedelta(0)).total_seconds())


def _agg_offset_sql(at: datetime | None = None) -> str:
    """SQLite datetime modifier，例如 '+28800 seconds'。"""
    seconds = _agg_offset_seconds(at)
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{abs(seconds)} seconds"


def agg_now() -> datetime:
    """返回聚合时区的当前时间（naive，仅用于日聚合切日与同时区运算）。"""
    return datetime.now(timezone.utc).astimezone(_agg_tz()).replace(tzinfo=None)


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
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"PRAGMA synchronous={_sanitize_pragma_env('SQLITE_SYNCHRONOUS', 'NORMAL', _VALID_SYNCHRONOUS)}")
    conn.execute(f"PRAGMA temp_store={_sanitize_pragma_env('SQLITE_TEMP_STORE', 'FILE', _VALID_TEMP_STORE)}")
    cache_size = _sanitize_int_env("SQLITE_CACHE_SIZE", None)
    if cache_size is not None:
        conn.execute(f"PRAGMA cache_size={cache_size}")
    conn.execute(f"PRAGMA mmap_size={_sanitize_int_env('SQLITE_MMAP_SIZE_STATS', _MMAP_SIZE_BYTES)}")
    conn.row_factory = sqlite3.Row
    return conn


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


def _to_iso(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="microseconds")


def _from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if "success" in data and data["success"] is not None:
        data["success"] = bool(data["success"])
    if "is_stream" in data and data["is_stream"] is not None:
        data["is_stream"] = bool(data["is_stream"])
    return data


def _ensure_sqlite_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _init_db_sync(db_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"PRAGMA journal_mode={_sanitize_pragma_env('SQLITE_JOURNAL_MODE', 'WAL', _VALID_JOURNAL_MODE)}")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS request_stats_raw (
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
                cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL,
                lag_ms INTEGER,
                finish_reason TEXT,
                success INTEGER NOT NULL,
                error_msg TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_id TEXT NOT NULL,
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
                PRIMARY KEY (date, channel_id, model, api_key_id)
            );

            CREATE TABLE IF NOT EXISTS hourly_stats (
                hour TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_id TEXT NOT NULL,
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
                PRIMARY KEY (hour, channel_id, model, api_key_id)
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


async def init_db(db_path: str | None = None) -> None:
    """初始化 SQLite 统计库。"""
    global _DB_PATH, _DB_AVAILABLE
    resolved_path = _resolve_db_path(db_path)
    async with _DB_INIT_LOCK:
        await asyncio.to_thread(_init_db_sync, resolved_path)
        _DB_PATH = resolved_path
        _DB_AVAILABLE = True


async def close_pool():
    """保留旧名称；SQLite 版关闭/重置模块状态。"""
    global _DB_PATH, _DB_AVAILABLE, _STATS_QUEUE, _STATS_QUEUE_LOOP
    await stop_stats_workers()
    _DB_PATH = None
    _DB_AVAILABLE = False
    _STATS_QUEUE = None
    _STATS_QUEUE_LOOP = None


def _ensure_queue() -> asyncio.Queue | None:
    global _STATS_QUEUE, _STATS_QUEUE_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Stats queue requires a running event loop; discarding record")
        return None
    if _STATS_QUEUE is None or _STATS_QUEUE_LOOP is not current_loop:
        _STATS_QUEUE = asyncio.Queue(maxsize=_STATS_QUEUE_MAX_SIZE)
        _STATS_QUEUE_LOOP = current_loop
    return _STATS_QUEUE


def start_stats_workers():
    """启动统计写入后台 worker。"""
    queue = _ensure_queue()
    if queue is None:
        return
    if _STATS_WORKERS:
        return
    for _ in range(_STATS_WORKER_COUNT):
        task = asyncio.create_task(_stats_worker())
        _STATS_WORKERS.append(task)
    logger.info(
        f"Stats workers started: {_STATS_WORKER_COUNT} workers, "
        f"queue max={queue.maxsize}, write timeout={_STATS_WRITE_TIMEOUT}s"
    )


async def stop_stats_workers():
    """停止统计写入后台 worker 并消费队列残留记录。"""
    global _STATS_QUEUE_LOOP
    for task in _STATS_WORKERS:
        task.cancel()
    for task in _STATS_WORKERS:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _STATS_WORKERS.clear()
    # Workers 已取消，drain 队列中未消费的记录
    await drain_queue()
    _STATS_QUEUE_LOOP = None


async def _stats_worker():
    while True:
        try:
            record = await _STATS_QUEUE.get()
            try:
                await asyncio.wait_for(_write_record(record), timeout=_STATS_WRITE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Stats write timed out ({_STATS_WRITE_TIMEOUT}s), "
                    f"discarding record for model={record.get('model')}"
                )
            except Exception as exc:
                logger.warning(f"Stats write failed: {exc}")
            finally:
                _STATS_QUEUE.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Stats worker error: {exc}")


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in _RAW_FIELDS}


def _write_record_sync(record: dict[str, Any]) -> None:
    if not _DB_AVAILABLE:
        return
    lightweight = _normalize_record(record)
    timestamp = lightweight.get("timestamp") or datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(timestamp, datetime):
        timestamp = _to_iso(timestamp)
    with _open_conn() as conn:
        conn.execute(
            """
            INSERT INTO request_stats_raw
            (timestamp, model, channel_id, channel_name, api_key_id, client_ip, is_stream,
             input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
             latency_ms, lag_ms, finish_reason,
             success, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
            ),
        )


async def _write_record(record: dict[str, Any]) -> None:
    await asyncio.to_thread(_write_record_sync, record)


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
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    request_body: dict | None = None,
    response_body: dict | None = None,
    lag_ms: int | None = None,
    finish_reason: str | None = None,
) -> None:
    """将请求记录入队，由后台 worker 或 drain_queue 写入 SQLite。"""
    if not _DB_AVAILABLE:
        return
    queue = _ensure_queue()
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
        "request_headers": request_headers,
        "response_headers": response_headers,
        "request_body": request_body,
        "response_body": response_body,
        "lag_ms": lag_ms,
        "finish_reason": finish_reason,
    }
    try:
        queue.put_nowait(record)
    except asyncio.QueueFull:
        logger.warning(
            f"Stats queue full ({_STATS_QUEUE_MAX_SIZE}); "
            f"spilling record for model={model} to overflow file"
        )
        _spill_to_overflow_file(record)


async def drain_queue() -> None:
    """消费当前队列中已入队的统计记录，主要供测试和优雅停机使用。"""
    queue = _STATS_QUEUE
    if queue is None:
        return
    while _STATS_WORKERS == [] and not queue.empty():
        record = await queue.get()
        try:
            await _write_record(record)
        finally:
            queue.task_done()
    await queue.join()


def _daily_bounds(start_date: date, end_date: date) -> tuple[str, str]:
    tz = _agg_tz()
    start_local = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=tz)
    end_local = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).replace(tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return _to_iso(start_utc), _to_iso(end_utc)


def _aggregate_daily_stats_sync(start_date: date, end_date: date) -> dict[str, Any]:
    if not _DB_AVAILABLE:
        return {"updated_rows": 0}
    start_iso, end_iso = _daily_bounds(start_date, end_date)
    updated_at = _to_iso(agg_now())
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
            (date, channel_id, model, api_key_id, request_count, success_count, fail_count,
             input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
             avg_latency_ms, avg_lag_ms, updated_at)
            SELECT
                date(datetime(timestamp, '{offset_modifier}')) AS date,
                channel_id,
                model,
                COALESCE(api_key_id, '') AS api_key_id,
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
            GROUP BY date, channel_id, model, COALESCE(api_key_id, '')
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


def local_date_to_utc_iso(local_date: date) -> str:
    """将聚合时区某日 0 点转为 naive UTC 的 ISO 字符串（DB timestamp 用）。"""
    tz = _agg_tz()
    local = datetime.combine(local_date, datetime.min.time()).replace(tzinfo=tz)
    return _to_iso(local.astimezone(timezone.utc).replace(tzinfo=None))


def _get_daily_stats_sync(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    if not _DB_AVAILABLE:
        return []
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
    with _open_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT date, channel_id, model, api_key_id, request_count, success_count,
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
) -> list[dict[str, Any]]:
    """查询日聚合统计。"""
    return await asyncio.to_thread(_get_daily_stats_sync, days, channel_id, model, api_key_id)


def _get_daily_stats_from_requests_sync(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    if not _DB_AVAILABLE:
        return []
    start_date = agg_now().date() - timedelta(days=days - 1)
    offset_modifier = _agg_offset_sql()
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
    with _open_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                date(datetime(timestamp, '{offset_modifier}')) AS date,
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
            GROUP BY date(datetime(timestamp, '{offset_modifier}'))
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
) -> list[dict[str, Any]]:
    """从明细表实时聚合日统计。"""
    return await asyncio.to_thread(_get_daily_stats_from_requests_sync, days, channel_id, model, api_key_id)


def _refresh_missing_daily_stats_sync() -> dict[str, Any]:
    if not _DB_AVAILABLE:
        return {"refreshed_dates": [], "count": 0, "debug": {"db_available": False}}
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
                "debug": {"today": str(today), "request_dates": [], "missing_dates": []},
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


async def refresh_stats() -> dict[str, Any]:
    """统一刷新统计：补全缺失历史日聚合 + 强制刷新近3天日聚合。"""
    if not _DB_AVAILABLE:
        return {"backfilled_count": 0, "recent_refreshed_days": 0}
    backfilled = await refresh_missing_daily_stats()
    today = agg_now().date()
    await aggregate_daily_stats(today - timedelta(days=2), today)
    return {
        "backfilled_count": backfilled.get("count", 0),
        "recent_refreshed_days": 3,
    }


def _overall_zero() -> dict[str, Any]:
    return {
        "total_requests": 0,
        "success_count": 0,
        "fail_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_input_tokens": 0,
        "total_cache_creation_input_tokens": 0,
        "channels": [],
        "models": [],
        "api_keys": [],
    }


def _get_overall_stats_sync(days: int = 7) -> dict[str, Any]:
    if not _DB_AVAILABLE:
        return _overall_zero()
    since = _to_iso(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days))
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
                COALESCE(SUM(cache_creation_input_tokens), 0) AS total_cache_creation_input_tokens
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


async def get_overall_stats(days: int = 7) -> dict[str, Any]:
    """总体统计数据。"""
    return await asyncio.to_thread(_get_overall_stats_sync, days)


async def get_overall_stats_since(since: str) -> dict[str, Any]:
    """从指定 UTC 时间戳开始查询总体统计（naive UTC ISO 格式）。"""
    return await asyncio.to_thread(_get_overall_stats_since_sync, since)


async def get_today_stats() -> dict[str, Any]:
    """今天（聚合时区 0 点至今）的实时统计。"""
    if not _DB_AVAILABLE:
        return {"overall": _overall_zero(), "daily": []}
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


def _get_overall_stats_since_sync(since: str) -> dict[str, Any]:
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
                COALESCE(SUM(cache_creation_input_tokens), 0) AS total_cache_creation_input_tokens
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


def _get_api_key_stats_sync() -> dict[str, dict[str, int]]:
    if not _DB_AVAILABLE:
        return {}
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


async def get_request_field(request_id: int, field: str) -> dict | None:  # noqa: ARG001
    """统计库不保存 headers/body，始终返回 None。"""
    return None


def _to_db_utc_iso(value: datetime) -> str:
    """将任意 datetime（aware/naive）归一为 naive UTC ISO，与 DB 中的 timestamp 字符串可比。"""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return _to_iso(value)


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
) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    if not _DB_AVAILABLE:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    conditions = ["1 = 1"]
    args: list[Any] = []
    if model:
        conditions.append("LOWER(model) LIKE LOWER(?) ESCAPE '\\'")
        args.append(f"%{_escape_like(model)}%")
    if channel:
        conditions.append(
            "(LOWER(channel_name) LIKE LOWER(?) ESCAPE '\\' "
            "OR LOWER(channel_id) LIKE LOWER(?) ESCAPE '\\')"
        )
        escaped = f"%{_escape_like(channel)}%"
        args.extend([escaped, escaped])
    if start:
        conditions.append("timestamp >= ?")
        args.append(_to_db_utc_iso(start))
    if end:
        conditions.append("timestamp < ?")
        args.append(_to_db_utc_iso(end))
    if success is not None:
        conditions.append("success = ?")
        args.append(1 if success else 0)
    if api_key_id:
        conditions.append("api_key_id = ?")
        args.append(api_key_id)
    if client_ip:
        conditions.append("client_ip LIKE ? ESCAPE '\\'")
        args.append(f"%{_escape_like(client_ip)}%")
    if is_stream is not None:
        conditions.append("is_stream = ?")
        args.append(1 if is_stream else 0)

    where_clause = " AND ".join(conditions)
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
                   latency_ms, lag_ms, finish_reason, success, error_msg
            FROM request_stats_raw
            WHERE {where_clause}
            ORDER BY timestamp DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*args, page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "items": [_from_row(row) for row in rows],
            "total": total or 0,
            "page": page,
            "page_size": page_size,
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
    )


def _list_tables_for_test_sync() -> set[str]:
    if not _DB_AVAILABLE:
        return set()
    with _open_conn() as conn:
        rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
        return {row["name"] for row in rows}


async def _list_tables_for_test() -> set[str]:
    return await asyncio.to_thread(_list_tables_for_test_sync)
