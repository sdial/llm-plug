"""PostgreSQL 统计模块 - 使用 asyncpg 存储请求统计"""
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any

import asyncio

import asyncpg
from loguru import logger

from config import DATABASE_URL, STATS_TRACKED_HEADERS, TRACK_ALL_HEADERS

_pool: asyncpg.Pool | None = None
_db_available: bool = False
_pool_lock = asyncio.Lock()


def utc8_now() -> datetime:
    """返回当前东8区时间（硬编码 UTC+8）"""
    return datetime.utcnow() + timedelta(hours=8)


def safe_parse_json(body: str | bytes | dict | None) -> dict | None:
    """安全解析 JSON，失败时返回 {"parse_error": true}"""
    if body is None:
        return None
    # 如果已经是 dict，直接返回
    if isinstance(body, dict):
        return body
    try:
        if isinstance(body, bytes):
            return json.loads(body.decode('utf-8'))
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"parse_error": True, "raw_type": type(body).__name__}


async def init_pool() -> asyncpg.Pool | None:
    """初始化数据库连接池"""
    global _pool, _db_available
    async with _pool_lock:
        if _pool is None and not _db_available:
            if not DATABASE_URL:
                logger.warning("DATABASE_URL 未配置，PostgreSQL 统计功能已禁用")
                return None
            try:
                _pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=2,
                max_size=40,
                max_inactive_connection_lifetime=600,
            )
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
        # 为每个连接设置 jsonb codec
        await conn.set_type_codec(
            'jsonb',
            encoder=json.dumps,
            decoder=json.loads,
            schema='pg_catalog'
        )
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
                request_headers JSONB,
                response_headers JSONB,
                request_body JSONB,
                response_body JSONB,
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
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_time ON daily_stats(date)")

        # 表结构迁移：处理旧字段
        # 1. 检查是否存在旧的 headers 列，若存在则重命名为 request_headers
        old_cols = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'requests' AND column_name = 'headers'
        """)
        if old_cols:
            await conn.execute("ALTER TABLE requests RENAME COLUMN headers TO request_headers")

        # 2. 确保新列存在（若表已存在但缺少这些列）
        await conn.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS request_headers JSONB")
        await conn.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS response_headers JSONB")
        await conn.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS request_body JSONB")
        await conn.execute("ALTER TABLE requests ADD COLUMN IF NOT EXISTS response_body JSONB")

        # 3. 创建 GIN 索引（用于 JSONB 查询）
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_request_body ON requests USING GIN (request_body)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_response_body ON requests USING GIN (response_body)")


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
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    request_body: dict | None = None,
    response_body: dict | None = None,
    lag_ms: int | None = None,
    finish_reason: str | None = None,
) -> None:
    """记录一次请求到明细表"""
    if not _db_available:
        return

    # 处理 request_headers：确保所有值都是字符串
    req_headers = {}
    if request_headers:
        if TRACK_ALL_HEADERS:
            for k, v in request_headers.items():
                if isinstance(v, str):
                    req_headers[k] = v
                elif v is None:
                    req_headers[k] = ""
                else:
                    req_headers[k] = str(v)
        else:
            target_keys = {k.lower() for k in STATS_TRACKED_HEADERS}
            for k, v in request_headers.items():
                if k.lower() in target_keys:
                    if isinstance(v, str):
                        req_headers[k] = v
                    elif v is None:
                        req_headers[k] = ""
                    else:
                        req_headers[k] = str(v)

    # 处理 response_headers：确保所有值都是字符串
    resp_headers = {}
    if response_headers:
        for k, v in response_headers.items():
            if isinstance(v, str):
                resp_headers[k] = v
            elif v is None:
                resp_headers[k] = ""
            else:
                resp_headers[k] = str(v)

    async with _get_conn() as conn:
        if conn is None:
            return
        await conn.execute(
            """
            INSERT INTO requests
            (timestamp, model, channel_id, channel_name, api_key_id, request_headers, response_headers,
             request_body, response_body, is_stream, input_tokens, output_tokens,
             latency_ms, lag_ms, finish_reason, success, error_msg)
            VALUES (now(), $1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11, $12, $13, $14, $15, $16)
            """,
            model, channel_id, channel_name, api_key_id,
            req_headers, resp_headers, request_body, response_body,
            is_stream, input_tokens, output_tokens,
            latency_ms, lag_ms, finish_reason, success, error_msg
        )



async def aggregate_daily_stats(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """手动触发指定日期范围的日聚合

    注意：start_date 和 end_date 是东8区日期，需要转换为 UTC 时间查询
    """
    if not _db_available:
        return {"updated_rows": 0}
    # 东8区日期的0点 = 该日期前一天的 16:00 UTC
    # 例如：东8区 2026-05-03 00:00:00 = UTC 2026-05-02 16:00:00
    start_utc = datetime.combine(start_date, datetime.min.time()) - timedelta(hours=8)
    end_utc = datetime.combine(end_date, datetime.min.time()) + timedelta(days=1) - timedelta(hours=8)

    async with _get_conn() as conn:
        await conn.execute(
            """
INSERT INTO daily_stats
        (date, channel_id, model, api_key_id, request_count, success_count, fail_count,
         input_tokens, output_tokens, avg_latency_ms, avg_lag_ms, updated_at)
        SELECT
        date_trunc('day', timestamp + interval '8 hours')::date as date,
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
            start_utc,
            end_utc
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM daily_stats WHERE date >= $1 AND date <= $2",
            start_date, end_date
        )
        return {"updated_rows": count or 0}




async def get_daily_stats(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    """查询日聚合统计"""
    if not _db_available:
        return []
    start_date = utc8_now().date() - timedelta(days=days - 1)
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


async def get_daily_stats_from_requests(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    """从 requests 明细表实时聚合日统计（daily_stats 无数据时的兜底）"""
    if not _db_available:
        return []

    # 计算东8区今天的日期，然后计算查询起始时间（UTC时间）
    today_utc8 = utc8_now().date()
    start_date = today_utc8 - timedelta(days=days - 1)
    # 东8区日期对应的UTC时间范围：start_date 00:00 UTC+8 = start_date-1 16:00 UTC
    start_utc = datetime.combine(start_date, datetime.min.time()) - timedelta(hours=8)

    conditions = ["timestamp >= $1"]
    args: list[Any] = [start_utc]
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
        if conn is None:
            return []
        rows = await conn.fetch(
            f"""
        SELECT
        date_trunc('day', timestamp + interval '8 hours')::date as date,
        COUNT(*) as request_count,
        SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
        SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail_count,
        SUM(input_tokens) as input_tokens,
        SUM(output_tokens) as output_tokens,
        AVG(latency_ms)::int as avg_latency_ms,
        AVG(lag_ms)::int as avg_lag_ms
        FROM requests
        WHERE {where_clause}
        GROUP BY date_trunc('day', timestamp + interval '8 hours')::date
            ORDER BY date ASC
            """,
            *args
        )
        return [dict(r) for r in rows]



async def refresh_missing_daily_stats() -> dict[str, Any]:
    """自动补全 daily_stats 中缺失的历史日期（不含当天）"""
    if not _db_available:
        return {
            "refreshed_dates": [], "count": 0,
            "debug": {"db_available": False, "reason": "_db_available is False"}
        }

    async with _get_conn() as conn:
        if conn is None:
            return {
                "refreshed_dates": [], "count": 0,
                "debug": {"db_available": True, "conn": None}
            }

        today = utc8_now().date()
        now_dt = utc8_now()

        # 获取 requests 表中存在的日期（排除当天）
        request_dates = await conn.fetch(
            """
            SELECT DISTINCT date_trunc('day', timestamp + interval '8 hours')::date as d
            FROM requests
            WHERE date_trunc('day', timestamp + interval '8 hours')::date < $1
            ORDER BY d
            """,
            today,
        )
        all_request_dates = {r["d"] for r in request_dates}

        # 获取 requests 表中的总记录数和时间范围
        req_range = await conn.fetchrow(
            "SELECT MIN(timestamp) as min_ts, MAX(timestamp) as max_ts, COUNT(*) as cnt FROM requests"
        )

        if not all_request_dates:
            return {
                "refreshed_dates": [], "count": 0,
                "debug": {
                    "today": str(today),
                    "now": now_dt.isoformat(),
                    "request_dates": [],
                    "request_range": dict(req_range) if req_range else None,
                }
            }

        min_date = min(all_request_dates)
        max_date = max(all_request_dates)

        # 获取 daily_stats 中已存在的日期
        existing_rows = await conn.fetch(
            "SELECT DISTINCT date FROM daily_stats WHERE date >= $1 AND date <= $2",
            min_date,
            max_date,
        )
        existing_dates = {r["date"] for r in existing_rows}

        missing_dates = sorted(all_request_dates - existing_dates)

        debug_info = {
            "today": str(today),
            "now": now_dt.isoformat(),
            "request_dates": [str(d) for d in sorted(all_request_dates)],
            "existing_dates": [str(d) for d in sorted(existing_dates)],
            "missing_dates": [str(d) for d in missing_dates],
            "request_range": dict(req_range) if req_range else None,
        }

        if not missing_dates:
            return {
                "refreshed_dates": [], "count": 0,
                "debug": debug_info,
            }

        # 按连续区间分组聚合
        refreshed = []
        start = missing_dates[0]
        prev = missing_dates[0]

        for d in missing_dates[1:]:
            if d == prev + timedelta(days=1):
                prev = d
            else:
                await aggregate_daily_stats(start, prev)
                refreshed.append(f"{start}~{prev}" if start != prev else str(start))
                start = d
                prev = d

        # 处理最后一个区间
        await aggregate_daily_stats(start, prev)
        refreshed.append(f"{start}~{prev}" if start != prev else str(start))

        return {
            "refreshed_dates": refreshed,
            "count": len(missing_dates),
            "debug": debug_info,
        }


async def refresh_stats() -> dict[str, Any]:
    """统一刷新统计：补全缺失历史日聚合 + 强制刷新近3天日聚合"""
    if not _db_available:
        return {"backfilled_count": 0, "recent_refreshed_days": 0}

    today = utc8_now().date()
    three_days_ago = today - timedelta(days=2)

    # 步骤1：补全缺失历史聚合（排除近3天，避免和步骤2重复）
    backfilled_count = 0
    async with _get_conn() as conn:
        if conn is None:
            return {"backfilled_count": 0, "recent_refreshed_days": 0}

        request_dates = await conn.fetch(
            """
            SELECT DISTINCT date_trunc('day', timestamp + interval '8 hours')::date as d
            FROM requests
            WHERE date_trunc('day', timestamp + interval '8 hours')::date < $1
            ORDER BY d
            """,
            three_days_ago,
        )
        all_request_dates = {r["d"] for r in request_dates}

        if all_request_dates:
            min_date = min(all_request_dates)
            max_date = max(all_request_dates)
            existing_rows = await conn.fetch(
                "SELECT DISTINCT date FROM daily_stats WHERE date >= $1 AND date <= $2",
                min_date, max_date,
            )
            existing_dates = {r["date"] for r in existing_rows}
            missing_dates = sorted(all_request_dates - existing_dates)

            if missing_dates:
                start = missing_dates[0]
                prev = missing_dates[0]
                for d in missing_dates[1:]:
                    if d == prev + timedelta(days=1):
                        prev = d
                    else:
                        await aggregate_daily_stats(start, prev)
                        start = d
                        prev = d
                await aggregate_daily_stats(start, prev)
                backfilled_count = len(missing_dates)

    # 步骤2：强制刷新近3天日聚合（含当天）
    await aggregate_daily_stats(three_days_ago, today)

    return {
        "backfilled_count": backfilled_count,
        "recent_refreshed_days": 3,
    }


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
            WHERE timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
            """,
            str(days)
        )

        channel_rows = await conn.fetch(
            """
            SELECT channel_name, COUNT(*) as count
            FROM requests
            WHERE timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
            GROUP BY channel_id, channel_name
            ORDER BY count DESC
            """,
            str(days)
        )

        model_rows = await conn.fetch(
            """
            SELECT model, COUNT(*) as count
            FROM requests
            WHERE timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
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
              AND timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
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


async def get_today_stats() -> dict[str, Any]:
    """今天（东8区0点至今）的实时统计，直接查 requests 表"""
    if not _db_available:
        return {
            "overall": {
                "total_requests": 0, "success_count": 0, "fail_count": 0,
                "total_input_tokens": 0, "total_output_tokens": 0,
                "channels": [], "models": [], "api_keys": [],
            },
            "daily": [],
        }
    utc8 = utc8_now()
    start_of_today = (utc8.replace(hour=0, minute=0, second=0, microsecond=0)
                      - timedelta(hours=8))

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
            WHERE timestamp >= $1
            """,
            start_of_today,
        )

        channel_rows = await conn.fetch(
            """
            SELECT channel_name, COUNT(*) as count
            FROM requests
            WHERE timestamp >= $1
            GROUP BY channel_id, channel_name
            ORDER BY count DESC
            """,
            start_of_today,
        )

        model_rows = await conn.fetch(
            """
            SELECT model, COUNT(*) as count
            FROM requests
            WHERE timestamp >= $1
            GROUP BY model
            ORDER BY count DESC
            LIMIT 20
            """,
            start_of_today,
        )

        key_rows = await conn.fetch(
            """
            SELECT api_key_id, COUNT(*) as count,
                   COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens
            FROM requests
            WHERE api_key_id IS NOT NULL AND api_key_id != ''
              AND timestamp >= $1
            GROUP BY api_key_id
            ORDER BY count DESC
            """,
            start_of_today,
        )

        daily_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as request_count,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail_count,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                AVG(latency_ms)::int as avg_latency_ms,
                AVG(lag_ms) FILTER (WHERE lag_ms IS NOT NULL)::int as avg_lag_ms
            FROM requests
            WHERE timestamp >= $1
            """,
            start_of_today,
        )

    total = row["total_requests"] or 0
    daily = []
    if total > 0:
        daily = [{
            "date": str(utc8.date()),
            "total_requests": daily_row["request_count"] or 0,
            "success_count": daily_row["success_count"] or 0,
            "fail_count": daily_row["fail_count"] or 0,
            "total_input_tokens": daily_row["input_tokens"] or 0,
            "total_output_tokens": daily_row["output_tokens"] or 0,
            "avg_latency_ms": daily_row["avg_latency_ms"] or 0,
            "avg_lag_ms": daily_row["avg_lag_ms"] or 0,
        }]

    return {
        "overall": {
            "total_requests": total,
            "success_count": row["success_count"] or 0,
            "fail_count": row["fail_count"] or 0,
            "total_input_tokens": row["total_input_tokens"] or 0,
            "total_output_tokens": row["total_output_tokens"] or 0,
            "channels": [{"name": r["channel_name"], "count": r["count"]} for r in channel_rows],
            "models": [{"name": r["model"], "count": r["count"]} for r in model_rows],
            "api_keys": [{"key_id": r["api_key_id"], "count": r["count"],
                          "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]} for r in key_rows],
        },
        "daily": daily,
    }


async def get_api_key_stats() -> dict[str, dict[str, int]]:
    """按 api_key_id 聚合全量统计数据，返回 {api_key_id: {request_count, input_tokens, output_tokens}}"""
    if not _db_available:
        return {}
    async with _get_conn() as conn:
        if conn is None:
            return {}
        rows = await conn.fetch(
            """
            SELECT api_key_id,
                   COUNT(*) as request_count,
                   COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                   COALESCE(SUM(output_tokens), 0) as total_output_tokens
            FROM requests
            WHERE api_key_id IS NOT NULL AND api_key_id != ''
            GROUP BY api_key_id
            """
        )
        return {
            r["api_key_id"]: {
                "request_count": r["request_count"],
                "total_input_tokens": r["total_input_tokens"],
                "total_output_tokens": r["total_output_tokens"],
            }
            for r in rows
        }


# 允许的字段映射：URL 路径名 → SQL 列名
_REQUEST_FIELD_MAP = {
    "request_headers": "request_headers",
    "request_body": "request_body",
    "response_headers": "response_headers",
    "response_body": "response_body",
}


async def get_request_field(request_id: int, field: str) -> dict | None:
    """查询单个请求的单个 JSONB 字段。field 必须在 _REQUEST_FIELD_MAP 中。"""
    if not _db_available:
        return None
    column = _REQUEST_FIELD_MAP.get(field)
    if column is None:
        return None
    async with _get_conn() as conn:
        if conn is None:
            return None
        row = await conn.fetchrow(
            f"SELECT {column} FROM requests WHERE id = $1",
            request_id,
        )
        if row is None:
            return None
        return {"data": row[column]}


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
            SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                   is_stream, input_tokens, output_tokens, cost, latency_ms, lag_ms,
                   finish_reason, success, error_msg
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
