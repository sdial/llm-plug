"""PostgreSQL 统计模块 - 使用 asyncpg 存储请求统计"""
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg
from config import DATABASE_URL, STATS_TRACKED_HEADERS, TRACK_ALL_HEADERS

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_db_available: bool = False


async def init_pool() -> asyncpg.Pool | None:
    """初始化数据库连接池"""
    global _pool, _db_available
    if _pool is None and not _db_available:
        if not DATABASE_URL:
            logger.warning("DATABASE_URL 未配置，PostgreSQL 统计功能已禁用")
            return None
        try:
            _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
            _db_available = True
        except Exception as exc:
            logger.warning("PostgreSQL 连接失败，统计功能已禁用: %s", exc)
            _db_available = False
    return _pool


async def close_pool():
    """关闭连接池"""
    global _pool, _db_available
    if _pool:
        await _pool.close()
        _pool = None
    _db_available = False


@asynccontextmanager
async def _get_conn():
    """获取数据库连接"""
    pool = await init_pool()
    if pool is None:
        yield None
        return
    async with pool.acquire() as conn:
        yield conn


async def init_db():
    """初始化数据库表结构"""
    async with _get_conn() as conn:
        if conn is None:
            return
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
                model TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                api_key_id TEXT,
                headers JSONB DEFAULT '{}',
                is_stream BOOLEAN NOT NULL,
                input_tokens INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                cost NUMERIC(10,6),
                latency_ms INT NOT NULL,
                lag_ms INT,
                finish_reason TEXT,
                success BOOLEAN NOT NULL,
                error_msg TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS hourly_stats (
                hour TIMESTAMPTZ NOT NULL,
                channel_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_id TEXT NOT NULL,
                request_count INT DEFAULT 0,
                success_count INT DEFAULT 0,
                fail_count INT DEFAULT 0,
                input_tokens BIGINT DEFAULT 0,
                output_tokens BIGINT DEFAULT 0,
                avg_latency_ms INT,
                avg_lag_ms INT,
                updated_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (hour, channel_id, model, api_key_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date DATE NOT NULL,
                channel_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_id TEXT NOT NULL,
                request_count INT DEFAULT 0,
                success_count INT DEFAULT 0,
                fail_count INT DEFAULT 0,
                input_tokens BIGINT DEFAULT 0,
                output_tokens BIGINT DEFAULT 0,
                avg_latency_ms INT,
                avg_lag_ms INT,
                updated_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (date, channel_id, model, api_key_id)
            )
        """)
        # 创建索引
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_channel ON requests(channel_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_api_key ON requests(api_key_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_stats_time ON hourly_stats(hour)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_time ON daily_stats(date)")


async def record_request(
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
    headers: dict[str, str] | None = None,
    lag_ms: int | None = None,
    finish_reason: str | None = None,
) -> None:
    """记录一次请求到明细表"""
    if not _db_available:
        return

    # 过滤并序列化请求头（大小写不敏感匹配）
    tracked = {}
    if headers:
        if TRACK_ALL_HEADERS:
            tracked = dict(headers)
        else:
            header_lower = {k.lower(): v for k, v in headers.items()}
            for key in STATS_TRACKED_HEADERS:
                val = header_lower.get(key.lower())
                if val:
                    tracked[key] = val

    async with _get_conn() as conn:
        if conn is None:
            return
        await conn.execute(
            """
            INSERT INTO requests
            (timestamp, model, channel_id, channel_name, api_key_id, headers, is_stream,
             input_tokens, output_tokens, latency_ms, lag_ms, finish_reason, success, error_msg)
            VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            """,
            model, channel_id, channel_name, api_key_id,
            json.dumps(tracked), is_stream, input_tokens, output_tokens,
            latency_ms, lag_ms, finish_reason, success, error_msg
        )


async def aggregate_hourly_stats(
    start_time: datetime,
    end_time: datetime,
) -> dict[str, Any]:
    """手动触发指定时间范围的小时聚合"""
    if not _db_available:
        return {"updated_rows": 0}
    async with _get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO hourly_stats
            (hour, channel_id, model, api_key_id, request_count, success_count, fail_count,
             input_tokens, output_tokens, avg_latency_ms, avg_lag_ms, updated_at)
            SELECT
                date_trunc('hour', timestamp) as hour,
                channel_id,
                model,
                COALESCE(api_key_id, '') as api_key_id,
                COUNT(*) as request_count,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail_count,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                AVG(latency_ms)::int as avg_latency_ms,
                AVG(lag_ms)::int as avg_lag_ms,
                now()
            FROM requests
            WHERE timestamp >= $1 AND timestamp < $2
            GROUP BY hour, channel_id, model, api_key_id
            ON CONFLICT (hour, channel_id, model, api_key_id) DO UPDATE SET
                request_count = EXCLUDED.request_count,
                success_count = EXCLUDED.success_count,
                fail_count = EXCLUDED.fail_count,
                input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                avg_latency_ms = EXCLUDED.avg_latency_ms,
                avg_lag_ms = EXCLUDED.avg_lag_ms,
                updated_at = now()
            """,
            start_time, end_time
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM hourly_stats WHERE hour >= $1 AND hour < $2",
            start_time, end_time
        )
        return {"updated_rows": count or 0}


async def aggregate_daily_stats(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """手动触发指定日期范围的日聚合"""
    if not _db_available:
        return {"updated_rows": 0}
    async with _get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO daily_stats
            (date, channel_id, model, api_key_id, request_count, success_count, fail_count,
             input_tokens, output_tokens, avg_latency_ms, avg_lag_ms, updated_at)
            SELECT
                date_trunc('day', timestamp)::date as date,
                channel_id,
                model,
                COALESCE(api_key_id, '') as api_key_id,
                COUNT(*) as request_count,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail_count,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                AVG(latency_ms)::int as avg_latency_ms,
                AVG(lag_ms)::int as avg_lag_ms,
                now()
            FROM requests
            WHERE timestamp >= $1 AND timestamp < $2
            GROUP BY date, channel_id, model, api_key_id
            ON CONFLICT (date, channel_id, model, api_key_id) DO UPDATE SET
                request_count = EXCLUDED.request_count,
                success_count = EXCLUDED.success_count,
                fail_count = EXCLUDED.fail_count,
                input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens,
                avg_latency_ms = EXCLUDED.avg_latency_ms,
                avg_lag_ms = EXCLUDED.avg_lag_ms,
                updated_at = now()
            """,
            datetime.combine(start_date, datetime.min.time()),
            datetime.combine(end_date, datetime.min.time()) + timedelta(days=1)
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM daily_stats WHERE date >= $1 AND date <= $2",
            start_date, end_date
        )
        return {"updated_rows": count or 0}


async def get_hourly_stats(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    """查询小时聚合统计"""
    if not _db_available:
        return []
    conditions = ["1=1"]
    args: list[Any] = []
    if start_time:
        args.append(start_time)
        conditions.append(f"hour >= ${len(args)}")
    if end_time:
        args.append(end_time)
        conditions.append(f"hour < ${len(args)}")
    if channel_id:
        args.append(channel_id)
        conditions.append(f"channel_id = ${len(args)}")
    if model:
        args.append(model)
        conditions.append(f"model = ${len(args)}")
    if api_key_id:
        args.append(api_key_id)
        conditions.append(f"api_key_id = ${len(args)}")

    where_clause = " AND ".join(conditions)
    async with _get_conn() as conn:
        rows = await conn.fetch(
            f"""
            SELECT hour, channel_id, model, api_key_id, request_count, success_count, fail_count,
                   input_tokens, output_tokens, avg_latency_ms, avg_lag_ms
            FROM hourly_stats
            WHERE {where_clause}
            ORDER BY hour DESC
            LIMIT 10000
            """,
            *args
        )
        return [dict(r) for r in rows]


async def get_daily_stats(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    """查询日聚合统计"""
    if not _db_available:
        return []
    start_date = date.today() - timedelta(days=days - 1)
    conditions = ["date >= $1"]
    args: list[Any] = [start_date]
    if channel_id:
        args.append(channel_id)
        conditions.append(f"channel_id = ${len(args)}")
    if model:
        args.append(model)
        conditions.append(f"model = ${len(args)}")
    if api_key_id:
        args.append(api_key_id)
        conditions.append(f"api_key_id = ${len(args)}")

    where_clause = " AND ".join(conditions)
    async with _get_conn() as conn:
        rows = await conn.fetch(
            f"""
            SELECT date, channel_id, model, api_key_id, request_count, success_count, fail_count,
                   input_tokens, output_tokens, avg_latency_ms, avg_lag_ms
            FROM daily_stats
            WHERE {where_clause}
            ORDER BY date ASC
            """,
            *args
        )
        return [dict(r) for r in rows]


async def get_overall_stats(days: int = 7) -> dict[str, Any]:
    """总体统计数据"""
    if not _db_available:
        return {
            "total_requests": 0,
            "success_count": 0,
            "fail_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "channels": [],
            "models": [],
            "api_keys": [],
        }
    async with _get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail_count,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens
            FROM requests
            WHERE timestamp >= now() - ($1 || ' days')::interval
            """,
            str(days)
        )

        channel_rows = await conn.fetch(
            """
            SELECT channel_name, COUNT(*) as count
            FROM requests
            WHERE timestamp >= now() - ($1 || ' days')::interval
            GROUP BY channel_id, channel_name
            ORDER BY count DESC
            """,
            str(days)
        )

        model_rows = await conn.fetch(
            """
            SELECT model, COUNT(*) as count
            FROM requests
            WHERE timestamp >= now() - ($1 || ' days')::interval
            GROUP BY model
            ORDER BY count DESC
            LIMIT 20
            """,
            str(days)
        )

        key_rows = await conn.fetch(
            """
            SELECT api_key_id, COUNT(*) as count,
                   COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens
            FROM requests
            WHERE api_key_id IS NOT NULL AND api_key_id != ''
              AND timestamp >= now() - ($1 || ' days')::interval
            GROUP BY api_key_id
            ORDER BY count DESC
            """,
            str(days)
        )

        return {
            "total_requests": row["total_requests"] or 0,
            "success_count": row["success_count"] or 0,
            "fail_count": row["fail_count"] or 0,
            "total_input_tokens": row["total_input_tokens"] or 0,
            "total_output_tokens": row["total_output_tokens"] or 0,
            "channels": [{"name": r["channel_name"], "count": r["count"]} for r in channel_rows],
            "models": [{"name": r["model"], "count": r["count"]} for r in model_rows],
            "api_keys": [{"key_id": r["api_key_id"], "count": r["count"],
                          "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]} for r in key_rows],
        }


async def cleanup_old_data(keep_days: int) -> int:
    """清理 N 天前的数据"""
    if not _db_available:
        return 0
    async with _get_conn() as conn:
        if conn is None:
            return 0
        if keep_days == 0:
            deleted = await conn.fetchval("DELETE FROM requests RETURNING COUNT(*)")
            await conn.execute("DELETE FROM hourly_stats")
            await conn.execute("DELETE FROM daily_stats")
        else:
            cutoff = datetime.now() - timedelta(days=keep_days)
            deleted = await conn.fetchval(
                "DELETE FROM requests WHERE timestamp < $1 RETURNING COUNT(*)",
                cutoff
            )
            await conn.execute("DELETE FROM hourly_stats WHERE hour < $1", cutoff)
            cutoff_date = (datetime.now() - timedelta(days=keep_days)).date()
            await conn.execute("DELETE FROM daily_stats WHERE date < $1", cutoff_date)
        return deleted or 0


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
    """查询请求记录（支持分页和过滤）"""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))

    if not _db_available:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    conditions = ["1=1"]
    args: list[Any] = []

    if model:
        args.append(f"%{model}%")
        conditions.append(f"model ILIKE ${len(args)}")
    if channel:
        args.append(f"%{channel}%")
        conditions.append(f"channel_name ILIKE ${len(args)}")
    if start:
        args.append(start)
        conditions.append(f"timestamp >= ${len(args)}")
    if end:
        args.append(end)
        conditions.append(f"timestamp < ${len(args)}")
    if success is not None:
        args.append(success)
        conditions.append(f"success = ${len(args)}")
    if api_key_id:
        args.append(api_key_id)
        conditions.append(f"api_key_id = ${len(args)}")
    if is_stream is not None:
        args.append(is_stream)
        conditions.append(f"is_stream = ${len(args)}")

    where_clause = " AND ".join(conditions)

    async with _get_conn() as conn:
        if conn is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}

        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM requests WHERE {where_clause}",
            *args
        )

        offset = (page - 1) * page_size
        data_args = args + [page_size, offset]
        rows = await conn.fetch(
            f"""
            SELECT id, timestamp, model, channel_id, channel_name, api_key_id, headers, is_stream,
                   input_tokens, output_tokens, cost, latency_ms, lag_ms, finish_reason, success, error_msg
            FROM requests
            WHERE {where_clause}
            ORDER BY timestamp DESC
            LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
            """,
            *data_args
        )

        return {
            "items": [dict(r) for r in rows],
            "total": total or 0,
            "page": page,
            "page_size": page_size,
        }
