# Request Log TTL Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `request_logs.db` 增加差异化 TTL 清理——原始数据（Header/Body）和完整记录各自独立保留期，每日后台自动清理。

**Architecture:** 两阶段清理：Phase 1 在 `raw_retention_days` 到期时将 BLOB 字段（request_headers / response_headers / request_body / response_body）置 NULL；Phase 2 在 `retention_days` 到期时删除整行。同时支持 SQLite 和 PostgreSQL 两种后端。通过 Admin API 可手动触发，服务启动后 10 秒执行一次，之后每 24 小时一次。

**Tech Stack:** Python asyncio, SQLite (`sqlite3`), PostgreSQL (`asyncpg`), FastAPI, Vanilla JS (无构建工具)

---

## 文件改动总览

| 文件 | 操作 | 职责 |
|------|------|------|
| `config.py` | 修改 | 增加 `request_log_retention_days` 和 `request_log_raw_retention_days` 配置项 |
| `request_logs.py` | 修改 | 在两个后端类及模块级增加 `cleanup_old_records()` |
| `routers/admin.py` | 修改 | 增加 `POST /admin/request-logs/cleanup` 手动触发端点 |
| `main.py` | 修改 | 增加每日后台清理循环并接入 lifespan |
| `static/index.html` | 修改 | 设置页"数据库"分区增加 TTL 配置卡片及机制说明 |
| `tests/test_request_logs.py` | 修改 | 增加 TTL 清理的集成测试 |

---

## Task 1：config.py — 新增两个配置项

**Files:**
- Modify: `config.py:69` (`_CONFIG_SCHEMA` 末尾)
- Modify: `config.py:98` (`_CONFIG_CONSTRAINTS` 末尾)

- [ ] **Step 1: 在 `_CONFIG_SCHEMA` 末尾追加两个字段**

将 `config.py` 第 69 行（`"aggregation_timezone"` 条目结尾，字典闭合大括号 `}` 前）改为：

```python
    "aggregation_timezone": {"type": "str", "default": "", "requires_restart": False},
    "request_log_retention_days": {
        "type": "int",
        "default": 0,
        "requires_restart": False,
    },
    "request_log_raw_retention_days": {
        "type": "int",
        "default": 0,
        "requires_restart": False,
    },
}
```

- [ ] **Step 2: 在 `_CONFIG_CONSTRAINTS` 追加两条约束**

将 `config.py` 第 98 行（`"aggregation_timezone"` 行后，字典闭合 `}` 前）改为：

```python
    "aggregation_timezone": {"validator": "iana_timezone"},
    "request_log_retention_days": {"min": 0},
    "request_log_raw_retention_days": {"min": 0},
}
```

- [ ] **Step 3: 验证配置可以读取和保存**

```bash
cd /home/sdial/Projects/llm-plug
uv run python -c "
import asyncio, config
asyncio.run(config.init_settings())
print('retention_days:', config.get_setting('request_log_retention_days'))
print('raw_retention_days:', config.get_setting('request_log_raw_retention_days'))
"
```

预期输出：
```
retention_days: 0
raw_retention_days: 0
```

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "feat: add request_log_retention_days and request_log_raw_retention_days config"
```

---

## Task 2：request_logs.py — 实现清理逻辑

**Files:**
- Modify: `request_logs.py` — `_BaseRequestLogBackend`、`SQLiteRequestLogBackend`、`PostgresRequestLogBackend`、模块级函数

- [ ] **Step 1: 写失败测试（先建测试，让它红）**

在 `tests/test_request_logs.py` 末尾追加：

```python
async def test_cleanup_nullifies_raw_fields_after_raw_retention(sqlite_request_logs, monkeypatch):
    """raw_retention_days=1, retention_days=365: old row's BLOB columns become NULL but row survives."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": True,
            "save_response_headers": True,
            "save_request_body": True,
            "save_response_body": True,
        },
    )

    # 写入一条带 BLOB 的记录，然后把它的 timestamp 手动改为 8 天前
    _sample_record(channel_id="ch_x", channel_name="X", model="gpt-x")
    await request_logs.drain_queue()

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(str(sqlite_request_logs))
    conn.execute("UPDATE request_logs SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    result = await request_logs.cleanup_old_records(retention_days=365, raw_retention_days=1)
    assert result["raw_fields_cleared"] == 1
    assert result["rows_deleted"] == 0

    # 行还在，但 BLOB 列已是 NULL
    field = await request_logs.get_request_field(1, "request_body")
    assert field == {"data": None}


async def test_cleanup_deletes_rows_after_retention(sqlite_request_logs):
    """retention_days=1: rows older than 1 day are deleted."""
    from datetime import datetime, timedelta, timezone

    _sample_record(channel_id="ch_old", channel_name="Old", model="gpt-old")
    await request_logs.drain_queue()

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(str(sqlite_request_logs))
    conn.execute("UPDATE request_logs SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    result = await request_logs.cleanup_old_records(retention_days=1, raw_retention_days=0)
    assert result["rows_deleted"] == 1
    assert result["raw_fields_cleared"] == 0

    listing = await request_logs.list_requests()
    assert listing["total"] == 0


async def test_cleanup_skips_raw_nullification_when_raw_days_ge_retention_days(sqlite_request_logs, monkeypatch):
    """When raw_retention_days >= retention_days, Phase 1 is skipped (rows just get deleted)."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": True,
            "save_response_headers": True,
            "save_request_body": True,
            "save_response_body": True,
        },
    )

    _sample_record(channel_id="ch_x", channel_name="X", model="gpt-x")
    await request_logs.drain_queue()

    import sqlite3
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(sep=" ", timespec="microseconds")
    conn = sqlite3.connect(str(sqlite_request_logs))
    conn.execute("UPDATE request_logs SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    # raw_days == retention_days → Phase 1 skipped, Phase 2 deletes
    result = await request_logs.cleanup_old_records(retention_days=7, raw_retention_days=7)
    assert result["raw_fields_cleared"] == 0
    assert result["rows_deleted"] == 1


async def test_cleanup_zero_days_is_noop(sqlite_request_logs):
    """Both days=0 means no cleanup."""
    _sample_record(channel_id="ch_x", channel_name="X", model="gpt-x")
    await request_logs.drain_queue()

    result = await request_logs.cleanup_old_records(retention_days=0, raw_retention_days=0)
    assert result["raw_fields_cleared"] == 0
    assert result["rows_deleted"] == 0

    listing = await request_logs.list_requests()
    assert listing["total"] == 1
```

- [ ] **Step 2: 跑测试确认它们失败**

```bash
uv run pytest tests/test_request_logs.py::test_cleanup_nullifies_raw_fields_after_raw_retention -v
```

预期：`FAILED` with `AttributeError: module 'request_logs' has no attribute 'cleanup_old_records'`

- [ ] **Step 3: 在 `_BaseRequestLogBackend` 添加抽象方法**

在 `request_logs.py` 第 159 行（`get_request_field` 方法之后，类定义结束前）插入：

```python
    async def cleanup_old_records(self, retention_days: int, raw_retention_days: int) -> dict:
        raise NotImplementedError
```

- [ ] **Step 4: 在 `SQLiteRequestLogBackend` 添加清理实现**

在 `SQLiteRequestLogBackend` 的 `get_request_field` 方法（第 370 行）之后插入：

```python
    def _cleanup_old_records_sync(self, retention_days: int, raw_retention_days: int) -> dict:
        result = {"raw_fields_cleared": 0, "rows_deleted": 0}
        with self._connect() as conn:
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
```

- [ ] **Step 5: 在 `PostgresRequestLogBackend` 添加清理实现**

在 `PostgresRequestLogBackend` 的 `get_request_field` 方法（第 565 行）之后插入：

```python
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
```

- [ ] **Step 6: 添加模块级 `cleanup_old_records` 函数**

在 `request_logs.py` 末尾（`get_request_field` 函数之后）追加：

```python
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
```

- [ ] **Step 7: 跑所有新测试确认通过**

```bash
uv run pytest tests/test_request_logs.py -v -k "cleanup"
```

预期：4 个测试全部 PASS。

- [ ] **Step 8: 跑全套 request_logs 测试确认无回归**

```bash
uv run pytest tests/test_request_logs.py -v
```

预期：全部 PASS。

- [ ] **Step 9: Commit**

```bash
git add request_logs.py tests/test_request_logs.py
git commit -m "feat: add cleanup_old_records to request log backends with two-phase TTL"
```

---

## Task 3：routers/admin.py — 手动触发端点

**Files:**
- Modify: `routers/admin.py` — 在现有 request-logs 相关路由附近添加新端点

- [ ] **Step 1: 写失败测试**

在 `tests/routers/test_admin.py` 末尾追加（使用已有的 `setup_test_db` autouse fixture 和 `client` fixture）：

```python
async def test_cleanup_request_logs_endpoint_returns_zero_when_nothing_old(client):
    """POST /admin/request-logs/cleanup returns 200 with stats dict when nothing to clean."""
    resp = await client.post("/admin/request-logs/cleanup")
    assert resp.status_code == 200
    body = resp.json()
    assert "raw_fields_cleared" in body
    assert "rows_deleted" in body
    assert body["raw_fields_cleared"] == 0
    assert body["rows_deleted"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run pytest tests/routers/test_admin.py::test_cleanup_request_logs_endpoint_returns_stats -v
```

预期：FAILED with 404 or AttributeError。

- [ ] **Step 3: 在 `routers/admin.py` 添加端点**

在 `admin.py` 的 `list_logs` 函数附近（约第 580 行，`get_request_field` 路由之后）追加：

```python
@router.post("/request-logs/cleanup")
async def cleanup_request_logs_endpoint():
    """手动触发请求记录 TTL 清理（按 settings 中的保留天数执行）"""
    return await request_logs.cleanup_old_records()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/routers/test_admin.py -v
```

预期：全部 PASS（包括新测试）。

- [ ] **Step 5: Commit**

```bash
git add routers/admin.py tests/routers/test_admin.py
git commit -m "feat: add POST /admin/request-logs/cleanup endpoint"
```

---

## Task 4：main.py — 后台自动清理循环

**Files:**
- Modify: `main.py` — 增加清理 loop，接入 lifespan

- [ ] **Step 1: 在 `main.py` 中 `_session_cleanup_loop` 函数附近（约第 54 行）添加新协程**

```python
async def _request_log_cleanup_loop():
    await asyncio.sleep(10)
    try:
        await request_logs.cleanup_old_records()
    except Exception as e:
        logger.warning(f"request log cleanup error on startup: {e}")
    while True:
        await asyncio.sleep(86400)
        try:
            await request_logs.cleanup_old_records()
        except Exception as e:
            logger.warning(f"request log cleanup error: {e}")
```

- [ ] **Step 2: 在 `lifespan` 函数中启动并注册清理任务**

找到 `lifespan` 函数（第 66 行），在 `cleanup_task` 和 `session_cleanup_task` 创建的位置（第 88~89 行）追加一行：

```python
    cleanup_task = asyncio.create_task(_client_cleanup_loop())
    session_cleanup_task = asyncio.create_task(_session_cleanup_loop())
    request_log_cleanup_task = asyncio.create_task(_request_log_cleanup_loop())
```

在 `finally` 块的取消和等待部分（第 95~102 行之后）追加：

```python
        request_log_cleanup_task.cancel()
        try:
            await request_log_cleanup_task
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 3: 手动验证服务启动日志中出现清理相关信息**

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 55555 --reload &
sleep 15
kill %1
```

若 `request_log_retention_days=0`，则不会有清理日志（0 表示不启用），这是正确行为。若手动设置 `request_log_retention_days=365`，日志会出现：
```
Request log cleanup: cleared raw fields for 0 rows, deleted 0 rows (raw_retention=7d, retention=365d)
```

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: add daily request log TTL cleanup loop in lifespan"
```

---

## Task 5：static/index.html — 设置页 TTL 配置卡片

**Files:**
- Modify: `static/index.html` — 在 `settings_database` section 末尾插入新 card；在 3 处 JS 函数中追加字段处理

- [ ] **Step 1: 在 HTML 中插入 TTL 配置卡片**

找到第 717~720 行（"Body 截断限制" card 的闭合 `</div>` 与"底部操作栏"注释之间），插入新 card：

```html
          <div class="card p-5 mt-4">
            <div class="flex items-center gap-2 mb-1.5">
              <span class="text-sm font-medium text-ink-900">数据保留策略</span>
              <span class="pill pill-success">热更新</span>
            </div>
            <p class="text-xs text-ink-600 mb-3">服务启动后 10 秒自动执行一次，之后每 24 小时运行一次。<strong>0 表示不清理</strong>，保留所有历史数据。</p>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
              <div>
                <label class="text-xs font-medium text-ink-700 block mb-1" for="set_request_log_raw_retention_days">原始数据保留天数</label>
                <div class="flex items-center gap-2">
                  <input type="number" id="set_request_log_raw_retention_days" min="0" data-section="database"
                    class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                  <span class="text-sm text-ink-500 flex-shrink-0">天</span>
                </div>
                <p class="text-xs text-ink-400 mt-1">Header 和 Body 超过此天数后被置空（行保留）</p>
              </div>
              <div>
                <label class="text-xs font-medium text-ink-700 block mb-1" for="set_request_log_retention_days">完整记录保留天数</label>
                <div class="flex items-center gap-2">
                  <input type="number" id="set_request_log_retention_days" min="0" data-section="database"
                    class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                  <span class="text-sm text-ink-500 flex-shrink-0">天</span>
                </div>
                <p class="text-xs text-ink-400 mt-1">整行记录（含元数据）超过此天数后被删除</p>
              </div>
            </div>
            <div class="bg-surface-50 rounded-lg border border-surface-200 px-4 py-3 text-xs text-ink-600 leading-5">
              <span class="font-medium text-ink-800">两阶段清理机制（示例：原始数据 7 天，完整记录 365 天）</span>
              <div class="mt-2 flex items-start gap-0 overflow-x-auto">
                <div class="flex flex-col items-center min-w-[60px]">
                  <div class="w-3 h-3 rounded-full bg-brand-500 mt-0.5"></div>
                  <div class="w-px h-8 bg-surface-300"></div>
                  <span class="text-ink-500 mt-1">写入</span>
                </div>
                <div class="h-px bg-surface-300 flex-1 mt-[5px] mx-1"></div>
                <div class="flex flex-col items-center min-w-[80px]">
                  <div class="w-3 h-3 rounded-full bg-amber-400 mt-0.5"></div>
                  <div class="w-px h-8 bg-surface-300"></div>
                  <span class="text-ink-500 mt-1 text-center">第 7 天<br>清空 RAW</span>
                </div>
                <div class="h-px bg-surface-300 flex-1 mt-[5px] mx-1"></div>
                <div class="flex flex-col items-center min-w-[80px]">
                  <div class="w-3 h-3 rounded-full bg-rose-400 mt-0.5"></div>
                  <div class="w-px h-8 bg-surface-300"></div>
                  <span class="text-ink-500 mt-1 text-center">第 365 天<br>删除整行</span>
                </div>
              </div>
              <p class="mt-2 text-ink-500">第 7 天后查看该记录的 Header/Body 时会显示「无数据」；模型、耗时、Token 等统计字段继续保留至第 365 天。若「原始数据保留天数」≥「完整记录保留天数」，两者同时在完整记录天数到期时删除（Phase 1 被跳过）。</p>
            </div>
          </div>
```

- [ ] **Step 2: 在 `loadSettings()` 函数末尾添加两个字段的加载逻辑**

找到 `loadSettings` 函数中 `document.getElementById('set_max_log_body_size_kb').value = ...` 这一行（约第 2244 行），在其后插入：

```javascript
    document.getElementById('set_request_log_raw_retention_days').value = data.request_log_raw_retention_days ?? 0;
    document.getElementById('set_request_log_retention_days').value = data.request_log_retention_days ?? 0;
```

- [ ] **Step 3: 在 `_detectSettingsDirty()` 末尾添加脏检测**

找到 `_detectSettingsDirty` 函数中 `const maxLogBodySizeKb = ...` 这行（约第 2184 行），在其后插入：

```javascript
  const rawRetentionDays = parseInt(document.getElementById('set_request_log_raw_retention_days').value) || 0;
  if (rawRetentionDays !== (orig.request_log_raw_retention_days ?? 0)) _settingsDirtySections.add('database');
  const retentionDays = parseInt(document.getElementById('set_request_log_retention_days').value) || 0;
  if (retentionDays !== (orig.request_log_retention_days ?? 0)) _settingsDirtySections.add('database');
```

- [ ] **Step 4: 在 `saveSettings()` 函数添加两个字段的收集逻辑**

找到 `saveSettings` 函数中 `const maxLogBodySizeKb = ...` 这行（约第 2278 行），在其后插入：

```javascript
  const rawRetentionDays = parseInt(document.getElementById('set_request_log_raw_retention_days').value) || 0;
  if (rawRetentionDays !== (orig.request_log_raw_retention_days ?? 0)) data.request_log_raw_retention_days = rawRetentionDays;
  const retentionDays = parseInt(document.getElementById('set_request_log_retention_days').value) || 0;
  if (retentionDays !== (orig.request_log_retention_days ?? 0)) data.request_log_retention_days = retentionDays;
```

- [ ] **Step 5: 启动服务验证 UI**

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 55555 --reload
```

打开 `http://localhost:55555` → 设置 → 数据库，确认：
- 出现"数据保留策略"卡片
- 两个输入框默认值为 `0`
- 两阶段时间轴说明可见
- 修改值后"保存设置"按钮激活（dirty 标记生效）
- 保存后刷新页面值保持

- [ ] **Step 6: Commit**

```bash
git add static/index.html
git commit -m "feat: add TTL retention settings UI in database settings page"
```

---

## Task 6：全量测试 + 最终验证

- [ ] **Step 1: 跑全套测试**

```bash
uv run pytest -v
```

预期：全部 PASS，无新增失败。

- [ ] **Step 2: 端到端功能验证**

```bash
# 1. 设置保留天数为 1 天（触发清理效果）
curl -X PUT http://localhost:55555/admin/settings \
  -H "Content-Type: application/json" \
  -d '{"request_log_retention_days": 1, "request_log_raw_retention_days": 0}'

# 2. 手动触发清理（返回清理统计）
curl -X POST http://localhost:55555/admin/request-logs/cleanup

# 3. 恢复默认（0 = 不清理）
curl -X PUT http://localhost:55555/admin/settings \
  -H "Content-Type: application/json" \
  -d '{"request_log_retention_days": 0, "request_log_raw_retention_days": 0}'
```

- [ ] **Step 3: Commit（如有遗漏调整）**

```bash
git add -p
git commit -m "fix: final adjustments from e2e verification"
```

---

## 设计决策备注

| 问题 | 决策 |
|------|------|
| `raw_days >= retention_days` 时如何处理 | Phase 1 跳过，行在 `retention_days` 时直接删除，无需报错 |
| BLOB 置 NULL 后 json-viewer 的展示 | 返回 `{"data": null}`，viewer 显示"无数据"——与"从未保存"一致，不增加 `expired` 标记 |
| `stats.db` 的 `request_stats_raw` TTL | 不在本次范围，后续单独规划 |
| PostgreSQL `asyncpg` rowcount 获取方式 | `conn.execute()` 返回字符串如 `"UPDATE 5"`，取 `int(tag.split()[-1])` |
| 清理间隔 | 24 小时，无需配置化（频繁运行无收益） |
