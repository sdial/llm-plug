# PostgreSQL 统计模块重构设计文档

## 1. 设计目标

将现有基于 SQLite 的统计模块迁移到 PostgreSQL，支持更细粒度的请求追踪，满足企业内部员工使用情况考核需求。

## 2. 背景与动机

当前 `stats.py` 使用 SQLite 存储，存在以下局限：
- 缺乏按小时/周/月的多粒度聚合能力
- 不支持延迟分布、首字延迟等精细化指标
- 渠道/模型/API Key 的维度拆分不足
- SQLite 并发能力有限，不适合未来扩展

## 3. 数据模型

### 3.1 requests（明细记录表）

存储每一次请求的完整信息。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | SERIAL | PRIMARY KEY | 自增主键 |
| timestamp | TIMESTAMPTZ | NOT NULL | 请求时间（带时区） |
| model | TEXT | NOT NULL | 模型名称 |
| channel_id | TEXT | NOT NULL | 渠道 ID |
| channel_name | TEXT | NOT NULL | 渠道名称（Provider） |
| api_key_id | TEXT | | API Key ID |
| headers | JSONB | DEFAULT '{}' | 白名单请求头信息（可配置，过滤敏感字段） |
| is_stream | BOOLEAN | NOT NULL | 是否流式请求 |
| input_tokens | INT | DEFAULT 0 | 输入 tokens |
| output_tokens | INT | DEFAULT 0 | 输出 tokens |
| cost | NUMERIC(10,6) | | 费用（备用，暂为 NULL） |
| latency_ms | INT | NOT NULL | 整体延迟（毫秒） |
| lag_ms | INT | | 首字延迟 TTFT（仅 stream 模式有值，非 stream 为 NULL） |
| finish_reason | TEXT | | 完成原因（stop / length / error 等） |
| success | BOOLEAN | NOT NULL | 是否成功 |
| error_msg | TEXT | | 错误信息 |

**索引策略：**

```sql
CREATE INDEX idx_requests_timestamp ON requests(timestamp);
CREATE INDEX idx_requests_channel ON requests(channel_id);
CREATE INDEX idx_requests_model ON requests(model);
CREATE INDEX idx_requests_api_key ON requests(api_key_id);
CREATE INDEX idx_requests_hour ON requests(date_trunc('hour', timestamp));
CREATE INDEX idx_headers_app ON requests((headers->>'X-App-Name'));
```

### 3.2 聚合统计表

采用**物化聚合表 + 手动触发更新**策略。聚合表存储预计算结果，查询性能高；通过前端按钮或后台任务触发聚合计算。

#### hourly_stats（小时聚合表）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| hour | TIMESTAMPTZ | NOT NULL | 小时起始时间 |
| channel_id | TEXT | NOT NULL | 渠道 ID |
| model | TEXT | NOT NULL | 模型 |
| api_key_id | TEXT | NOT NULL | API Key ID |
| request_count | INT | DEFAULT 0 | 请求数 |
| success_count | INT | DEFAULT 0 | 成功数 |
| fail_count | INT | DEFAULT 0 | 失败数 |
| input_tokens | BIGINT | DEFAULT 0 | 输入 tokens |
| output_tokens | BIGINT | DEFAULT 0 | 输出 tokens |
| avg_latency_ms | INT | | 平均延迟 |
| avg_lag_ms | INT | | 平均首字延迟 |
| updated_at | TIMESTAMPTZ | DEFAULT now() | 更新时间 |

**主键**: `(hour, channel_id, model, api_key_id)`

#### daily_stats（日聚合表）

字段与 `hourly_stats` 相同，仅 `hour` 改为 `date` (DATE 类型)。

**主键**: `(date, channel_id, model, api_key_id)`

#### 聚合表索引

```sql
CREATE INDEX idx_hourly_stats_time ON hourly_stats(hour);
CREATE INDEX idx_daily_stats_time ON daily_stats(date);
CREATE INDEX idx_hourly_stats_channel ON hourly_stats(channel_id);
CREATE INDEX idx_hourly_stats_model ON hourly_stats(model);
CREATE INDEX idx_hourly_stats_api_key ON hourly_stats(api_key_id);
```

#### 聚合更新逻辑

```sql
-- 从 requests 明细表聚合到 hourly_stats
INSERT INTO hourly_stats (hour, channel_id, model, api_key_id, request_count, success_count, fail_count, input_tokens, output_tokens, avg_latency_ms, avg_lag_ms)
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
    AVG(lag_ms)::int as avg_lag_ms
FROM requests
WHERE timestamp >= :start_time AND timestamp < :end_time
GROUP BY hour, channel_id, model, api_key_id
ON CONFLICT (hour, channel_id, model, api_key_id) DO UPDATE SET
    request_count = EXCLUDED.request_count,
    success_count = EXCLUDED.success_count,
    fail_count = EXCLUDED.fail_count,
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    avg_latency_ms = EXCLUDED.avg_latency_ms,
    avg_lag_ms = EXCLUDED.avg_lag_ms,
    updated_at = now();
```

## 4. API 设计

### 4.1 record_request

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
    ...
```

### 4.2 查询接口（读聚合表）

```python
async def get_hourly_stats(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    """查询小时聚合统计"""

async def get_daily_stats(
    days: int = 7,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
) -> list[dict[str, Any]]:
    """查询日聚合统计"""

async def get_overall_stats(
    days: int = 7,
) -> dict[str, Any]:
    """总体统计数据（基于聚合表汇总）"""

async def get_requests(
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    channel_id: str | None = None,
    model: str | None = None,
    api_key_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """分页查询请求明细（直接读 requests 表）"""

async def cleanup_old_data(
    keep_days: int,
) -> int:
    """清理 N 天前的数据（同时清理 requests 和聚合表）"""
```

### 4.3 手动触发聚合接口

```python
async def aggregate_hourly_stats(
    start_time: datetime,
    end_time: datetime,
) -> dict[str, Any]:
    """
    手动触发指定时间范围的小时聚合。
    前端按钮调用此接口，将 requests 明细聚合到 hourly_stats 表。
    返回更新的记录数。
    """

async def aggregate_daily_stats(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    手动触发指定日期范围的日聚合。
    将 hourly_stats 汇总到 daily_stats（或直接从 requests 聚合）。
    """
```

**Admin API 端点：**

```python
@router.post("/stats/aggregate/hourly")
def trigger_hourly_aggregation(
    start_time: datetime,
    end_time: datetime,
):
    """前端按钮：触发小时聚合"""

@router.post("/stats/aggregate/daily")
def trigger_daily_aggregation(
    start_date: date,
    end_date: date,
):
    """前端按钮：触发日聚合"""
```

## 5. 架构改动

### 5.1 新增文件

| 文件 | 说明 |
|------|------|
| `stats_pg.py` | PostgreSQL 统计模块（替换 `stats.py`） |
| `tests/test_stats_pg.py` | PostgreSQL 统计模块测试 |

### 5.2 修改文件

| 文件 | 修改内容 |
|------|----------|
| `proxy_core.py` | 提取 `finish_reason` 和 `lag_ms`，传递给 `record_request` |
| `routers/admin.py` | 适配新的异步统计接口 |
| `main.py` | 传递请求头信息到统计模块 |
| `pyproject.toml` | 新增 `asyncpg` 和 `psycopg2-binary` 依赖 |

### 5.3 依赖变更

```toml
[project.dependencies]
asyncpg = "^0.29"
psycopg2-binary = "^2.9"
```

## 6. 关键字段获取逻辑

### 6.1 finish_reason

从上游响应体中提取：

- **OpenAI 格式**: `choices[0].finish_reason`
- **Anthropic 格式**: `stop_reason`
- **流式结束标记**: 最后一个 SSE chunk 中的 `finish_reason`

### 6.2 lag_ms（首字延迟）

- **非流式请求**: `lag_ms = None`（或等于 `latency_ms`）
- **流式请求**: 从请求开始到第一个 SSE chunk 返回的时间
- 在 `proxy_core.py` 的 `_do_stream_request` 中计时

### 6.3 headers（JSONB）

收集以下关键请求头（可配置）：

```python
TRACKED_HEADERS = [
    "X-App-Name",
    "X-Request-ID",
    "User-Agent",
    # 其他业务相关 header
]
```

## 7. 配置变更

新增环境变量（或配置文件）：

```env
# PostgreSQL 连接
DATABASE_URL=postgresql://user:pass@localhost:5432/llmplug

# 统计配置
STATS_TRACKED_HEADERS=X-App-Name,X-Request-ID,User-Agent
STATS_CLEANUP_DAYS=90
```

## 8. 测试策略

- **单元测试**: 使用 `pytest-postgresql` 或 Docker 容器启动测试用 PG 实例
- **集成测试**: 验证完整的请求 → 记录 → 查询流程
- **性能测试**: 验证百万级数据量下的聚合查询性能

## 9. 迁移策略

1. **测试阶段**: 直接切换，历史 SQLite 数据可丢弃
2. **生产阶段**（未来）:
   - 并行运行双写（SQLite + PG）一段时间
   - 验证 PG 数据一致性
   - 停止 SQLite 写入，保留只读查询
   - 最终移除 SQLite 代码

## 10. 风险评估

| 风险 | 缓解措施 |
|------|----------|
| PG 连接失败导致请求失败 | 统计写入使用 `asyncio.shield` + 异常捕获，不阻塞主流程 |
| headers JSONB 字段过大 | 只采集白名单 header，过滤掉 Authorization 等敏感字段 |
| 聚合任务执行时间长 | 限制单次聚合时间范围（如最多7天），支持分批触发 |

## 11. 后续扩展

- **计费系统**: `cost` 字段接入定价模型
- **自动定时聚合**: 通过 APScheduler / Celery 定时执行聚合任务，减少手动操作
- **分区表**: 数据量超过千万级时引入按时间分区
