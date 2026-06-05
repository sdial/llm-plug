"""Request log storage backend for debugging payloads."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger

import config

if TYPE_CHECKING:
    import asyncpg

# 64 MB mmap;走 OS page cache 共享内存,替代每连接私有 cache。
# 生产部署在 1 GB 内存的受限设备上,故保守取 64 MB。
_SQLITE_MMAP_SIZE_BYTES = 64 * 1024 * 1024

_RAW_FIELDS = {
    "request_headers",
    "response_headers",
    "request_body",
    "response_body",
}

_RAW_FIELD_SELECT_SQLITE: dict[str, str] = {
    "request_headers": "SELECT request_headers FROM request_logs WHERE id = ?",
    "response_headers": "SELECT response_headers FROM request_logs WHERE id = ?",
    "request_body": "SELECT request_body FROM request_logs WHERE id = ?",
    "response_body": "SELECT response_body FROM request_logs WHERE id = ?",
}

_RAW_FIELD_SELECT_POSTGRES: dict[str, str] = {
    "request_headers": "SELECT request_headers FROM request_logs WHERE id = $1",
    "response_headers": "SELECT response_headers FROM request_logs WHERE id = $1",
    "request_body": "SELECT request_body FROM request_logs WHERE id = $1",
    "response_body": "SELECT response_body FROM request_logs WHERE id = $1",
}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

_BACKEND_UNINITIALIZED_ERROR = "request log backend is not initialized"

_OVERFLOW_LOG_FILENAME = "request_logs_overflow.jsonl"

_backend: "_BaseRequestLogBackend | None" = None
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


def _ensure_sqlite_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


class _BaseRequestLogBackend:
    async def init(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def write_record(self, record: dict[str, Any]) -> None:
        raise NotImplementedError

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
        raise NotImplementedError

    async def get_request_field(self, request_id: int, field: str) -> dict | None:
        raise NotImplementedError

    async def cleanup_old_records(self, retention_days: int, raw_retention_days: int) -> dict:
        raise NotImplementedError


class SQLiteRequestLogBackend(_BaseRequestLogBackend):
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def close(self) -> None:
        return  # SQLite backend needs no cleanup

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute(f"PRAGMA mmap_size={_SQLITE_MMAP_SIZE_BYTES}")
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def _open_conn(self):
        """打开请求日志 DB 连接:保证 fd 释放 + 隐式事务的 commit/rollback。

        短连接配方的统一入口。直接 with self._connect() 只 commit/rollback,
        不会 close;本 helper 用 try/finally 兜底 close,异常路径同样可靠。
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_sync(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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
            _ensure_sqlite_columns(
                conn,
                "request_logs",
                {
                    "cache_read_input_tokens": "INTEGER NOT NULL DEFAULT 0",
                    "cache_creation_input_tokens": "INTEGER NOT NULL DEFAULT 0",
                },
            )

    @staticmethod
    def _json_dumps(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def _write_record_sync(self, record: dict[str, Any]) -> None:
        with self._open_conn() as conn:
            conn.execute(
                """
                INSERT INTO request_logs
                (timestamp, model, channel_id, channel_name, api_key_id, client_ip, is_stream,
                 input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                 latency_ms, lag_ms, finish_reason,
                 success, error_msg, request_headers, response_headers,
                 request_body, response_body)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _to_iso(record.get("timestamp")),
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
        if client_ip:
            conditions.append("client_ip LIKE ? ESCAPE '\\'")
            args.append(f"%{_escape_like(client_ip)}%")
        if is_stream is not None:
            conditions.append("is_stream = ?")
            args.append(1 if is_stream else 0)

        where_clause = " AND ".join(conditions)
        with self._open_conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM request_logs WHERE {where_clause}",
                args,
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                       client_ip, is_stream, input_tokens, output_tokens,
                       cache_read_input_tokens, cache_creation_input_tokens,
                       latency_ms, lag_ms, finish_reason, success, error_msg
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

    def _get_request_field_sync(self, request_id: int, field: str) -> dict | None:
        sql = _RAW_FIELD_SELECT_SQLITE.get(field)
        if sql is None:
            return None
        with self._open_conn() as conn:
            row = conn.execute(sql, (request_id,)).fetchone()
        if row is None:
            return None
        if row[field] is None:
            return {"data": None}
        return {"data": json.loads(row[field])}

    async def get_request_field(self, request_id: int, field: str) -> dict | None:
        return await asyncio.to_thread(self._get_request_field_sync, request_id, field)

    def _cleanup_old_records_sync(self, retention_days: int, raw_retention_days: int) -> dict:
        result = {"raw_fields_cleared": 0, "rows_deleted": 0}
        with self._open_conn() as conn:
            if raw_retention_days > 0 and (retention_days == 0 or raw_retention_days < retention_days):
                cur = conn.execute(
                    """
                    UPDATE request_logs
                    SET request_headers = NULL,
                        response_headers = NULL,
                        request_body = NULL,
                        response_body = NULL
                    WHERE timestamp < datetime('now', ?)
                      AND (request_headers IS NOT NULL
                           OR response_headers IS NOT NULL
                           OR request_body IS NOT NULL
                           OR response_body IS NOT NULL)
                    """,
                    (f"-{raw_retention_days} days",),
                )
                result["raw_fields_cleared"] = cur.rowcount
            if retention_days > 0:
                cur = conn.execute(
                    "DELETE FROM request_logs WHERE timestamp < datetime('now', ?)",
                    (f"-{retention_days} days",),
                )
                result["rows_deleted"] = cur.rowcount
        return result

    async def cleanup_old_records(self, retention_days: int, raw_retention_days: int) -> dict:
        return await asyncio.to_thread(self._cleanup_old_records_sync, retention_days, raw_retention_days)


class PostgresRequestLogBackend(_BaseRequestLogBackend):
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        import asyncpg
        if not self.database_url:
            raise ValueError("request_log_database_url is required for postgres request logs")
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=1,
            max_size=5,
            init=self._init_connection,
            timeout=5,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
                    model TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    api_key_id TEXT,
                    client_ip TEXT,
                    is_stream BOOLEAN NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL,
                    lag_ms INTEGER,
                    finish_reason TEXT,
                    success BOOLEAN NOT NULL,
                    error_msg TEXT,
                    request_headers JSONB,
                    response_headers JSONB,
                    request_body JSONB,
                    response_body JSONB
                );

                CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(timestamp);
                CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model);
                CREATE INDEX IF NOT EXISTS idx_request_logs_channel ON request_logs(channel_id, channel_name);
                CREATE INDEX IF NOT EXISTS idx_request_logs_api_key ON request_logs(api_key_id);
                CREATE INDEX IF NOT EXISTS idx_request_logs_client_ip ON request_logs(client_ip);
                """
            )
            await conn.execute(
                "ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS "
                "cache_read_input_tokens INTEGER NOT NULL DEFAULT 0"
            )
            await conn.execute(
                "ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS "
                "cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0"
            )

    async def _init_connection(self, conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("postgres request log backend is not initialized")
        return self.pool

    async def write_record(self, record: dict[str, Any]) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO request_logs
                (timestamp, model, channel_id, channel_name, api_key_id, client_ip, is_stream,
                 input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                 latency_ms, lag_ms, finish_reason,
                 success, error_msg, request_headers, response_headers,
                 request_body, response_body)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14, $15, $16, $17, $18, $19, $20)
                """,
                record.get("timestamp") or _utc_now(),
                record["model"],
                record["channel_id"],
                record["channel_name"],
                record.get("api_key_id"),
                record.get("client_ip"),
                bool(record["is_stream"]),
                int(record.get("input_tokens") or 0),
                int(record.get("output_tokens") or 0),
                int(record.get("cache_read_input_tokens") or 0),
                int(record.get("cache_creation_input_tokens") or 0),
                int(record["latency_ms"]),
                record.get("lag_ms"),
                record.get("finish_reason"),
                bool(record["success"]),
                record.get("error_msg"),
                record.get("request_headers"),
                record.get("response_headers"),
                record.get("request_body"),
                record.get("response_body"),
            )

    @staticmethod
    def _placeholder(args: list[Any], value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

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
        pool = self._require_pool()
        page, page_size = _normalize_pagination(page, page_size)
        conditions = ["1 = 1"]
        args: list[Any] = []
        if model:
            conditions.append(
                f"model ILIKE {self._placeholder(args, f'%{_escape_like(model)}%')} ESCAPE '\\'"
            )
        if channel:
            escaped = f"%{_escape_like(channel)}%"
            name_param = self._placeholder(args, escaped)
            id_param = self._placeholder(args, escaped)
            conditions.append(
                f"(channel_name ILIKE {name_param} ESCAPE '\\' "
                f"OR channel_id ILIKE {id_param} ESCAPE '\\')"
            )
        if start:
            conditions.append(f"timestamp >= {self._placeholder(args, _normalize_to_utc_aware(start))}")
        if end:
            conditions.append(f"timestamp < {self._placeholder(args, _normalize_to_utc_aware(end))}")
        if success is not None:
            conditions.append(f"success = {self._placeholder(args, bool(success))}")
        if api_key_id:
            conditions.append(f"api_key_id = {self._placeholder(args, api_key_id)}")
        if client_ip:
            conditions.append(
                f"client_ip ILIKE {self._placeholder(args, f'%{_escape_like(client_ip)}%')} ESCAPE '\\'"
            )
        if is_stream is not None:
            conditions.append(f"is_stream = {self._placeholder(args, bool(is_stream))}")

        where_clause = " AND ".join(conditions)
        limit_param = f"${len(args) + 1}"
        offset_param = f"${len(args) + 2}"
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM request_logs WHERE {where_clause}",
                *args,
            )
            rows = await conn.fetch(
                f"""
                SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                       client_ip, is_stream, input_tokens, output_tokens,
                       cache_read_input_tokens, cache_creation_input_tokens,
                       latency_ms, lag_ms, finish_reason, success, error_msg
                FROM request_logs
                WHERE {where_clause}
                ORDER BY timestamp DESC, id DESC
                LIMIT {limit_param} OFFSET {offset_param}
                """,
                *args,
                page_size,
                (page - 1) * page_size,
            )
        return {
            "available": True,
            "items": [_base_item_from_mapping(dict(row)) for row in rows],
            "total": total or 0,
            "page": page,
            "page_size": page_size,
        }

    async def get_request_field(self, request_id: int, field: str) -> dict | None:
        sql = _RAW_FIELD_SELECT_POSTGRES.get(field)
        if sql is None:
            return None
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, request_id)
        if row is None:
            return None
        if row[field] is None:
            return {"data": None}
        data = row[field]
        if isinstance(data, str):
            data = json.loads(data)
        return {"data": data}

    async def cleanup_old_records(self, retention_days: int, raw_retention_days: int) -> dict:
        pool = self._require_pool()
        result = {"raw_fields_cleared": 0, "rows_deleted": 0}
        async with pool.acquire() as conn:
            if raw_retention_days > 0 and (retention_days == 0 or raw_retention_days < retention_days):
                tag = await conn.execute(
                    """
                    UPDATE request_logs
                    SET request_headers = NULL,
                        response_headers = NULL,
                        request_body = NULL,
                        response_body = NULL
                    WHERE timestamp < NOW() - $1 * INTERVAL '1 day'
                      AND (request_headers IS NOT NULL
                           OR response_headers IS NOT NULL
                           OR request_body IS NOT NULL
                           OR response_body IS NOT NULL)
                    """,
                    raw_retention_days,
                )
                result["raw_fields_cleared"] = int(tag.split()[-1])
            if retention_days > 0:
                tag = await conn.execute(
                    "DELETE FROM request_logs WHERE timestamp < NOW() - $1 * INTERVAL '1 day'",
                    retention_days,
                )
                result["rows_deleted"] = int(tag.split()[-1])
        return result


def _build_backend(settings: dict | None = None) -> _BaseRequestLogBackend:
    db_type = str(_get_setting(settings, "request_log_db_type") or "sqlite").lower()
    if db_type == "sqlite":
        db_path = _get_setting(settings, "request_log_sqlite_path")
        if not db_path:
            db_path = os.path.join(config.DATA_DIR, "request_logs.db")
        return SQLiteRequestLogBackend(str(db_path))
    if db_type == "postgres":
        return PostgresRequestLogBackend(str(_get_setting(settings, "request_log_database_url") or ""))
    raise ValueError(f"unsupported request_log_db_type: {db_type}")


async def _create_initialized_backend(settings: dict | None = None) -> tuple[_BaseRequestLogBackend | None, dict]:
    backend: _BaseRequestLogBackend | None = None
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
        result = await backend.cleanup_old_records(r_days, raw_days)
        if result.get("raw_fields_cleared") or result.get("rows_deleted"):
            logger.info(
                f"Request log cleanup: cleared raw fields for {result['raw_fields_cleared']} rows, "
                f"deleted {result['rows_deleted']} rows "
                f"(raw_retention={raw_days}d, retention={r_days}d)"
            )
        return result
    except Exception as exc:
        logger.warning(f"Request log cleanup failed: {exc}")
        return {"error": str(exc), "raw_fields_cleared": 0, "rows_deleted": 0}
