"""请求记录查询语义共享模块（ADR-0017 D0）。

stats（聚合明细表 request_stats_raw）与 request_logs（月度明细表 request_logs）
服务同一个管理端接口的 source 切换，二者共用同一套过滤 / 汇总 / 分页 /
行归一 / 时间戳归一语义——本模块是这些语义的单一住所：改过滤条件或聚合
口径只改这里，两个来源自动一致。

纯查询语义：SQL 只在这里拼，连接管理、入队与写路径都留给调用方
（stats._open_conn / request_logs 后端各自持有连接）。
"""

from datetime import UTC, datetime
from typing import Any

from db_write_behind import _escape_like

__all__ = [
    "append_request_source_condition",
    "build_summary_sql",
    "build_where_clause",
    "normalize_bool_fields",
    "normalize_pagination",
    "normalize_query_time",
    "record_timestamp_to_iso",
    "to_utc_naive_datetime",
]


def to_utc_naive_datetime(value: datetime) -> datetime:
    """naive 输入按 UTC 解释；aware 转 UTC。返回 naive UTC datetime。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None)


def record_timestamp_to_iso(value: Any) -> str:
    """记录时间戳转 ISO：datetime → 'YYYY-MM-DD HH:MM:SS.ffffff'；其余原样字符串化；空值回退当前时间。

    两侧同名不同义的 `_to_iso`（一侧只收 datetime、一侧收任意值还回退当前时间）
    由本 helper 收敛为一份语义（ADR-0017 D0）。
    """
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="microseconds")
    if value:
        return str(value)
    return datetime.now(UTC).isoformat(sep=" ", timespec="microseconds")


def normalize_query_time(value: datetime) -> str:
    """查询时间参数归一：aware/naive → naive UTC ISO，与库内 timestamp 字符串可比。"""
    return record_timestamp_to_iso(to_utc_naive_datetime(value))


def normalize_pagination(page: int = 1, page_size: int = 10) -> tuple[int, int]:
    """分页归一：page 下限 1，page_size 夹在 [1, 100]。"""
    return max(1, page), max(1, min(page_size, 100))


def normalize_bool_fields(row: dict[str, Any]) -> dict[str, Any]:
    """行归一：success / is_stream 的 SQLite 整数 0/1 → bool（None 保留）。原地修改并返回。"""
    if row.get("success") is not None:
        row["success"] = bool(row["success"])
    if row.get("is_stream") is not None:
        row["is_stream"] = bool(row["is_stream"])
    return row


def append_request_source_condition(conditions: list[str], args: list[Any], request_source: str | tuple[str, ...] | None) -> None:
    """追加 request_source 过滤条件；None／空集合＝不过滤（保留全量）。"""
    if not request_source:
        return
    sources = (request_source,) if isinstance(request_source, str) else tuple(request_source)
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        conditions.append(f"request_source IN ({placeholders})")
        args.extend(sources)


def build_where_clause(
    model: str | None = None,
    channel: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    success: bool | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    is_stream: bool | None = None,
    request_source: str | tuple[str, ...] | None = None,
) -> tuple[str, list[Any]]:
    """九条件 WHERE 构造器——stats 与 request_logs 列表查询共用的唯一一份方言。

    条件集：model / channel / start / end / success / api_key_id / client_ip /
    is_stream / request_source；client_ip 模糊搜统一 LOWER()（大小写不敏感，
    不依赖 SQLite LIKE 的默认 ASCII 大小写行为）。返回 (where_clause, args)。
    """
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
        args.append(normalize_query_time(start))
    if end:
        conditions.append("timestamp < ?")
        args.append(normalize_query_time(end))
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
    append_request_source_condition(conditions, args, request_source)
    return " AND ".join(conditions), args


def build_summary_sql(table: str, where_clause: str) -> str:
    """汇总聚合 SQL——success_count / latency / lag 口径共享，以表名为参数。

    stats 查聚合明细表 request_stats_raw，request_logs 查月度明细表 request_logs。
    """
    return f"""
        SELECT
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
            COALESCE(SUM(CASE WHEN success = 1 THEN latency_ms ELSE 0 END), 0) AS success_latency_sum,
            SUM(CASE WHEN success = 1 AND latency_ms IS NOT NULL THEN 1 ELSE 0 END) AS success_latency_count,
            COALESCE(SUM(CASE WHEN success = 1 THEN lag_ms ELSE 0 END), 0) AS success_lag_sum,
            SUM(CASE WHEN success = 1 AND lag_ms IS NOT NULL THEN 1 ELSE 0 END) AS success_lag_count
        FROM {table}
        WHERE {where_clause}
        """
