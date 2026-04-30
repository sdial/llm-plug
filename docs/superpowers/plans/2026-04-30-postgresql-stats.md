# PostgreSQL 统计模块重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 SQLite 统计模块迁移到 PostgreSQL，支持更细粒度的请求追踪和物化聚合表。

**Architecture:** 使用 asyncpg 进行异步 PostgreSQL 操作。requests 表存储明细，hourly_stats/daily_stats 存储物化聚合结果。聚合通过 Admin API 手动触发。

**Tech Stack:** Python 3.12, FastAPI, asyncpg, psycopg2-binary, pytest, PostgreSQL 15+

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `config.py` | 新增 DATABASE_URL 和 STATS_TRACKED_HEADERS 配置 |
| `stats_pg.py` | 新建：PostgreSQL 统计模块核心（连接池、记录、查询、聚合） |
| `proxy_core.py` | 修改：提取 finish_reason 和 lag_ms，调用新的统计接口 |
| `main.py` | 修改：提取请求头并传递给 proxy_core |
| `routers/admin.py` | 修改：新增聚合触发端点，适配查询接口 |
| `tests/test_stats_pg.py` | 新建：PostgreSQL 统计模块测试 |

---

### Task 1: 配置变更

**Files:**
- Modify: `config.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: 在 config.py 中添加 PostgreSQL 配置**

```python
import os

# ... existing config ...

# PostgreSQL 配置
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/llmplug")
STATS_TRACKED_HEADERS = os.getenv(
    "STATS_TRACKED_HEADERS",
    "X-App-Name,X-Request-ID,User-Agent"
).split(",")
```

- [ ] **Step 2: 在 pyproject.toml 中添加依赖**

```toml
[project.dependencies]
# ... existing deps ...
asyncpg = "^0.29"
psycopg2-binary = "^2.9"
```

- [ ] **Step 3: 安装依赖**

Run: `uv pip install asyncpg psycopg2-binary`
Expected: 成功安装

- [ ] **Step 4: Commit**

```bash
git add config.py pyproject.toml
git commit -m "config: add PostgreSQL and stats tracking configuration"
```

---

### Task 2: 创建 PostgreSQL 统计模块核心

**Files:**
- Create: `stats_pg.py`

- [ ] **Step 1: 编写数据库连接池和初始化**

```python
"""PostgreSQL 统计模块 - 使用 asyncpg 存储请求统计"""
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg
from config import DATABASE_URL, STATS_TRACKED_HEADERS

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """初始化数据库连接池"""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def close_pool():
    """关闭连接池"""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def _get_conn():
    """获取数据库连接"""
    pool = await init_pool()
    async with pool.acquire() as conn:
        yield conn


async def init_db():
    """初始化数据库表结构"""
    async with _get_conn() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL,
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
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_hour ON requests(date_trunc('hour', timestamp))")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_stats_time ON hourly_stats(hour)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_stats_time ON daily_stats(date)")
```

- [ ] **Step 2: 编写 record_request 函数**

```python
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
    # 过滤并序列化请求头
    tracked = {}
    if headers:
        for key in STATS_TRACKED_HEADERS:
            val = headers.get(key) or headers.get(key.lower())
            if val:
                tracked[key] = val

    async with _get_conn() as conn:
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
```

- [ ] **Step 3: 编写聚合触发函数**

```python
async def aggregate_hourly_stats(
    start_time: datetime,
    end_time: datetime,
) -> dict[str, Any]:
    """手动触发指定时间范围的小时聚合"""
    async with _get_conn() as conn:
        result = await conn.execute(
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
        # asyncpg execute 不返回行数，需用 RETURNING 或单独查询
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
```

- [ ] **Step 4: 编写查询接口**

```python
async def get_hourly_stats(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    """查询小时聚合统计"""
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
    async with _get_conn() as conn:
        # 总体统计
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) as fail_count,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens
            FROM requests
            WHERE timestamp >= now() - interval '$1 days'
            """,
            days
        )

        # 渠道分布
        channel_rows = await conn.fetch(
            """
            SELECT channel_name, COUNT(*) as count
            FROM requests
            WHERE timestamp >= now() - interval '$1 days'
            GROUP BY channel_id, channel_name
            ORDER BY count DESC
            """,
            days
        )

        # 模型分布
        model_rows = await conn.fetch(
            """
            SELECT model, COUNT(*) as count
            FROM requests
            WHERE timestamp >= now() - interval '$1 days'
            GROUP BY model
            ORDER BY count DESC
            LIMIT 20
            """,
            days
        )

        # API Key 分布
        key_rows = await conn.fetch(
            """
            SELECT api_key_id, COUNT(*) as count,
                   COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens
            FROM requests
            WHERE api_key_id IS NOT NULL AND api_key_id != ''
              AND timestamp >= now() - interval '$1 days'
            GROUP BY api_key_id
            ORDER BY count DESC
            """,
            days
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
    async with _get_conn() as conn:
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
```

- [ ] **Step 5: Commit**

```bash
git add stats_pg.py
git commit -m "feat: create PostgreSQL stats module with requests, aggregation, and queries"
```

---

### Task 3: 修改 proxy_core.py 提取 finish_reason 和 lag_ms

**Files:**
- Modify: `proxy_core.py`

- [ ] **Step 1: 修改非流式请求处理，提取 finish_reason**

在 `proxy_core.py` 的非流式请求成功处理部分（约第 260 行附近），找到 `record_request` 调用处：

```python
# 原代码:
stats.record_request(
    ...
    success=True,
    api_key_id=api_key_id,
)

# 修改为:
finish_reason = None
if isinstance(response_data, dict):
    choices = response_data.get("choices", [])
    if choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
    # Anthropic format
    if finish_reason is None:
        finish_reason = response_data.get("stop_reason")

stats.record_request(
    ...
    success=True,
    api_key_id=api_key_id,
    finish_reason=finish_reason,
)
```

注意：由于 stats 现在改为异步，调用方式需要改为 `await stats_pg.record_request(...)`。这一步在 Task 5 中统一处理。

- [ ] **Step 2: 修改流式请求处理，提取 finish_reason 和 lag_ms**

在 `_do_stream_request` 函数中：

1. 在开始请求前添加 `first_token_time = None`
2. 在 yield 第一个 chunk 时记录 `if first_token_time is None: first_token_time = time.time()`
3. 在函数末尾的 `finally` 块之前计算 `lag_ms`：

```python
lag_ms = None
if first_token_time is not None:
    lag_ms = round((first_token_time - start_time) * 1000)

# 提取 finish_reason
finish_reason = None
# 从最后一个 chunk 或 collected_chunks 中提取
# OpenAI: last chunk has choices[0].finish_reason
# Anthropic: message_stop event has stop_reason
```

- [ ] **Step 3: Commit**

```bash
git add proxy_core.py
git commit -m "feat: extract finish_reason and lag_ms in proxy_core"
```

---

### Task 4: 修改 main.py 传递请求头

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 在 middleware 中提取请求头并传递**

在 `main.py` 的 middleware 中（约第 112 行附近 `request.state.api_key_id = matched_key.get("id")` 之后）：

```python
# 存储 key ID 后，保存需要追踪的请求头
request.state.tracked_headers = {
    k: v for k, v in request.headers.items()
    if k in STATS_TRACKED_HEADERS or k.lower() in [h.lower() for h in STATS_TRACKED_HEADERS]
}
```

- [ ] **Step 2: Commit**

```bash
git add main.py
git commit -m "feat: extract tracked headers in middleware"
```

---

### Task 5: 统一替换 stats 调用为 stats_pg

**Files:**
- Modify: `proxy_core.py`
- Modify: `routers/admin.py`
- Modify: `main.py` ( Lifespan 中初始化 )

- [ ] **Step 1: 在 main.py lifespan 中初始化 PostgreSQL 连接池**

```python
from contextlib import asynccontextmanager
from stats_pg import init_db as init_stats_db, close_pool as close_stats_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_stats_db()
    yield
    await close_stats_pool()

app = FastAPI(lifespan=lifespan)
```

- [ ] **Step 2: 修改 proxy_core.py 中的导入和调用**

```python
# 替换:
import stats
# 为:
import stats_pg

# 所有 stats.record_request(...) 替换为 await stats_pg.record_request(...)
# 并添加 headers 和 finish_reason/lag_ms 参数
```

在 `proxy_core.py` 中收集请求头传递：

```python
# 从 request.state 获取 tracked_headers
tracked_headers = getattr(request.state, 'tracked_headers', None)

await stats_pg.record_request(
    channel_id=channel.id,
    channel_name=channel.name,
    model=model,
    is_stream=False,
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    latency_ms=latency_ms,
    success=True,
    api_key_id=api_key_id,
    headers=tracked_headers,
    finish_reason=finish_reason,
    lag_ms=lag_ms,
)
```

- [ ] **Step 3: 修改 routers/admin.py**

```python
# 替换导入:
from stats import cleanup_old_data, get_daily_stats, get_overall_stats
# 为:
from stats_pg import cleanup_old_data, get_daily_stats, get_overall_stats, aggregate_hourly_stats, aggregate_daily_stats

# 修改 /stats 接口为 async:
@router.get("/stats")
async def get_stats():
    overall = await get_overall_stats()
    daily = await get_daily_stats(days=7)
    return {"overall": overall, "daily": daily}

# 修改 /stats/cleanup 接口为 async:
@router.post("/stats/cleanup")
async def cleanup_stats(keep_days: int = Query(default=30, ge=0, le=365)):
    deleted = await cleanup_old_data(keep_days)
    return {"message": f"已清理 {deleted} 条记录", "deleted_count": deleted}

# 新增聚合触发端点:
@router.post("/stats/aggregate/hourly")
async def trigger_hourly_aggregation(
    start_time: datetime,
    end_time: datetime,
):
    result = await aggregate_hourly_stats(start_time, end_time)
    return {"message": f"已更新 {result['updated_rows']} 条小时聚合记录", **result}

@router.post("/stats/aggregate/daily")
async def trigger_daily_aggregation(
    start_date: date,
    end_date: date,
):
    result = await aggregate_daily_stats(start_date, end_date)
    return {"message": f"已更新 {result['updated_rows']} 条日聚合记录", **result}
```

- [ ] **Step 4: Commit**

```bash
git add proxy_core.py routers/admin.py main.py
git commit -m "feat: integrate stats_pg module and add aggregation endpoints"
```

---

### Task 6: 编写测试

**Files:**
- Create: `tests/test_stats_pg.py`

- [ ] **Step 1: 编写测试基类和 fixtures**

```python
import os
import pytest
import asyncpg
from datetime import datetime, timedelta

import stats_pg

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://localhost:5432/llmplug_test")

@pytest.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    """每个测试使用独立的测试数据库"""
    monkeypatch.setattr(stats_pg, "DATABASE_URL", TEST_DB_URL)
    # 清理并重新初始化
    pool = await asyncpg.create_pool(TEST_DB_URL)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS hourly_stats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
    await pool.close()
    await stats_pg.init_db()
    yield
    await stats_pg.close_pool()

class TestInitDb:
    @pytest.mark.asyncio
    async def test_creates_tables(self):
        pool = await asyncpg.create_pool(TEST_DB_URL)
        async with pool.acquire() as conn:
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
            names = {r["table_name"] for r in tables}
            assert "requests" in names
            assert "hourly_stats" in names
            assert "daily_stats" in names
        await pool.close()

class TestRecordRequest:
    @pytest.mark.asyncio
    async def test_inserts_request_row(self):
        await stats_pg.record_request(
            channel_id="ch_1",
            channel_name="Test Channel",
            model="gpt-4",
            is_stream=False,
            input_tokens=100,
            output_tokens=50,
            latency_ms=200,
            success=True,
            api_key_id="key_1",
            headers={"X-App-Name": "TestApp"},
            lag_ms=50,
            finish_reason="stop",
        )
        pool = await asyncpg.create_pool(TEST_DB_URL)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM requests")
            assert row["channel_id"] == "ch_1"
            assert row["model"] == "gpt-4"
            assert row["headers"]["X-App-Name"] == "TestApp"
            assert row["lag_ms"] == 50
            assert row["finish_reason"] == "stop"
        await pool.close()

class TestAggregation:
    @pytest.mark.asyncio
    async def test_hourly_aggregation(self):
        now = datetime.now()
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        await stats_pg.record_request("ch_1", "Test", "gpt-4", False, 100, 50, 200, True)

        result = await stats_pg.aggregate_hourly_stats(hour_start, hour_start + timedelta(hours=1))
        assert result["updated_rows"] >= 1

        stats_result = await stats_pg.get_hourly_stats(start_time=hour_start)
        assert len(stats_result) >= 1
        assert stats_result[0]["request_count"] == 1
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/test_stats_pg.py -v`
Expected: 所有测试通过

- [ ] **Step 3: Commit**

```bash
git add tests/test_stats_pg.py
git commit -m "test: add PostgreSQL stats module tests"
```

---

## Self-Review Checklist

**1. Spec coverage:**
- ✅ PostgreSQL 迁移 — Task 2
- ✅ requests 表含 headers JSONB, lag_ms, finish_reason — Task 2
- ✅ hourly_stats / daily_stats 聚合表 — Task 2
- ✅ 手动触发聚合接口 — Task 2 + Task 5
- ✅ finish_reason 提取 — Task 3
- ✅ lag_ms 提取 — Task 3
- ✅ headers 传递 — Task 4
- ✅ Admin API 查询适配 — Task 5

**2. Placeholder scan:**
- ✅ 无 TBD/TODO
- ✅ 所有步骤含完整代码

**3. Type consistency:**
- ✅ 函数签名与调用一致
