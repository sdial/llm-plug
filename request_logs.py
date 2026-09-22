"""Request log storage backend for debugging payloads."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

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
    build_summary_sql,
    build_where_clause,
    normalize_bool_fields,
    normalize_pagination,
    normalize_query_time,
    record_timestamp_to_iso,
    to_utc_naive_datetime,
)

# 请求记录仅支持 SQLite3，不再扩展其他关系型数据库后端。
BACKEND = "sqlite3"

# 请求来源枚举（ADR-0009）：存储层与查询入口共用的唯一事实源，校验方从此导入。
REQUEST_SOURCES = ("client", "group_probe", "admin_test")

_SQLITE_MMAP_SIZE_BYTES = 64 * 1024 * 1024


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

_RAW_CAPTURE_FLAGS = {
    "request_headers": "save_request_headers",
    "response_headers": "save_response_headers",
    "request_body": "save_request_body",
    "response_body": "save_response_body",
}


_BACKEND_UNINITIALIZED_ERROR = "request log backend is not initialized"

_OVERFLOW_LOG_FILENAME = "request_logs_overflow.jsonl"

_backend: SQLiteRequestLogBackend | None = None
_backend_error = _BACKEND_UNINITIALIZED_ERROR
_backend_lock = asyncio.Lock()

_REQUEST_QUEUE_MAX_SIZE = 1000
_REQUEST_WORKER_COUNT = _sanitize_int_env("REQUEST_LOG_WORKER_COUNT", 2)
_REQUEST_WRITE_TIMEOUT = 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        "requested_model": row.get("requested_model"),
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
        "sensitivity_info": _safe_json_loads(row.get("sensitivity_info")),
        "conversion_info": _safe_json_loads(row.get("conversion_info")),
        "shaping_info": _safe_json_loads(row.get("shaping_info")),
        "api_type": row.get("api_type"),
        "request_source": row.get("request_source"),
    }
    if isinstance(data["timestamp"], datetime):
        data["timestamp"] = data["timestamp"].isoformat()
    return normalize_bool_fields(data)


def _safe_json_loads(value: Any) -> Any:
    """将 JSON 字符串安全解析为 Python 对象；失败时返回原值。"""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


class SQLiteRequestLogBackend:
    def __init__(self, db_path: str, logs_dir: str | None = None):
        """db_path 指向聚合入口库（其所在目录即 data_dir）。

        logs_dir 显式指定月度分库目录，缺省为 data_dir/request_raw_logs；
        存储管理页等纯视图以显式 logs_dir 绑定任意月库目录（ADR-0017 D1）。
        """
        self.db_path = db_path
        self.data_dir = os.path.dirname(os.path.abspath(db_path))
        self.logs_dir = logs_dir if logs_dir else os.path.join(self.data_dir, "request_raw_logs")

    # db/-wal/-shm 伴随文件三件套后缀——伴随文件知识的唯一定义处（ADR-0017 D1）
    _SQLITE_SIDECAR_SUFFIXES = ("", "-wal", "-shm")

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    async def close(self) -> None:
        return

    @staticmethod
    def _current_year_month() -> str:
        return _utc_now().strftime("%Y%m")

    @staticmethod
    def is_valid_month_key(year_month: str) -> bool:
        """月度分库键（YYYYMM 六位数字）的唯一校验口径。"""
        return len(year_month) == 6 and year_month.isdigit()

    @staticmethod
    def _month_end_utc_naive(year_month: str) -> datetime:
        year = int(year_month[:4])
        month = int(year_month[4:])
        if month == 12:
            return datetime(year + 1, 1, 1)
        return datetime(year, month + 1, 1)

    def month_db_path(self, year_month: str) -> str:
        """月份键 → 月度库文件路径；文件名格式 request_logs_{y}_{m}.sqlite3 的唯一定义处（ADR-0017 D1）。"""
        return os.path.join(self.logs_dir, f"request_logs_{year_month[:4]}_{year_month[4:]}.sqlite3")

    def discover_month_dbs(self) -> list[str]:
        """扫描月库目录，返回升序月份键列表。

        容差归一（ADR-0017 D1）：统一带数字校验，非数字命名的杂散文件在所有
        消费方（列表查询 / raw 字段读取 / 保留清理 / 存储管理页）一致被忽略。
        """
        if not os.path.isdir(self.logs_dir):
            return []
        months: list[str] = []
        for path in glob.glob(os.path.join(self.logs_dir, "request_logs_????_??.sqlite3")):
            basename = os.path.basename(path)
            parts = basename.replace("request_logs_", "").replace(".sqlite3", "").split("_")
            if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2 and parts[0].isdigit() and parts[1].isdigit():
                months.append(parts[0] + parts[1])
        return sorted(months)

    def month_db_sibling_files(self, db_path: str) -> list[tuple[str, int]]:
        """月度库伴随文件三件套（db/-wal/-shm）中实际存在的文件 [(路径, 字节大小)]；只列出，不删除。"""
        siblings: list[tuple[str, int]] = []
        for suffix in self._SQLITE_SIDECAR_SUFFIXES:
            path = db_path + suffix
            if os.path.exists(path):
                try:
                    siblings.append((path, os.path.getsize(path)))
                except OSError:
                    pass
        return siblings

    def remove_month_db_files(self, db_path: str) -> list[tuple[str, int]]:
        """删除月度库及其 -wal/-shm 伴随文件，返回成功删除的 [(文件名, 字节大小)]；单个文件失败告警跳过。"""
        removed: list[tuple[str, int]] = []
        for path, _size in self.month_db_sibling_files(db_path):
            try:
                size = os.path.getsize(path)
                os.remove(path)
                removed.append((os.path.basename(path), size))
            except OSError as exc:
                logger.warning(f"Failed to remove {path}: {exc}")
        return removed

    def month_db_record_count(self, db_path: str) -> int:
        """月度库记录数（存储管理页详情视图消费）；表缺失或库损坏返回 0。

        行数统计走统一连接工厂 `_connect_to`——storage_stats 等视图不得自行连接 SQLite（ADR-0017 D1）。
        """
        record_count = 0
        try:
            with closing(self._connect_to(db_path)) as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='request_logs'")
                if cursor.fetchone() is not None:
                    cursor = conn.execute("SELECT COUNT(*) FROM request_logs")
                    row = cursor.fetchone()
                    if row is not None:
                        record_count = int(row[0])
        except sqlite3.DatabaseError as exc:
            logger.debug(f"Database error reading {db_path}: {exc}")
            record_count = 0
        return record_count

    def _connect_to(self, db_path: str) -> sqlite3.Connection:
        return create_connection(db_path, mmap_size=("SQLITE_MMAP_SIZE_LOGS", _SQLITE_MMAP_SIZE_BYTES))

    def _ensure_month_db(self, year_month: str) -> str:
        path = self.month_db_path(year_month)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(f"PRAGMA journal_mode={_sanitize_pragma_env('SQLITE_JOURNAL_MODE', 'WAL', _VALID_JOURNAL_MODE)}")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        write_id TEXT UNIQUE,
                        request_ref TEXT,
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
                        response_body TEXT,
                        sensitivity_info TEXT,
                        conversion_info TEXT,
                        shaping_info TEXT,
                        api_type TEXT,
                        raw_capture_info TEXT,
                        raw_cleared_at TEXT,
                        request_source TEXT NOT NULL DEFAULT 'client'
                    );

                    CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_list_order ON request_logs(timestamp DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_channel ON request_logs(channel_id, channel_name);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_api_key ON request_logs(api_key_id);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_client_ip ON request_logs(client_ip);
                    CREATE INDEX IF NOT EXISTS idx_request_logs_source ON request_logs(request_source, timestamp DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_request_logs_request_ref ON request_logs(request_ref) WHERE request_ref IS NOT NULL;
                    """
                )
        else:
            self._migrate_month_db(path)
        return path

    @staticmethod
    def _migrate_month_db(db_path: str) -> None:
        """补齐旧月度库缺失的列与索引；幂等，可对任意历史月份重复执行。

        Why 启动迁移必须覆盖全部历史月度库：漏迁的旧月份在跨月 SELECT 新列时抛
        OperationalError，会被 _list_requests_sync 的 except 静默吞掉，整月历史
        从列表里消失。
        """
        with closing(sqlite3.connect(db_path)) as conn, conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)").fetchall()}
            if "sensitivity_info" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN sensitivity_info TEXT")
            if "conversion_info" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN conversion_info TEXT")
            if "shaping_info" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN shaping_info TEXT")
            if "api_type" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN api_type TEXT")
            if "request_source" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN request_source TEXT NOT NULL DEFAULT 'client'")
            if "write_id" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN write_id TEXT")
            if "request_ref" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN request_ref TEXT")
            if "raw_capture_info" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN raw_capture_info TEXT")
            if "raw_cleared_at" not in cols:
                conn.execute("ALTER TABLE request_logs ADD COLUMN raw_cleared_at TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_source ON request_logs(request_source, timestamp DESC)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_request_logs_write_id ON request_logs(write_id) WHERE write_id IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_request_logs_request_ref ON request_logs(request_ref) WHERE request_ref IS NOT NULL")

    def _init_sync(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        self._ensure_month_db(self._current_year_month())
        # 全部历史月度库逐一补列（含当月重复调用，迁移幂等）；单个损坏文件不阻断启动，
        # 查询侧本就按月容错跳过。
        for month in self.discover_month_dbs():
            db_path = self.month_db_path(month)
            if not os.path.exists(db_path):
                continue
            try:
                self._migrate_month_db(db_path)
            except sqlite3.Error as exc:
                logger.warning(f"Skip migrating request log month db {db_path}: {exc}")

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
            if SQLiteRequestLogBackend.is_valid_month_key(month):
                return month, int(local_id)
        return None, int(raw)

    def _write_record_sync(self, record: dict[str, Any]) -> None:
        ts_str = record_timestamp_to_iso(record.get("timestamp"))
        ym = ts_str[:4] + ts_str[5:7] if len(ts_str) >= 7 else self._current_year_month()
        db_path = self._ensure_month_db(ym)
        with closing(self._connect_to(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO request_logs
                (write_id, request_ref, timestamp, model, requested_model, channel_id, channel_name, api_key_id, client_ip,
                 is_stream, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                 latency_ms, lag_ms, finish_reason, success, error_msg,
                 request_headers, response_headers, request_body, response_body, sensitivity_info, conversion_info, shaping_info,
                 api_type, raw_capture_info,
                 request_source)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("_write_id"),
                    record.get("request_ref"),
                    ts_str,
                    record["model"],
                    record.get("requested_model"),
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
                    self._json_dumps(record.get("sensitivity_info")),
                    self._json_dumps(record.get("conversion_info")),
                    self._json_dumps(record.get("shaping_info")),
                    record.get("api_type"),
                    self._json_dumps(record.get("raw_capture_info")),
                    record.get("request_source") or "client",
                ),
            )

    async def write_record(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self._write_record_sync, record)

    def _query_single_month_page(
        self,
        month: str,
        db_path: str,
        model: str | None,
        channel: str | None,
        start: datetime | None,
        end: datetime | None,
        success: bool | None,
        api_key_id: str | None,
        client_ip: str | None,
        is_stream: bool | None,
        request_source: str | tuple[str, ...] | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int, dict[str, float | int]]:
        # 九条件 WHERE / 汇总聚合 SQL 走共享查询模块（ADR-0017 D0）：与 stats 列表查询同一份方言
        where_clause, args = build_where_clause(model, channel, start, end, success, api_key_id, client_ip, is_stream, request_source)
        with closing(self._connect_to(db_path)) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM request_logs WHERE {where_clause}",
                args,
            ).fetchone()[0]
            if limit <= 0:
                rows = []
            else:
                rows = conn.execute(
                    f"""
                    SELECT id, timestamp, model, requested_model, channel_id, channel_name, api_key_id,
                           client_ip, is_stream, input_tokens, output_tokens,
                           cache_read_input_tokens, cache_creation_input_tokens,
                           latency_ms, lag_ms, finish_reason, success, error_msg, sensitivity_info, conversion_info, shaping_info,
                           api_type, request_source
                    FROM request_logs
                    WHERE {where_clause}
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*args, limit, offset],
                ).fetchall()
            agg = conn.execute(
                build_summary_sql("request_logs", where_clause),
                args,
            ).fetchone()
        summary = {
            "total": total or 0,
            "success_count": agg["success_count"] or 0,
            "input_tokens": agg["input_tokens"],
            "output_tokens": agg["output_tokens"],
            "cache_read_input_tokens": agg["cache_read_input_tokens"],
            "success_latency_sum": agg["success_latency_sum"],
            "success_latency_count": agg["success_latency_count"] or 0,
            "success_lag_sum": agg["success_lag_sum"],
            "success_lag_count": agg["success_lag_count"] or 0,
        }
        items = []
        for row in rows:
            item = _base_item_from_mapping(dict(row))
            # 复合 id 月份前缀由月键生成（ADR-0017 D1）：与记录所在月一致，不再从文件名固定位置切片
            item["id"] = f"{month}_{item['id']}"
            items.append(item)
        return items, total or 0, summary

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
        request_source: str | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        page, page_size = normalize_pagination(page, page_size)
        months = sorted(set(self.discover_month_dbs() + [self._current_year_month()]), reverse=True)
        target_offset = (page - 1) * page_size
        remaining_skip = target_offset
        collected: list[dict[str, Any]] = []
        total = 0
        agg = {
            "total": 0,
            "success_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "success_latency_sum": 0,
            "success_latency_count": 0,
            "success_lag_sum": 0,
            "success_lag_count": 0,
        }
        for month in months:
            db_path = self.month_db_path(month)
            if not os.path.exists(db_path):
                continue
            limit = max(page_size - len(collected), 0)
            query_limit = 0 if limit == 0 and remaining_skip == 0 else limit
            try:
                rows, month_total, month_agg = self._query_single_month_page(
                    month,
                    db_path,
                    model,
                    channel,
                    start,
                    end,
                    success,
                    api_key_id,
                    client_ip,
                    is_stream,
                    request_source,
                    query_limit,
                    remaining_skip,
                )
            except sqlite3.OperationalError:
                continue
            total += month_total
            for key in agg:
                agg[key] += month_agg[key]
            if remaining_skip >= month_total:
                remaining_skip -= month_total
                continue
            remaining_skip = 0
            if limit > 0:
                collected.extend(rows)
        summary = {
            "total_requests": agg["total"],
            "success_count": agg["success_count"],
            "input_tokens": agg["input_tokens"],
            "output_tokens": agg["output_tokens"],
            "cache_read_input_tokens": agg["cache_read_input_tokens"],
            "avg_latency_ms": (agg["success_latency_sum"] / agg["success_latency_count"] if agg["success_latency_count"] else None),
            "avg_lag_ms": (agg["success_lag_sum"] / agg["success_lag_count"] if agg["success_lag_count"] else None),
        }
        return {
            "available": True,
            "items": collected[:page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "summary": summary,
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
        request_source: str | tuple[str, ...] | None = None,
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
            request_source,
        )

    def _get_request_field_sync(self, request_id: int | str, field: str) -> dict | None:
        sql = _RAW_FIELD_SELECT.get(field)
        if sql is None:
            return None
        month, local_id = self._parse_request_id(request_id)
        search_months = [month] if month else reversed(self.discover_month_dbs())
        for candidate in search_months:
            if candidate is None:
                continue
            db_path = self.month_db_path(candidate)
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

    def _get_raw_info_sync(self, request_ref: str, timestamp: str, fields: tuple[str, ...]) -> dict[str, Any]:
        """按 Request Reference 只读取事件所属月份，绝不退化为跨月猜测。"""
        ym = timestamp[:4] + timestamp[5:7] if len(timestamp) >= 7 else ""
        if not self.is_valid_month_key(ym):
            return {"status": "invalid_timestamp", "fields": {}}
        db_path = self.month_db_path(ym)
        if not os.path.exists(db_path):
            return {"status": "month_missing", "fields": {}}
        columns = ", ".join(["raw_capture_info", "raw_cleared_at", *fields])
        with closing(self._connect_to(db_path)) as conn:
            row = conn.execute(f"SELECT {columns} FROM request_logs WHERE request_ref = ?", (request_ref,)).fetchone()
        if row is None:
            return {"status": "record_missing", "fields": {}}
        capture = json.loads(row["raw_capture_info"]) if row["raw_capture_info"] else {}
        result: dict[str, str] = {}
        for field in fields:
            if not capture.get(field, False):
                result[field] = "not_captured"
            elif row["raw_cleared_at"] is not None:
                result[field] = "cleared"
            elif row[field] is None:
                result[field] = "not_captured"
            else:
                result[field] = "available"
        return {"status": "available", "fields": result}

    async def get_raw_info(self, request_ref: str, timestamp: str, fields: tuple[str, ...] = tuple(_RAW_FIELDS)) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_raw_info_sync, request_ref, timestamp, fields)

    def _get_request_field_by_reference_sync(self, request_ref: str, timestamp: str, field: str) -> dict | None:
        info = self._get_raw_info_sync(request_ref, timestamp, (field,))
        if info.get("fields", {}).get(field) != "available":
            return None
        ym = timestamp[:4] + timestamp[5:7]
        with closing(self._connect_to(self.month_db_path(ym))) as conn:
            row = conn.execute(f"SELECT {field} FROM request_logs WHERE request_ref = ?", (request_ref,)).fetchone()
        return {"data": json.loads(row[field])} if row is not None else None

    async def get_request_field_by_reference(self, request_ref: str, timestamp: str, field: str) -> dict | None:
        return await asyncio.to_thread(self._get_request_field_by_reference_sync, request_ref, timestamp, field)

    def _cleanup_old_records_sync(self, retention_days: int, raw_retention_days: int) -> dict[str, int]:
        result = {"raw_fields_cleared": 0, "rows_deleted": 0, "month_dbs_deleted": 0}
        retention_cutoff_dt = to_utc_naive_datetime(_utc_now() - timedelta(days=retention_days)) if retention_days > 0 else None
        retention_cutoff = record_timestamp_to_iso(retention_cutoff_dt) if retention_cutoff_dt else None
        raw_cutoff = None
        if raw_retention_days > 0 and (retention_days == 0 or raw_retention_days < retention_days):
            raw_cutoff = normalize_query_time(_utc_now() - timedelta(days=raw_retention_days))
        for month in self.discover_month_dbs():
            db_path = self.month_db_path(month)
            if not os.path.exists(db_path):
                continue
            if retention_cutoff_dt and self._month_end_utc_naive(month) <= retention_cutoff_dt:
                self.remove_month_db_files(db_path)
                result["month_dbs_deleted"] += 1
                continue
            mutated = False
            with closing(self._connect_to(db_path)) as conn, conn:
                if raw_cutoff is not None:
                    cursor = conn.execute(
                        """
                        UPDATE request_logs
                        SET request_headers = NULL,
                            response_headers = NULL,
                            request_body = NULL,
                            response_body = NULL,
                            raw_cleared_at = ?
                        WHERE timestamp < ?
                          AND (request_headers IS NOT NULL
                               OR response_headers IS NOT NULL
                               OR request_body IS NOT NULL
                               OR response_body IS NOT NULL)
                        """,
                        (record_timestamp_to_iso(_utc_now()), raw_cutoff),
                    )
                    changed = cursor.rowcount if cursor.rowcount is not None else 0
                    result["raw_fields_cleared"] += changed
                    mutated = mutated or changed > 0
                if retention_cutoff is not None:
                    cursor = conn.execute(
                        "DELETE FROM request_logs WHERE timestamp < ?",
                        (retention_cutoff,),
                    )
                    changed = cursor.rowcount if cursor.rowcount is not None else 0
                    result["rows_deleted"] += changed
                    mutated = mutated or changed > 0
            if mutated and os.path.exists(db_path):
                with closing(self._connect_to(db_path)) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return result

    async def cleanup_old_records(self, retention_days: int, raw_retention_days: int) -> dict[str, int]:
        return await asyncio.to_thread(self._cleanup_old_records_sync, retention_days, raw_retention_days)


def _build_backend(settings: dict | None = None) -> SQLiteRequestLogBackend:
    db_path = _get_setting(settings, "request_log_sqlite_path")
    if not db_path:
        db_path = os.path.join(config.DATA_DIR, "request_logs.db")
    return SQLiteRequestLogBackend(str(db_path))


async def _create_initialized_backend(
    settings: dict | None = None,
) -> tuple[SQLiteRequestLogBackend | None, dict]:
    backend: SQLiteRequestLogBackend | None = None
    try:
        backend = _build_backend(settings)
        await backend.init()
        return backend, {"available": True}
    except Exception as exc:
        if backend is not None:
            with contextlib.suppress(Exception):
                await backend.close()
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
    global _backend, _backend_error
    await stop_request_log_workers()
    async with _backend_lock:
        backend = _backend
        _backend = None
        _backend_error = _BACKEND_UNINITIALIZED_ERROR
        # 重置队列句柄：下一次使用按当前参数重建全新队列（close-后-重建语义）
        _wiring.reset()
        if backend is not None:
            await backend.close()


async def _request_log_write(record: dict[str, Any]) -> None:
    """reqlog 写回调（注入共享队列）：消费时晚绑定 _backend，不可用则丢弃并告警。"""
    backend = _backend
    if backend is None:
        logger.warning(f"Request log backend unavailable ({_backend_error}); discarding queued record for model={record.get('model')}")
        return
    await backend.write_record(record)


def _serialize_overflow(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    ts = payload.get("timestamp")
    if isinstance(ts, datetime):
        payload["timestamp"] = record_timestamp_to_iso(ts)
    return payload


def _queue_params() -> WriteBehindParams:
    """队列参数快照：每次（重）建时重新读取模块全局（测试 monkeypatch 后立即生效）。"""
    return WriteBehindParams(
        worker_count=_REQUEST_WORKER_COUNT,
        overflow_path=os.path.join(config.DATA_DIR, _OVERFLOW_LOG_FILENAME),
        overflow_serialize=_serialize_overflow,
        maxsize=_REQUEST_QUEUE_MAX_SIZE,
        write_timeout=_REQUEST_WRITE_TIMEOUT,
        name="request_logs",
    )


# 请求日志队列接线句柄：loop 检查/重建、启停、排空、重置钩子的实现全在共享模块（ADR-0013/D0），
# 差异（写回调/溢出路径与序列化/worker 数/maxsize/写超时）经 _queue_params 注入。
_wiring = WriteBehindWiring(write=_request_log_write, params=_queue_params)


def start_request_log_workers(worker_count: int | None = None) -> None:
    """启动请求日志写入后台 worker（per-call worker_count 覆盖，缺省用 REQUEST_LOG_WORKER_COUNT）。"""
    _wiring.start(worker_count)


async def stop_request_log_workers() -> None:
    """停止请求日志写入后台 worker 并消费队列残留记录。"""
    await _wiring.stop()


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
    requested_model: str | None = None,
    sensitivity_info: dict[str, Any] | None = None,
    conversion_info: dict[str, Any] | None = None,
    shaping_info: dict[str, Any] | None = None,
    api_type: str | None = None,
    request_source: str = "client",
    request_ref: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    if _backend is None:
        logger.warning(f"Request log backend unavailable ({_backend_error}); discarding record for model={model}")
        return
    queue = _wiring.ensure_queue()
    if queue is None:
        return
    flags = _get_save_flags()
    record = {
        "timestamp": timestamp or _utc_now(),
        "request_ref": request_ref,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "model": model,
        "requested_model": requested_model,
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
        "sensitivity_info": sensitivity_info,
        "conversion_info": conversion_info,
        "shaping_info": shaping_info,
        "api_type": api_type,
        "request_source": request_source,
        "raw_capture_info": {field: bool(flags.get(flag)) for field, flag in _RAW_CAPTURE_FLAGS.items()},
    }
    queue.enqueue(record)


async def drain_queue() -> None:
    """消费当前队列中已入队的请求日志记录，主要供测试和优雅停机使用。"""
    await _wiring.drain()


async def wait_for_queue() -> None:
    """等待队列排空：与 drain_queue 排空等价；无 worker / 无句柄时安全返回。"""
    await _wiring.wait()


def _unavailable_result(page: int, page_size: int) -> dict[str, Any]:
    page, page_size = normalize_pagination(page, page_size)
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
    request_source: str | tuple[str, ...] | None = None,
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
            request_source=request_source,
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


async def get_raw_info(request_ref: str, timestamp: str) -> dict[str, Any]:
    """返回一个 Request Reference 的 RAW 可用性；读取只限事件所属月库。"""
    backend = _backend
    if backend is None:
        return {"status": "backend_unavailable", "fields": {}}
    try:
        return await backend.get_raw_info(request_ref, timestamp)
    except Exception as exc:
        logger.warning(f"Request raw info read failed: {exc}")
        return {"status": "backend_unavailable", "fields": {}}


async def get_request_field_by_reference(request_ref: str, timestamp: str, field: str) -> dict | None:
    if not _raw_field_allowed(field):
        return None
    backend = _backend
    if backend is None:
        return None
    try:
        return await backend.get_request_field_by_reference(request_ref, timestamp, field)
    except Exception as exc:
        logger.warning(f"Request raw field read failed: {exc}")
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
