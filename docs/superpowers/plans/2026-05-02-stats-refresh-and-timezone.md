# 统计页面"刷新统计"按钮改造 & 时区统一 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 统计页面的"刷新统计"按钮改为补全历史 + 强制刷新近 3 天（含当天）+ 刷新近 24 小时时聚合，并统一全链路使用东 8 区时区。

**架构：** 后端在 `stats.py` 新增 `utc8_now()` 工具函数和 `refresh_stats()` 统一刷新函数；所有 SQL 中 `now()` 日期边界改为 `now() + interval '8 hours'`，`date_trunc` 中的 `timestamp` 改为 `timestamp + interval '8 hours'`；`routers/admin.py` 新增 `POST /admin/stats/refresh` 端点；前端按钮改调用新端点，去掉 alert，加行内提示。

**技术栈：** Python / asyncpg / FastAPI / 原生 HTML+JS

**规格文档：** `docs/superpowers/specs/2026-05-02-stats-refresh-and-timezone-design.md`

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `stats.py` | 核心统计模块：时区工具函数、聚合/查询 SQL 时区修正、新增 `refresh_stats()` | 修改 |
| `routers/admin.py` | 管理路由：新增 `POST /admin/stats/refresh` 端点、`get_stats()` 时区修正 | 修改 |
| `static/index.html` | 前端页面：刷新按钮改调新端点、去 alert、加行内提示 | 修改 |
| `tests/test_stats_pg.py` | 后端测试：新增 `test_refresh_stats`、`test_timezone_offset` | 修改 |

---

### 任务 1：在 `stats.py` 中添加 `utc8_now()` 工具函数并修正聚合 SQL 时区

**文件：**
- 修改：`stats.py:1-10`（import 区）、`stats.py:237-282`（`aggregate_hourly_stats`）、`stats.py:285-331`（`aggregate_daily_stats`）

- [ ] **步骤 1：添加 `utc8_now()` 函数**

在 `stats.py` 的 import 区域之后（约第 11 行，`_pool` 变量之前）插入：

```python
def utc8_now() -> datetime:
    """返回当前东8区时间（硬编码 UTC+8）"""
    return datetime.utcnow() + timedelta(hours=8)
```

- [ ] **步骤 2：修正 `aggregate_hourly_stats` 的 SQL 时区偏移**

将 `stats.py:251` 的：
```sql
date_trunc('hour', timestamp) as hour,
```
改为：
```sql
date_trunc('hour', timestamp + interval '8 hours') as hour,
```

将 `stats.py:265` 的 `GROUP BY hour` 保持不变（它引用的是 SELECT 别名，不需要改）。

- [ ] **步骤 3：修正 `aggregate_daily_stats` 的 SQL 时区偏移**

将 `stats.py:299` 的：
```sql
date_trunc('day', timestamp)::date as date,
```
改为：
```sql
date_trunc('day', timestamp + interval '8 hours')::date as date,
```

将 `stats.py:313` 的 `GROUP BY date` 保持不变（引用 SELECT 别名）。

- [ ] **步骤 4：运行 ruff 检查**

运行：`uv run ruff check stats.py`

预期：无错误

- [ ] **步骤 5：Commit**

```bash
git add stats.py
git commit -m "feat(stats): add utc8_now() and apply timezone offset to aggregate SQL"
```

---

### 任务 2：修正 `stats.py` 中查询函数的时区偏移

**文件：**
- 修改：`stats.py:378-412`（`get_daily_stats`）、`stats.py:415-459`（`get_daily_stats_from_requests`）、`stats.py:462-512`（`get_hourly_stats_from_requests`）、`stats.py:515-611`（`refresh_missing_daily_stats`）、`stats.py:614-689`（`get_overall_stats`）

- [ ] **步骤 1：修正 `get_daily_stats`**

将 `stats.py:387` 的：
```python
start_date = date.today() - timedelta(days=days - 1)
```
改为：
```python
start_date = utc8_now().date() - timedelta(days=days - 1)
```

- [ ] **步骤 2：修正 `get_daily_stats_from_requests`**

将 `stats.py:424` 的：
```python
start_date = date.today() - timedelta(days=days - 1)
```
改为：
```python
start_date = utc8_now().date() - timedelta(days=days - 1)
```

将 `stats.py:444` 的：
```sql
date_trunc('day', timestamp)::date as date,
```
改为：
```sql
date_trunc('day', timestamp + interval '8 hours')::date as date,
```

将 `stats.py:454` 的：
```sql
GROUP BY date_trunc('day', timestamp)::date
```
改为：
```sql
GROUP BY date_trunc('day', timestamp + interval '8 hours')::date
```

- [ ] **步骤 3：修正 `get_hourly_stats_from_requests`**

将 `stats.py:497` 的：
```sql
date_trunc('hour', timestamp) as hour,
```
改为：
```sql
date_trunc('hour', timestamp + interval '8 hours') as hour,
```

将 `stats.py:507` 的：
```sql
GROUP BY date_trunc('hour', timestamp)
```
改为：
```sql
GROUP BY date_trunc('hour', timestamp + interval '8 hours')
```

- [ ] **步骤 4：修正 `refresh_missing_daily_stats`**

将 `stats.py:530` 的：
```python
today = date.today()
now_dt = datetime.now()
```
改为：
```python
today = utc8_now().date()
now_dt = utc8_now()
```

将 `stats.py:536` 的：
```sql
SELECT DISTINCT date_trunc('day', timestamp)::date as d
FROM requests
WHERE date_trunc('day', timestamp)::date < $1
```
改为：
```sql
SELECT DISTINCT date_trunc('day', timestamp + interval '8 hours')::date as d
FROM requests
WHERE date_trunc('day', timestamp + interval '8 hours')::date < $1
```

- [ ] **步骤 5：修正 `get_overall_stats`**

将 `stats.py:637` 的 4 处 `now() - ($1 || ' days')::interval` 全部改为 `(now() + interval '8 hours') - ($1 || ' days')::interval`。

具体替换 4 条 SQL：

第 1 处（`stats.py:637`，总请求统计）：
```sql
WHERE timestamp >= now() - ($1 || ' days')::interval
```
→
```sql
WHERE timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
```

第 2 处（`stats.py:646`，渠道分布）：
```sql
WHERE timestamp >= now() - ($1 || ' days')::interval
```
→
```sql
WHERE timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
```

第 3 处（`stats.py:657`，模型分布）：
```sql
WHERE timestamp >= now() - ($1 || ' days')::interval
```
→
```sql
WHERE timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
```

第 4 处（`stats.py:672`，API Key 分布）：
```sql
AND timestamp >= now() - ($1 || ' days')::interval
```
→
```sql
AND timestamp >= (now() + interval '8 hours') - ($1 || ' days')::interval
```

- [ ] **步骤 6：运行 ruff 检查**

运行：`uv run ruff check stats.py`

预期：无错误

- [ ] **步骤 7：Commit**

```bash
git add stats.py
git commit -m "feat(stats): apply UTC+8 timezone offset to all query functions"
```

---

### 任务 3：修正 `routers/admin.py` 中 `get_stats()` 的时区计算

**文件：**
- 修改：`routers/admin.py:387-389`

- [ ] **步骤 1：修正 `get_stats()` 中的时间计算**

在 `routers/admin.py` 顶部（约第 15 行 `from datetime import ...` 之后）添加导入：

```python
from stats import (
    get_daily_stats, get_daily_stats_from_requests,
    get_overall_stats, get_hourly_stats, get_hourly_stats_from_requests,
    aggregate_hourly_stats, aggregate_daily_stats, list_requests,
    refresh_missing_daily_stats, get_request_field, utc8_now,
)
```

（在现有的 import 行末尾追加 `, utc8_now`）

然后将 `routers/admin.py:388-389` 的：
```python
now = datetime.now()
start_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
```
改为：
```python
now = utc8_now()
start_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
```

- [ ] **步骤 2：运行 ruff 检查**

运行：`uv run ruff check routers/admin.py`

预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add routers/admin.py
git commit -m "feat(admin): use utc8_now() for hourly stats time range in get_stats"
```

---

### 任务 4：在 `stats.py` 中新增 `refresh_stats()` 统一刷新函数

**文件：**
- 修改：`stats.py`（在 `refresh_missing_daily_stats` 函数之后、`get_overall_stats` 函数之前插入）

- [ ] **步骤 1：添加 `refresh_stats()` 函数**

在 `stats.py` 的 `refresh_missing_daily_stats()` 函数之后（约第 611 行之后）插入：

```python
async def refresh_stats() -> dict[str, Any]:
    """统一刷新统计：补全缺失历史日聚合 + 强制刷新近3天日聚合 + 刷新近24小时时聚合"""
    if not _db_available:
        return {"backfilled_count": 0, "recent_refreshed_days": 0, "hourly_refreshed": False}

    today = utc8_now().date()
    three_days_ago = today - timedelta(days=2)

    # 步骤1：补全缺失历史聚合（排除近3天，避免和步骤2重复）
    backfilled_count = 0
    async with _get_conn() as conn:
        if conn is None:
            return {"backfilled_count": 0, "recent_refreshed_days": 0, "hourly_refreshed": False}

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
                await aggregate_daily_stats(start, prev)
                refreshed.append(f"{start}~{prev}" if start != prev else str(start))
                backfilled_count = len(missing_dates)

    # 步骤2：强制刷新近3天日聚合（含当天）
    await aggregate_daily_stats(three_days_ago, today)

    # 步骤3：强制刷新近24小时时聚合
    now = utc8_now()
    hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    hour_start = hour_end - timedelta(hours=24)
    await aggregate_hourly_stats(hour_start, hour_end)

    return {
        "backfilled_count": backfilled_count,
        "recent_refreshed_days": 3,
        "hourly_refreshed": True,
    }
```

- [ ] **步骤 2：运行 ruff 检查**

运行：`uv run ruff check stats.py`

预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add stats.py
git commit -m "feat(stats): add refresh_stats() unified refresh function"
```

---

### 任务 5：在 `routers/admin.py` 中新增 `POST /admin/stats/refresh` 端点

**文件：**
- 修改：`routers/admin.py:441-450`（在 `refresh_daily_stats_endpoint` 之后插入新端点）

- [ ] **步骤 1：添加导入和新端点**

在 `routers/admin.py` 的 import 行追加 `refresh_stats`：

```python
from stats import (
    get_daily_stats, get_daily_stats_from_requests,
    get_overall_stats, get_hourly_stats, get_hourly_stats_from_requests,
    aggregate_hourly_stats, aggregate_daily_stats, list_requests,
    refresh_missing_daily_stats, get_request_field, utc8_now, refresh_stats,
)
```

在 `refresh_daily_stats_endpoint` 函数之后（约第 450 行之后）插入：

```python
@router.post("/stats/refresh")
async def refresh_stats_endpoint():
    """补全缺失历史聚合 + 强制刷新近3天日聚合 + 刷新近24小时时聚合"""
    result = await refresh_stats()
    return result
```

- [ ] **步骤 2：运行 ruff 检查**

运行：`uv run ruff check routers/admin.py`

预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add routers/admin.py
git commit -m "feat(admin): add POST /admin/stats/refresh endpoint"
```

---

### 任务 6：前端改造 — 刷新按钮改调新端点 + 去 alert + 行内提示

**文件：**
- 修改：`static/index.html:208-209`（按钮 HTML）、`static/index.html:858-888`（`refreshDailyStats` 函数）

- [ ] **步骤 1：在按钮旁添加行内提示 `<span>` 元素**

将 `static/index.html:209` 的：
```html
<button id="refreshDailyBtn" onclick="refreshDailyStats()" class="pill pill-brand hover:opacity-80 transition cursor-pointer text-xs">刷新统计</button>
```
改为：
```html
<div class="flex items-center gap-2">
  <button id="refreshDailyBtn" onclick="refreshStats()" class="pill pill-brand hover:opacity-80 transition cursor-pointer text-xs">刷新统计</button>
  <span id="refreshHint" class="text-xs text-emerald-600 opacity-0 transition-opacity duration-300"></span>
</div>
```

- [ ] **步骤 2：重写 `refreshDailyStats` 函数为 `refreshStats`**

将 `static/index.html:859-888` 的整个 `refreshDailyStats` 函数替换为：

```javascript
async function refreshStats() {
  const btn = document.getElementById('refreshDailyBtn');
  const hint = document.getElementById('refreshHint');
  const origText = btn.textContent;
  btn.textContent = '刷新中...';
  btn.disabled = true;
  hint.textContent = '';
  hint.classList.add('opacity-0');
  hint.classList.remove('opacity-100');
  try {
    const resp = await fetch('/admin/stats/refresh', { method: 'POST' });
    if (!resp.ok) throw new Error('请求失败');
    await resp.json();
    hint.textContent = '已刷新';
    hint.classList.remove('opacity-0');
    hint.classList.add('opacity-100');
    setTimeout(() => {
      hint.classList.remove('opacity-100');
      hint.classList.add('opacity-0');
    }, 1500);
    loadStats();
  } catch (e) {
    hint.textContent = '刷新失败';
    hint.classList.remove('opacity-0', 'text-emerald-600');
    hint.classList.add('opacity-100', 'text-rose-600');
    setTimeout(() => {
      hint.classList.remove('opacity-100', 'text-rose-600');
      hint.classList.add('opacity-0', 'text-emerald-600');
    }, 2000);
  } finally {
    btn.textContent = origText;
    btn.disabled = false;
  }
}
```

- [ ] **步骤 3：Commit**

```bash
git add static/index.html
git commit -m "feat(ui): refresh stats button calls new endpoint with inline hint, no alert"
```

---

### 任务 7：后端测试 — 新增 `test_timezone_offset` 和 `test_refresh_stats`

**文件：**
- 修改：`tests/test_stats_pg.py`（在文件末尾追加）

- [ ] **步骤 1：添加 `test_timezone_offset` 测试**

在 `tests/test_stats_pg.py` 末尾追加：

```python
class TestTimezoneOffset:
    async def test_daily_aggregation_respects_utc8_boundary(self):
        """验证日聚合按东8区日期归类：UTC 15:00 = 东8区 23:00 归入同一天，UTC 01:00 = 东8区 09:00"""
        from datetime import date

        pool = await _create_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers,
                 is_stream, input_tokens, output_tokens, latency_ms, success, error_msg)
                VALUES
                ($1, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL),
                ($2, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL)
                """,
                datetime(2026, 5, 2, 15, 0, 0),
                datetime(2026, 5, 2, 1, 0, 0),
            )
        await pool.close()

        today = date(2026, 5, 2)
        result = await stats.aggregate_daily_stats(today, today)
        assert result["updated_rows"] >= 1

        raw_daily = await stats.get_daily_stats(days=1)
        assert len(raw_daily) >= 1
        day_data = next((d for d in raw_daily if str(d.get("date")) == "2026-05-02"), None)
        assert day_data is not None
        assert day_data["request_count"] == 2

    async def test_daily_aggregation_cross_day_boundary(self):
        """UTC 16:00 = 东8区 00:00 次日，应归入次日的聚合行"""
        pool = await _create_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers,
                 is_stream, input_tokens, output_tokens, latency_ms, success, error_msg)
                VALUES
                ($1, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL)
                """,
                datetime(2026, 5, 2, 16, 0, 0),
            )
        await pool.close()

        result = await stats.aggregate_daily_stats(date(2026, 5, 2), date(2026, 5, 3))
        assert result["updated_rows"] >= 1

        raw_daily = await stats.get_daily_stats(days=2)
        may3_data = next((d for d in raw_daily if str(d.get("date")) == "2026-05-03"), None)
        assert may3_data is not None
        assert may3_data["request_count"] == 1
```

- [ ] **步骤 2：添加 `test_refresh_stats` 测试**

继续在 `tests/test_stats_pg.py` 末尾追加：

```python
class TestRefreshStats:
    async def test_refresh_stats_backfills_and_refreshes_recent(self):
        """验证 refresh_stats 补全历史 + 强制刷新近3天"""
        pool = await _create_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                (timestamp, model, channel_id, channel_name, api_key_id, request_headers,
                 is_stream, input_tokens, output_tokens, latency_ms, success, error_msg)
                VALUES
                ($1, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 100, 50, 200, true, NULL),
                ($2, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 200, 100, 300, true, NULL),
                ($3, 'gpt-4', 'ch_1', 'Test', '', '{}', false, 50, 25, 100, true, NULL)
                """,
                datetime(2026, 4, 28, 12, 0, 0),
                datetime(2026, 5, 1, 12, 0, 0),
                datetime.utcnow(),
            )
        await pool.close()

        result = await stats.refresh_stats()
        assert result["recent_refreshed_days"] == 3
        assert result["hourly_refreshed"] is True

        raw_daily = await stats.get_daily_stats(days=7)
        assert len(raw_daily) >= 1

        raw_hourly = await stats.get_hourly_stats(
            start_time=datetime.utcnow() - timedelta(hours=24)
        )
        assert len(raw_hourly) >= 1
```

- [ ] **步骤 3：运行测试**

运行：`uv run pytest tests/test_stats_pg.py -v`

预期：所有测试 PASS

- [ ] **步骤 4：Commit**

```bash
git add tests/test_stats_pg.py
git commit -m "test(stats): add timezone offset and refresh_stats tests"
```

---

### 任务 8：最终验证

- [ ] **步骤 1：运行 ruff 全量检查**

运行：`uv run ruff check .`

预期：无错误

- [ ] **步骤 2：运行全量测试**

运行：`uv run pytest`

预期：所有测试 PASS

- [ ] **步骤 3：Commit（如有 lint 修复）**

仅在上一步有修复时执行。
