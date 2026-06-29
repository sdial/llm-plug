"""Request log storage backend for debugging payloads."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

import config

# 请求记录仅支持 SQLite3，不再扩展其他关系型数据库后端。
BACKEND = "sqlite3"

_SQLITE_MMAP_SIZE_BYTES = 64 * 1024 * 1024
_VALID_SYNCHRONOUS = {"OFF", "NORMAL", "FULL", "EXTRA", "0", "1", "2", "3"}
_VALID_TEMP_STORE = {"DEFAULT", "FILE", "MEMORY", "0", "1", "2"}
_VALID_JOURNAL_MODE = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}


def _sanitize_pragma_env(name: str, default: str, valid: set[str]) -> str:
    val = os.environ.get(name, default)
    if val.upper() not in valid:
        logger.warning("非法 {}={!r}, 回退默认 {}", name, val, default)
        return default
    return val


def _sanitize_int_env(name: str | None, default: int | None) -> int | None:
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

_RAW_FIELDS = {
    "request_headers",
    "response_headers",
    "request_body",
    "response_body",
}

_RAW_FIELD_SELECT: dict[str, str] = {
    "request_headers": "SELECT request_headers FROM request_logs WHERE id = ?",
    "response_headers": "SELECT response_headers FROM request_logs WHERE id = ?",
    "request_body": "SELECT request_body FROM request_logs WHERE id = ?",
    "response_body": "SELECT response_body FROM request_logs WHERE id = ?",
}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

_BACKEND_UNINITIALIZED_ERROR = "request log backend is not initialized"

_OVERFLOW_LOG_FILENAME = "request_logs_overflow.jsonl"

_backend: SQLiteRequestLogBackend | None = None
_backend_error = _BACKEND_UNINITIALIZED_ERROR
_backend_lock = asyncio.Lock()

_REQUEST_QUEUE: asyncio.Queue | None = None
_REQUEST_QUEUE_LOOP: asyncio.AbstractEventLoop | None = None
_REQUEST_QUEUE_MAX_SIZE = 1000
_REQUEST_WORKERS: list[asyncio.Task] = []
_REQUEST_WORKER_COUNT = _sanitize_int_env("REQUEST_LOG_WORKER_COUNT", 2)
_REQUEST_WRITE_TIMEOUT = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_to_utc_aware(value: datetime) -> datetime:
    """naive 输入按 UTC 解释；aware 转 UTC。返回 aware UTC datetime。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_to_utc_naive(value: datetime) -> datetime:
    """返回 naive UTC datetime（SQLite TEXT 时间戳比较用）。"""
    return _normalize_to_utc_aware(value).replace(tzinfo=None)


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="microseconds")
    if value:
        return str(value)
    return _utc_now().isoformat(sep=" ", timespec="microseconds")


def _normalize_pagination(page: int = 1, page_size: int = 10) -> tuple[int, int]:
    return max(1, page), max(1, min(page_size, 100))


def _get_setting(settings: dict | None, key: str) -> Any:
    if settings is not None and key in settings:
        return settings[key]
    return config.get_setting(key)


def _get_save_flags() -> dict[str, bool]:
    return {
        "save_request_headers": bool(config.get_setting("save_request_headers")),
        "save_response_headers": bool(config.get_setting("save_response_headers")),
        "save_request_body": bool(config.get_setting("save_request_body")),
        "save_response_body": bool(config.get_setting("save_response_body")),
    }


def _raw_field_allowed(field: str) -> bool:
    return field in _RAW_FIELDS


def _base_item_from_mapping(row: dict[str, Any]) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "model": row["model"],
        "channel_id": row["channel_id"],
        "channel_name": row["channel_name"],
        "api_key_id": row.get("api_key_id"),
        "client_ip": row.get("client_ip"),
        "is_stream": row["is_stream"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_input_tokens": row.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": row.get("cache_creation_input_tokens", 0),
        "latency_ms": row["latency_ms"],
        "lag_ms": row.get("lag_ms"),
        "finish_reason": row.get("finish_reason"),
        "success": row["success"],
        "error_msg": row.get("error_msg"),
    }
    if isinstance(data["timestamp"], datetime):
        data["timestamp"] = data["timestamp"].isoformat()
    if data["success"] is not None:
        data["success"] = bool(data["success"])
    if data["is_stream"] is not None:
        data["is_stream"] = bool(data["is_stream"])
    return data


class SQLiteRequestLogBackend:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.data_dir = os.path.dirname(os.path.abspath(db_path))

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def close(self) -> None:
        return

    @staticmethod
    def _current_year_month() -> str:
        return _utc_now().strftime("%Y%m")

    @property
    def _logs_dir(self) -> str:
        return os.path.join(self.data_dir, "request_raw_logs")

    def _month_db_path(self, year_month: str) -> str:
        return os.path.join(self._logs_dir, f"request_logs_{year_month[:4]}_{year_month[4:]}.sqlite3")

    def _discover_month_dbs(self) -> list[str]:
        if not os.path.isdir(self._logs_dir):
            return []
        months: list[str] = []
        for path in glob.glob(os.path.join(self._logs_dir, "request_logs_????_??.sqlite3")):
            basename = os.path.basename(path)
            parts = basename.replace("request_logs_", "").replace(".sqlite3", "").split("_")
            if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
                months.append(parts[0] + parts[1])
        return sorted(months)

    def _connect_to(self, db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(f"PRAGMA synchronous={_sanitize_pragma_env('SQLITE_SYNCHRONOUS', 'NORMAL', _VALID_SYNCHRONOUS)}")
        conn.execute(f"PRAGMA temp_store={_sanitize_pragma_env('SQLITE_TEMP_STORE', 'FILE', _VALID_TEMP_STORE)}")
        cache_size = _sanitize_int_env("SQLITE_CACHE_SIZE", None)
        if cache_size is not None:
            conn.execute(f"PRAGMA cache_size={cache_size}")
        conn.execute(f"PRAGMA mmap_size={_sanitize_int_env('SQLITE_MMAP_SIZE_LOGS', _SQLITE_MMAP_SIZE_BYTES)}")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_month_db(self, year_month: str) -> str:
        path = self._month_db_path(year_month)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(f"PRAGMA journal_mode={_sanitize_pragma_env('SQLITE_JOURNAL_MODE', 'WAL', _VALID_JOURNAL_MODE)}")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS request_logs (
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
                        error_msg TEXT,
                        request_headers TEXT,
                        response_headers TEXT,
                        request_body TEXT,
                        response_body TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_channel ON request_logs(channel_id, channel_name);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_api_key ON request_logs(api_key_id);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_client_ip ON request_logs(client_ip);
                    """
                )
        else:
            with closing(sqlite3.connect(path)) as conn, conn:
                existing = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)")}
                migrations = {
                    "client_ip": "TEXT",
                    "cache_read_input_tokens": "INTEGER NOT NULL DEFAULT 0",
                    "cache_creation_input_tokens": "INTEGER NOT NULL DEFAULT 0",
                }
                for column, definition in migrations.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE request_logs ADD COLUMN {column} {definition}")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_client_ip ON request_logs(client_ip)")
        return path

    def _init_sync(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        self._ensure_month_db(self._current_year_month())

    @staticmethod
    def _json_dumps(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _parse_request_id(request_id: int | str) -> tuple[str | None, int]:
        raw = str(request_id)
        if "_" in raw:
            month, local_id = raw.split("_", 1)
            if month.isdigit() and len(month) == 6:
                return month, int(local_id)
        return None, int(raw)

    def _write_record_sync(self, record: dict[str, Any]) -> None:
        ts_str = _to_iso(record.get("timestamp"))
        ym = ts_str[:4] + ts_str[5:7] if len(ts_str) >= 7 else self._current_year_month()
        db_path = self._ensure_month_db(ym)
        with closing(self._connect_to(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO request_logs
                (timestamp, model, channel_id, channel_name, api_key_id, client_ip, is_stream,
                 input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                 latency_ms, lag_ms, finish_reason, success, error_msg,
                 request_headers, response_headers, request_body, response_body)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_str,
                    record["model"],
                    record["channel_id"],
                    record["channel_name"],
                    record.get("api_key_id"),
                    record.get("client_ip"),
                    1 if record["is_stream"] else 0,
                    int(record.get("input_tokens") or 0),
                    int(record.get("output_tokens") or 0),
                    int(record.get("cache_read_input_tokens") or 0),
                    int(record.get("cache_creation_input_tokens") or 0),
                    int(record["latency_ms"]),
                    record.get("lag_ms"),
                    record.get("finish_reason"),
                    1 if record["success"] else 0,
                    record.get("error_msg"),
                    self._json_dumps(record.get("request_headers")),
                    self._json_dumps(record.get("response_headers")),
                    self._json_dumps(record.get("request_body")),
                    self._json_dumps(record.get("response_body")),
                ),
            )

    async def write_record(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self._write_record_sync, record)

    def _query_single_month(
        self,
        db_path: str,
        model: str | None,
        channel: str | None,
        start: datetime | None,
        end: datetime | None,
        success: bool | None,
        api_key_id: str | None,
        client_ip: str | None,
        is_stream: bool | None,
    ) -> list[dict[str, Any]]:
        conditions = ["1 = 1"]
        args: list[Any] = []
        if model:
            conditions.append("LOWER(model) LIKE LOWER(?) ESCAPE '\\'")
            args.append(f"%{_escape_like(model)}%")
        if channel:
            conditions.append("(LOWER(channel_name) LIKE LOWER(?) ESCAPE '\\' OR LOWER(channel_id) LIKE LOWER(?) ESCAPE '\\')")
            escaped = f"%{_escape_like(channel)}%"
            args.extend([escaped, escaped])
        if start:
            conditions.append("timestamp >= ?")
            args.append(_to_iso(_normalize_to_utc_naive(start)))
        if end:
            conditions.append("timestamp < ?")
            args.append(_to_iso(_normalize_to_utc_naive(end)))
        if success is not None:
            conditions.append("success = ?")
            args.append(1 if success else 0)
        if api_key_id:
            conditions.append("api_key_id = ?")
            args.append(api_key_id)
        if client_ip:
            conditions.append("LOWER(client_ip) LIKE LOWER(?) ESCAPE '\\'")
            args.append(f"%{_escape_like(client_ip)}%")
        if is_stream is not None:
            conditions.append("is_stream = ?")
            args.append(1 if is_stream else 0)
        where_clause = " AND ".join(conditions)
        with closing(self._connect_to(db_path)) as conn:
            rows = conn.execute(
                f"""
                SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                       client_ip, is_stream, input_tokens, output_tokens,
                       cache_read_input_tokens, cache_creation_input_tokens,
                       latency_ms, lag_ms, finish_reason, success, error_msg
                FROM request_logs
                WHERE {where_clause}
                """,
                args,
            ).fetchall()
        items = []
        for row in rows:
            item = _base_item_from_mapping(dict(row))
            item["id"] = f"{os.path.basename(db_path)[13:17]}{os.path.basename(db_path)[18:20]}_{item['id']}"
            items.append(item)
        return items

    def _list_requests_sync(
        self,
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
        page, page_size = _normalize_pagination(page, page_size)
        months = sorted(set(self._discover_month_dbs() + [self._current_year_month()]))
        items: list[dict[str, Any]] = []
        for month in months:
            db_path = self._month_db_path(month)
            if not os.path.exists(db_path):
                continue
            items.extend(self._query_single_month(db_path, model, channel, start, end, success, api_key_id, client_ip, is_stream))
        items.sort(key=lambda item: (item["timestamp"], item["id"]), reverse=True)
        total = len(items)
        offset = (page - 1) * page_size
        return {
            "available": True,
            "items": items[offset:offset + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def list_requests(
        self,
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
        return await asyncio.to_thread(
            self._list_requests_sync,
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

    def _get_request_field_sync(self, request_id: int | str, field: str) -> dict | None:
        sql = _RAW_FIELD_SELECT.get(field)
        if sql is None:
            return None
        month, local_id = self._parse_request_id(request_id)
        search_months = [month] if month else reversed(self._discover_month_dbs())
        for candidate in search_months:
            if candidate is None:
                continue
            db_path = self._month_db_path(candidate)
            if not os.path.exists(db_path):
                continue
            with closing(self._connect_to(db_path)) as conn:
                row = conn.execute(sql, (local_id,)).fetchone()
            if row is not None:
                if row[field] is None:
                    return {"data": None}
                return {"data": json.loads(row[field])}
        return None

    async def get_request_field(self, request_id: int | str, field: str) -> dict | None:
        return await asyncio.to_thread(self._get_request_field_sync, request_id, field)

    def _cleanup_old_records_sync(self, retention_days: int, raw_retention_days: int) -> dict[str, int]:
        result = {"raw_fields_cleared": 0, "rows_deleted": 0}
        for month in self._discover_month_dbs():
            db_path = self._month_db_path(month)
            if not os.path.exists(db_path):
                continue
            with closing(self._connect_to(db_path)) as conn, conn:
                if raw_retention_days > 0 and (retention_days == 0 or raw_retention_days < retention_days):
                    cutoff = _to_iso(_normalize_to_utc_naive(_utc_now() - timedelta(days=raw_retention_days)))
                    cursor = conn.execute(
                        """
                        UPDATE request_logs
                        SET request_headers = NULL,
                            response_headers = NULL,
                            request_body = NULL,
                            response_body = NULL
                        WHERE timestamp < ?
                          AND (request_headers IS NOT NULL
                               OR response_headers IS NOT NULL
                               OR request_body IS NOT NULL
                               OR response_body IS NOT NULL)
                        """,
                        (cutoff,),
                    )
                    result["raw_fields_cleared"] += cursor.rowcount if cursor.rowcount is not None else 0
                if retention_days > 0:
                    cutoff = _to_iso(_normalize_to_utc_naive(_utc_now() - timedelta(days=retention_days)))
                    cursor = conn.execute("DELETE FROM request_logs WHERE timestamp < ?", (cutoff,))
                    result["rows_deleted"] += cursor.rowcount if cursor.rowcount is not None else 0
        return result

    async def cleanup_old_records(self, retention_days: int, raw_retention_days: int) -> dict[str, int]:
        return await asyncio.to_thread(self._cleanup_old_records_sync, retention_days, raw_retention_days)

def _build_backend(settings: dict | None = None) -> SQLiteRequestLogBackend:
    db_path = _get_setting(settings, "request_log_sqlite_path")
    if not db_path:
        db_path = os.path.join(config.DATA_DIR, "request_logs.db")
    return SQLiteRequestLogBackend(str(db_path))


async def _create_initialized_backend(settings: dict | None = None) -> tuple[SQLiteRequestLogBackend | None, dict]:
    backend: SQLiteRequestLogBackend | None = None
    try:
        backend = _build_backend(settings)
        await backend.init()
        return backend, {"available": True}
    except Exception as exc:
        if backend is not None:
            try:
                await backend.close()
            except Exception:
                pass
        logger.warning(f"Request log backend init failed: {exc}")
        return None, {"available": False, "error": str(exc)}


async def init_backend(settings: dict | None = None) -> dict:
    global _backend, _backend_error
    async with _backend_lock:
        new_backend, result = await _create_initialized_backend(settings)
        old_backend = _backend
        if result.get("available"):
            _backend = new_backend
            _backend_error = ""
            if old_backend is not None and old_backend is not new_backend:
                await old_backend.close()
        else:
            _backend = None
            _backend_error = result.get("error") or _BACKEND_UNINITIALIZED_ERROR
            if old_backend is not None:
                await old_backend.close()
        return result


async def reload_backend(settings: dict | None = None) -> dict:
    global _backend, _backend_error
    async with _backend_lock:
        new_backend, result = await _create_initialized_backend(settings)
        if not result.get("available"):
            return result
        old_backend = _backend
        _backend = new_backend
        _backend_error = ""
        if old_backend is not None and old_backend is not new_backend:
            await old_backend.close()
        return result


async def close_backend() -> None:
    global _backend, _backend_error, _REQUEST_QUEUE, _REQUEST_QUEUE_LOOP
    await stop_request_log_workers()
    async with _backend_lock:
        backend = _backend
        _backend = None
        _backend_error = _BACKEND_UNINITIALIZED_ERROR
        _REQUEST_QUEUE = None
        _REQUEST_QUEUE_LOOP = None
        if backend is not None:
            await backend.close()


def _ensure_queue() -> asyncio.Queue | None:
    global _REQUEST_QUEUE, _REQUEST_QUEUE_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Request log queue requires a running event loop; discarding record")
        return None
    if _REQUEST_QUEUE is None or _REQUEST_QUEUE_LOOP is not current_loop:
        _REQUEST_QUEUE = asyncio.Queue(maxsize=_REQUEST_QUEUE_MAX_SIZE)
        _REQUEST_QUEUE_LOOP = current_loop
    return _REQUEST_QUEUE


def start_request_log_workers(worker_count: int | None = None) -> None:
    queue = _ensure_queue()
    if queue is None:
        return
    if _REQUEST_WORKERS:
        return
    count = worker_count or _REQUEST_WORKER_COUNT
    for _ in range(count):
        _REQUEST_WORKERS.append(asyncio.create_task(_request_log_worker()))
    logger.info(f"Request log workers started: {count} workers, queue max={queue.maxsize}")


async def stop_request_log_workers() -> None:
    global _REQUEST_QUEUE, _REQUEST_QUEUE_LOOP
    for task in _REQUEST_WORKERS:
        task.cancel()
    for task in _REQUEST_WORKERS:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _REQUEST_WORKERS.clear()
    _REQUEST_QUEUE = None
    _REQUEST_QUEUE_LOOP = None


async def _request_log_worker() -> None:
    while True:
        try:
            queue = _REQUEST_QUEUE
            if queue is None:
                await asyncio.sleep(0)
                continue
            record = await queue.get()
            try:
                backend = _backend
                if backend is None:
                    logger.warning(
                        f"Request log backend unavailable ({_backend_error}); discarding queued record "
                        f"for model={record.get('model')}"
                    )
                else:
                    await asyncio.wait_for(backend.write_record(record), timeout=_REQUEST_WRITE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Request log write timed out ({_REQUEST_WRITE_TIMEOUT}s), "
                    f"discarding record for model={record.get('model')}"
                )
            except Exception as exc:
                logger.warning(f"Request log write failed: {exc}")
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Request log worker error: {exc}")


def _filtered_raw_value(flags: dict[str, bool], flag_name: str, value: Any) -> Any:
    if not flags.get(flag_name):
        return None
    return _truncate_raw_value(value)


def _truncate_raw_value(value: Any) -> Any:
    if value is None:
        return None
    max_bytes = config.get_setting("max_log_body_size")
    try:
        limit = int(max_bytes) if max_bytes is not None else 0
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return value
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return value
    raw = encoded.encode("utf-8")
    if len(raw) <= limit:
        return value
    truncated = raw[:limit].decode("utf-8", errors="ignore")
    return {
        "_truncated": True,
        "_original_bytes": len(raw),
        "_limit_bytes": limit,
        "_preview": truncated,
    }


def _spill_to_overflow_file(record: dict[str, Any]) -> None:
    try:
        path = os.path.join(config.DATA_DIR, _OVERFLOW_LOG_FILENAME)
        os.makedirs(config.DATA_DIR, exist_ok=True)
        payload = dict(record)
        ts = payload.get("timestamp")
        if isinstance(ts, datetime):
            payload["timestamp"] = ts.isoformat(sep=" ", timespec="microseconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.error(f"Failed to spill request log to overflow file: {exc}")


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
    if _backend is None:
        logger.warning(f"Request log backend unavailable ({_backend_error}); discarding record for model={model}")
        return
    queue = _ensure_queue()
    if queue is None:
        return
    flags = _get_save_flags()
    record = {
        "timestamp": _utc_now(),
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
        "request_headers": _filtered_raw_value(flags, "save_request_headers", request_headers),
        "response_headers": _filtered_raw_value(flags, "save_response_headers", response_headers),
        "request_body": _filtered_raw_value(flags, "save_request_body", request_body),
        "response_body": _filtered_raw_value(flags, "save_response_body", response_body),
        "lag_ms": lag_ms,
        "finish_reason": finish_reason,
    }
    try:
        queue.put_nowait(record)
    except asyncio.QueueFull:
        logger.warning(
            f"Request log queue full ({_REQUEST_QUEUE_MAX_SIZE}); "
            f"spilling record for model={model} to overflow file"
        )
        _spill_to_overflow_file(record)


async def drain_queue() -> None:
    queue = _REQUEST_QUEUE
    if queue is None:
        return
    while not _REQUEST_WORKERS and not queue.empty():
        record = await queue.get()
        try:
            backend = _backend
            if backend is None:
                logger.warning(
                    f"Request log backend unavailable ({_backend_error}); discarding queued record "
                    f"for model={record.get('model')}"
                )
            else:
                await backend.write_record(record)
        except Exception as exc:
            logger.warning(f"Request log write failed: {exc}")
        finally:
            queue.task_done()
    await queue.join()


async def wait_for_queue() -> None:
    queue = _REQUEST_QUEUE
    if queue is not None:
        await queue.join()


def _unavailable_result(page: int, page_size: int) -> dict[str, Any]:
    page, page_size = _normalize_pagination(page, page_size)
    return {
        "available": False,
        "error": _backend_error or _BACKEND_UNINITIALIZED_ERROR,
        "items": [],
        "total": 0,
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
    backend = _backend
    if backend is None:
        return _unavailable_result(page, page_size)
    try:
        return await backend.list_requests(
            model=model,
            channel=channel,
            start=start,
            end=end,
            success=success,
            api_key_id=api_key_id,
            client_ip=client_ip,
            is_stream=is_stream,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        logger.warning(f"Request log list failed: {exc}")
        result = _unavailable_result(page, page_size)
        result["error"] = str(exc)
        return result


async def get_request_field(request_id: int, field: str) -> dict | None:
    if not _raw_field_allowed(field):
        return None
    backend = _backend
    if backend is None:
        logger.warning(f"Request log backend unavailable ({_backend_error}); cannot read {field}")
        return None
    try:
        return await backend.get_request_field(request_id, field)
    except Exception as exc:
        logger.warning(f"Request log field read failed: {exc}")
        return None

async def cleanup_old_records(
    retention_days: int | None = None,
    raw_retention_days: int | None = None,
) -> dict[str, Any]:
    backend = _backend
    if backend is None:
        return {"error": _backend_error, "raw_fields_cleared": 0, "rows_deleted": 0}
    r_days = retention_days if retention_days is not None else int(config.get_setting("request_log_retention_days") or 0)
    raw_days = raw_retention_days if raw_retention_days is not None else int(config.get_setting("request_log_raw_retention_days") or 0)
    try:
        return await backend.cleanup_old_records(r_days, raw_days)
    except Exception as exc:
        logger.warning(f"Request log cleanup failed: {exc}")
        return {"error": str(exc), "raw_fields_cleared": 0, "rows_deleted": 0}
