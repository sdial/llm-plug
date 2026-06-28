"""Request log storage backend for debugging payloads."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from loguru import logger

import config

# 请求记录仅支持 SQLite3，不再扩展其他关系型数据库后端。
BACKEND = "sqlite3"

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
_REQUEST_WORKER_COUNT = 2
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
        "is_stream": row["is_stream"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
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

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def close(self) -> None:
        return  # SQLite backend needs no cleanup

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sync(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
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

                CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(timestamp);
                CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model);
                CREATE INDEX IF NOT EXISTS idx_request_logs_channel ON request_logs(channel_id, channel_name);
                CREATE INDEX IF NOT EXISTS idx_request_logs_api_key ON request_logs(api_key_id);
                """
            )

    @staticmethod
    def _json_dumps(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def _write_record_sync(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO request_logs
                (timestamp, model, channel_id, channel_name, api_key_id, is_stream,
                 input_tokens, output_tokens, latency_ms, lag_ms, finish_reason,
                 success, error_msg, request_headers, response_headers,
                 request_body, response_body)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _to_iso(record.get("timestamp")),
                    record["model"],
                    record["channel_id"],
                    record["channel_name"],
                    record.get("api_key_id"),
                    1 if record["is_stream"] else 0,
                    int(record.get("input_tokens") or 0),
                    int(record.get("output_tokens") or 0),
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

    def _list_requests_sync(
        self,
        model: str | None = None,
        channel: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        success: bool | None = None,
        api_key_id: str | None = None,
        is_stream: bool | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        page, page_size = _normalize_pagination(page, page_size)
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
        if is_stream is not None:
            conditions.append("is_stream = ?")
            args.append(1 if is_stream else 0)

        where_clause = " AND ".join(conditions)
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM request_logs WHERE {where_clause}",
                args,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                       is_stream, input_tokens, output_tokens, latency_ms, lag_ms,
                       finish_reason, success, error_msg
                FROM request_logs
                WHERE {where_clause}
                ORDER BY timestamp DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "available": True,
            "items": [_base_item_from_mapping(dict(row)) for row in rows],
            "total": total or 0,
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
            is_stream,
            page,
            page_size,
        )

    def _get_request_field_sync(self, request_id: int, field: str) -> dict | None:
        sql = _RAW_FIELD_SELECT.get(field)
        if sql is None:
            return None
        with self._connect() as conn:
            row = conn.execute(sql, (request_id,)).fetchone()
        if row is None:
            return None
        if row[field] is None:
            return {"data": None}
        return {"data": json.loads(row[field])}

    async def get_request_field(self, request_id: int, field: str) -> dict | None:
        return await asyncio.to_thread(self._get_request_field_sync, request_id, field)


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
    error_msg: str | None = None,
    api_key_id: str | None = None,
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
        "latency_ms": latency_ms,
        "success": success,
        "error_msg": error_msg,
        "api_key_id": api_key_id,
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
