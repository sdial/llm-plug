# 请求记录 TAB 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在管理后台新增「请求记录」TAB，展示 PostgreSQL requests 表数据，支持搜索过滤、分页浏览和详情查看，URL hash 同步状态。

**Architecture:** 后端新增 `list_requests()` 数据查询函数和 `/admin/requests` REST 端点；前端在 `index.html` 新增 TAB 面板、搜索表单、表格、分页器、详情模态框及 URL hash 同步逻辑。

**Tech Stack:** Python (FastAPI, asyncpg), Vanilla JS + Tailwind CSS (CDN)

---

## 文件映射

| 文件 | 操作 | 职责 |
|------|------|------|
| `stats_pg.py` | 修改 | 新增 `list_requests()` 动态 SQL 查询函数 |
| `routers/admin.py` | 修改 | 新增 `GET /admin/requests` 端点 |
| `static/index.html` | 修改 | 新增 TAB HTML + JS 交互逻辑 |
| `tests/test_stats_pg.py` | 修改 | 新增 `list_requests()` 单元测试 |
| `tests/routers/test_admin.py` | 创建 | 新增 `/admin/requests` 端点集成测试 |

---

### Task 1: `stats_pg.py` 新增 `list_requests()`

**Files:**
- Modify: `stats_pg.py`
- Test: `tests/test_stats_pg.py`

- [x] **Step 1: 写 failing test**

在 `tests/test_stats_pg.py` 末尾新增 `TestListRequests` 类：

```python
class TestListRequests:
    async def test_empty_result(self):
        result = await stats_pg.list_requests(page=1, page_size=10)
        assert result["items"] == []
        assert result["total"] == 0
        assert result["page"] == 1
        assert result["page_size"] == 10

    async def test_pagination(self):
        for i in range(15):
            await stats_pg.record_request(
                channel_id=f"ch_{i}", channel_name=f"Channel {i}", model="gpt-4",
                is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            )
        result = await stats_pg.list_requests(page=1, page_size=10)
        assert len(result["items"]) == 10
        assert result["total"] == 15

        result = await stats_pg.list_requests(page=2, page_size=10)
        assert len(result["items"]) == 5
        assert result["total"] == 15

    async def test_filter_by_model(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-3.5",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(model="gpt-4")
        assert result["total"] == 1
        assert result["items"][0]["model"] == "gpt-4"

    async def test_filter_by_success(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=False,
        )
        result = await stats_pg.list_requests(success=True)
        assert result["total"] == 1
        assert result["items"][0]["success"] is True

        result = await stats_pg.list_requests(success=False)
        assert result["total"] == 1
        assert result["items"][0]["success"] is False

    async def test_filter_by_channel(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Alpha", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_2", channel_name="Beta", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(channel="Alpha")
        assert result["total"] == 1
        assert result["items"][0]["channel_name"] == "Alpha"

    async def test_filter_by_is_stream(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=True, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(is_stream=True)
        assert result["total"] == 1
        assert result["items"][0]["is_stream"] is True

    async def test_combined_filters(self):
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        await stats_pg.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-3.5",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        result = await stats_pg.list_requests(model="gpt-4", success=True)
        assert result["total"] == 1
        assert result["items"][0]["model"] == "gpt-4"
```

- [x] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_stats_pg.py::TestListRequests -v`
Expected: FAIL with `AttributeError: module 'stats_pg' has no attribute 'list_requests'`

- [x] **Step 3: 实现 `list_requests()`**

在 `stats_pg.py` 的 `cleanup_old_data` 函数之后新增：

```python
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
```

- [x] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_stats_pg.py::TestListRequests -v`
Expected: ALL PASS

- [x] **Step 5: Commit**

```bash
git add tests/test_stats_pg.py stats_pg.py
git commit -m "feat: add list_requests() with pagination and filtering"
```

---

### Task 2: `routers/admin.py` 新增 `/admin/requests` 端点

**Files:**
- Modify: `routers/admin.py`
- Test: `tests/routers/test_admin.py`

- [x] **Step 1: 写 failing test**

创建 `tests/routers/test_admin.py`：

```python
import os
import pytest
import pytest_asyncio
import asyncpg

import stats_pg
from main import app
from fastapi.testclient import TestClient

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(stats_pg, "DATABASE_URL", TEST_DB_URL)
    pool = await asyncpg.create_pool(TEST_DB_URL)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS hourly_stats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
    await pool.close()
    await stats_pg.init_db()
    yield
    await stats_pg.close_pool()


class TestListRequestsEndpoint:
    async def test_returns_empty_list(self):
        with TestClient(app) as client:
            resp = client.get("/admin/requests")
            assert resp.status_code == 200
            data = resp.json()
            assert data["items"] == []
            assert data["total"] == 0

    async def test_pagination_and_filtering(self):
        with TestClient(app) as client:
            for i in range(15):
                await stats_pg.record_request(
                    channel_id=f"ch_{i}", channel_name=f"Channel {i}", model="gpt-4",
                    is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
                )
            resp = client.get("/admin/requests?page=1&page_size=10")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["items"]) == 10
            assert data["total"] == 15

            resp = client.get("/admin/requests?page=2&page_size=10")
            data = resp.json()
            assert len(data["items"]) == 5
```

- [x] **Step 2: 运行测试确认失败**

Run: `pytest tests/routers/test_admin.py -v`
Expected: FAIL with `404 Not Found`

- [x] **Step 3: 实现端点**

在 `routers/admin.py` 中：

1. 修改导入行：

old_string:
```python
from stats_pg import cleanup_old_data, get_daily_stats, get_overall_stats, aggregate_hourly_stats, aggregate_daily_stats
```

new_string:
```python
from stats_pg import cleanup_old_data, get_daily_stats, get_overall_stats, aggregate_hourly_stats, aggregate_daily_stats, list_requests
```

2. 在文件末尾（`trigger_daily_aggregation` 之后）新增端点：

```python
@router.get("/requests")
async def list_requests_endpoint(
    model: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    success: bool | None = Query(default=None),
    api_key_id: str | None = Query(default=None),
    is_stream: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
):
    """查询请求记录（支持分页和过滤）"""
    result = await list_requests(
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
    return result
```

- [x] **Step 4: 运行测试确认通过**

Run: `pytest tests/routers/test_admin.py -v`
Expected: ALL PASS

- [x] **Step 5: Commit**

```bash
git add tests/routers/test_admin.py routers/admin.py
git commit -m "feat: add /admin/requests endpoint"
```

---

### Task 3: `index.html` 新增「请求记录」TAB

**Files:**
- Modify: `static/index.html`

- [x] **Step 1: 新增 TAB 按钮**

old_string:
```html
            <button onclick="switchTab('stats')" id="tab_stats" class="px-4 py-2.5 text-sm font-medium tab-inactive">统计</button>
        </div>
    </div>
```

new_string:
```html
            <button onclick="switchTab('stats')" id="tab_stats" class="px-4 py-2.5 text-sm font-medium tab-inactive">统计</button>
            <button onclick="switchTab('requests')" id="tab_requests" class="px-4 py-2.5 text-sm font-medium tab-inactive">请求记录</button>
        </div>
    </div>
```

- [x] **Step 2: 新增 `requestsTab` 面板（在 `statsTab` 之后）**

old_string:
```html
        </div>
    </div>

    <!-- 添加/编辑渠道模态框 -->
```

new_string:
```html
        </div>

        <!-- 请求记录 Tab -->
        <div id="requestsTab" class="hidden">
            <!-- 搜索表单 -->
            <div class="card p-4 mb-4">
                <div class="flex flex-wrap gap-3 items-end">
                    <div>
                        <label class="block text-xs text-ink-600 mb-1">模型</label>
                        <input type="text" id="reqFilterModel" placeholder="模型名称" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white w-32">
                    </div>
                    <div>
                        <label class="block text-xs text-ink-600 mb-1">渠道</label>
                        <select id="reqFilterChannel" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white w-40">
                            <option value="">全部渠道</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs text-ink-600 mb-1">开始时间</label>
                        <input type="datetime-local" id="reqFilterStart" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                    </div>
                    <div>
                        <label class="block text-xs text-ink-600 mb-1">结束时间</label>
                        <input type="datetime-local" id="reqFilterEnd" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                    </div>
                    <div>
                        <label class="block text-xs text-ink-600 mb-1">状态</label>
                        <select id="reqFilterSuccess" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white w-28">
                            <option value="">全部</option>
                            <option value="true">成功</option>
                            <option value="false">失败</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs text-ink-600 mb-1">API Key ID</label>
                        <input type="text" id="reqFilterApiKeyId" placeholder="API Key ID" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white w-32">
                    </div>
                    <div class="flex items-center gap-2 pb-0.5">
                        <input type="checkbox" id="reqFilterStream" class="rounded border-surface-200 text-brand-500 focus:ring-brand-500/30">
                        <label for="reqFilterStream" class="text-sm text-ink-900">仅流式</label>
                    </div>
                    <button onclick="searchRequests()" class="btn-primary text-sm px-3 py-1.5 font-medium">搜索</button>
                    <button onclick="resetRequestFilters()" class="btn-secondary text-sm px-3 py-1.5 font-medium">重置</button>
                </div>
            </div>

            <!-- 表格 -->
            <div class="card overflow-hidden mb-4">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b border-surface-200">
                            <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">时间</th>
                            <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">模型</th>
                            <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">渠道</th>
                            <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">输入Token</th>
                            <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">输出Token</th>
                            <th class="text-right py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">延迟(ms)</th>
                            <th class="text-left py-3 px-4 text-xs text-ink-600 font-semibold uppercase tracking-wider">状态</th>
                        </tr>
                    </thead>
                    <tbody id="requestsTbody">
                        <tr><td colspan="7" class="py-4 text-center text-ink-400 text-sm">加载中...</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- 分页器 -->
            <div class="flex items-center justify-between">
                <div class="text-sm text-ink-600">
                    共 <span id="reqTotal">0</span> 条
                </div>
                <div class="flex items-center gap-2">
                    <select id="reqPageSize" onchange="changeRequestPageSize()" class="text-sm border border-surface-200 rounded-lg px-2 py-1.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                        <option value="10">10条/页</option>
                        <option value="20">20条/页</option>
                        <option value="50">50条/页</option>
                        <option value="100">100条/页</option>
                    </select>
                    <button onclick="prevRequestPage()" id="reqPrevBtn" class="btn-secondary text-sm px-3 py-1.5 font-medium">上一页</button>
                    <span class="text-sm text-ink-600">第 <span id="reqPage">1</span> 页</span>
                    <button onclick="nextRequestPage()" id="reqNextBtn" class="btn-secondary text-sm px-3 py-1.5 font-medium">下一页</button>
                </div>
            </div>
        </div>
    </div>

    <!-- 添加/编辑渠道模态框 -->
```

- [x] **Step 3: 新增详情模态框（在 `copyKeyModal` 之后）**

old_string:
```html
    </div>

    <script>
```

new_string:
```html
    </div>

    <!-- 请求详情模态框 -->
    <div id="requestDetailModal" class="fixed inset-0 bg-black/30 hidden flex items-center justify-center z-50 backdrop-blur-sm">
        <div class="bg-white rounded-2xl shadow-xl w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
            <div class="p-6">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-base font-bold text-ink-900">请求详情</h3>
                    <button onclick="closeRequestDetailModal()" class="text-ink-400 hover:text-ink-600 text-2xl leading-none">&times;</button>
                </div>
                <div id="requestDetailContent" class="space-y-3 text-sm"></div>
            </div>
        </div>
    </div>

    <script>
```

- [x] **Step 4: 新增全局变量**

old_string:
```javascript
        let pendingCopyKey = '';

        function esc(s) {
```

new_string:
```javascript
        let pendingCopyKey = '';
        const API_REQUESTS = '/admin/requests';
        let requestsData = [];
        let requestPage = 1;
        let requestPageSize = 10;
        let requestTotal = 0;

        function esc(s) {
```

- [x] **Step 5: 修改 `switchTab` 支持 requests**

old_string:
```javascript
        function switchTab(tab, updateHash = true) {
            currentTab = tab;
            if (updateHash) {
                history.replaceState(null, '', '#' + tab);
            }
            const tabs = ['channels', 'apikeys', 'stats'];
            const panelMap = { channels: 'channelsTab', apikeys: 'apikeysTab', stats: 'statsTab' };
```

new_string:
```javascript
        function switchTab(tab, updateHash = true) {
            currentTab = tab;
            if (updateHash) {
                if (tab === 'requests') {
                    syncRequestHash();
                } else {
                    history.replaceState(null, '', '#' + tab);
                }
            }
            const tabs = ['channels', 'apikeys', 'stats', 'requests'];
            const panelMap = { channels: 'channelsTab', apikeys: 'apikeysTab', stats: 'statsTab', requests: 'requestsTab' };
```

并在 `switchTab` 的 `if` 链末尾新增：

old_string:
```javascript
            if (tab === 'stats') loadStats();
            if (tab === 'apikeys') loadApiKeys();
        }
```

new_string:
```javascript
            if (tab === 'stats') loadStats();
            if (tab === 'apikeys') loadApiKeys();
            if (tab === 'requests') loadRequests();
        }
```

- [x] **Step 6: 修改 `initTabFromHash` 支持 requests 状态恢复**

old_string:
```javascript
        function initTabFromHash() {
            const hash = window.location.hash.slice(1);
            const validTabs = ['channels', 'apikeys', 'stats'];
            if (hash && validTabs.includes(hash)) {
                switchTab(hash, false);
            }
        }
```

new_string:
```javascript
        function initTabFromHash() {
            const hash = window.location.hash.slice(1);
            const [tab, queryString] = hash.split('?');
            const validTabs = ['channels', 'apikeys', 'stats', 'requests'];
            if (tab && validTabs.includes(tab)) {
                if (tab === 'requests' && queryString) {
                    const params = new URLSearchParams(queryString);
                    document.getElementById('reqFilterModel').value = params.get('model') || '';
                    document.getElementById('reqFilterChannel').value = params.get('channel') || '';
                    document.getElementById('reqFilterStart').value = params.get('start') || '';
                    document.getElementById('reqFilterEnd').value = params.get('end') || '';
                    document.getElementById('reqFilterSuccess').value = params.get('success') || '';
                    document.getElementById('reqFilterApiKeyId').value = params.get('api_key_id') || '';
                    document.getElementById('reqFilterStream').checked = params.get('is_stream') === 'true';
                    requestPage = parseInt(params.get('page')) || 1;
                    requestPageSize = parseInt(params.get('page_size')) || 10;
                    document.getElementById('reqPageSize').value = requestPageSize;
                }
                switchTab(tab, false);
            }
        }
```

- [x] **Step 7: 在 `cleanupStats` 之后新增请求记录相关 JS 函数**

old_string:
```javascript
        async function cleanupStats() {
            const days = document.getElementById('cleanup_days').value;
            if (!confirm(`确定要清理 ${days} 天前的数据吗？此操作不可恢复。`)) return;
            try {
                const resp = await fetch(`/admin/stats/cleanup?keep_days=${days}`, { method: 'POST' });
                const result = await resp.json();
                alert(result.message);
                loadStats();
            } catch (e) {
                alert('清理失败: ' + e.message);
            }
        }

        // 初始化
        loadChannels();
        initTabFromHash();
```

new_string:
```javascript
        async function cleanupStats() {
            const days = document.getElementById('cleanup_days').value;
            if (!confirm(`确定要清理 ${days} 天前的数据吗？此操作不可恢复。`)) return;
            try {
                const resp = await fetch(`/admin/stats/cleanup?keep_days=${days}`, { method: 'POST' });
                const result = await resp.json();
                alert(result.message);
                loadStats();
            } catch (e) {
                alert('清理失败: ' + e.message);
            }
        }

        // ========== 请求记录 ==========
        async function loadRequests() {
            try {
                if (channels.length === 0) {
                    await loadChannels();
                }
                populateRequestChannelFilter();

                const params = buildRequestQuery();
                const resp = await fetch(`${API_REQUESTS}?${params.toString()}`);
                const data = await resp.json();
                requestsData = data.items || [];
                requestTotal = data.total || 0;
                requestPage = data.page || 1;
                requestPageSize = data.page_size || 10;
                renderRequests();
                renderRequestPagination();
            } catch (e) {
                console.error('加载请求记录失败:', e);
                document.getElementById('requestsTbody').innerHTML = '<tr><td colspan="7" class="py-4 text-center text-ink-400 text-sm">加载失败</td></tr>';
            }
        }

        function populateRequestChannelFilter() {
            const select = document.getElementById('reqFilterChannel');
            const currentVal = select.value;
            select.innerHTML = '<option value="">全部渠道</option>';
            channels.forEach(ch => {
                select.innerHTML += `<option value="${esc(ch.name)}">${esc(ch.name)}</option>`;
            });
            select.value = currentVal;
        }

        function buildRequestQuery() {
            const params = new URLSearchParams();
            const model = document.getElementById('reqFilterModel').value.trim();
            if (model) params.set('model', model);
            const channel = document.getElementById('reqFilterChannel').value;
            if (channel) params.set('channel', channel);
            const start = document.getElementById('reqFilterStart').value;
            if (start) params.set('start', start);
            const end = document.getElementById('reqFilterEnd').value;
            if (end) params.set('end', end);
            const success = document.getElementById('reqFilterSuccess').value;
            if (success) params.set('success', success);
            const apiKeyId = document.getElementById('reqFilterApiKeyId').value.trim();
            if (apiKeyId) params.set('api_key_id', apiKeyId);
            if (document.getElementById('reqFilterStream').checked) params.set('is_stream', 'true');
            params.set('page', requestPage);
            params.set('page_size', requestPageSize);
            return params;
        }

        function renderRequests() {
            const tbody = document.getElementById('requestsTbody');
            if (!requestsData.length) {
                tbody.innerHTML = '<tr><td colspan="7" class="py-4 text-center text-ink-400 text-sm">暂无请求记录</td></tr>';
                return;
            }
            tbody.innerHTML = requestsData.map(req => `
                <tr class="border-b border-surface-200 last:border-0 hover:bg-surface-50 transition-colors duration-150 cursor-pointer" onclick="openRequestDetail(${req.id})">
                    <td class="py-3 px-4 text-sm text-ink-900">${formatTimestamp(req.timestamp)}</td>
                    <td class="py-3 px-4 text-sm text-ink-900">${esc(req.model)}</td>
                    <td class="py-3 px-4 text-sm text-ink-900">${esc(req.channel_name)}</td>
                    <td class="py-3 px-4 text-right text-sm text-ink-900 font-medium">${req.input_tokens || 0}</td>
                    <td class="py-3 px-4 text-right text-sm text-ink-900 font-medium">${req.output_tokens || 0}</td>
                    <td class="py-3 px-4 text-right text-sm text-ink-900 font-medium">${req.latency_ms}</td>
                    <td class="py-3 px-4">
                        <span class="pill ${req.success ? 'pill-success' : 'pill-danger'}">${req.success ? '成功' : '失败'}</span>
                    </td>
                </tr>
            `).join('');
        }

        function formatTimestamp(ts) {
            const d = new Date(ts);
            return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).replace(/\//g, '-');
        }

        function renderRequestPagination() {
            document.getElementById('reqTotal').textContent = requestTotal;
            document.getElementById('reqPage').textContent = requestPage;
            document.getElementById('reqPrevBtn').disabled = requestPage <= 1;
            document.getElementById('reqNextBtn').disabled = requestPage * requestPageSize >= requestTotal;
        }

        function prevRequestPage() {
            if (requestPage > 1) {
                requestPage--;
                loadRequests();
                syncRequestHash();
            }
        }

        function nextRequestPage() {
            if (requestPage * requestPageSize < requestTotal) {
                requestPage++;
                loadRequests();
                syncRequestHash();
            }
        }

        function changeRequestPageSize() {
            requestPageSize = parseInt(document.getElementById('reqPageSize').value);
            requestPage = 1;
            loadRequests();
            syncRequestHash();
        }

        function searchRequests() {
            requestPage = 1;
            loadRequests();
            syncRequestHash();
        }

        function resetRequestFilters() {
            document.getElementById('reqFilterModel').value = '';
            document.getElementById('reqFilterChannel').value = '';
            document.getElementById('reqFilterStart').value = '';
            document.getElementById('reqFilterEnd').value = '';
            document.getElementById('reqFilterSuccess').value = '';
            document.getElementById('reqFilterApiKeyId').value = '';
            document.getElementById('reqFilterStream').checked = false;
            requestPage = 1;
            loadRequests();
            syncRequestHash();
        }

        function syncRequestHash() {
            const params = new URLSearchParams();
            const model = document.getElementById('reqFilterModel').value.trim();
            if (model) params.set('model', model);
            const channel = document.getElementById('reqFilterChannel').value;
            if (channel) params.set('channel', channel);
            const start = document.getElementById('reqFilterStart').value;
            if (start) params.set('start', start);
            const end = document.getElementById('reqFilterEnd').value;
            if (end) params.set('end', end);
            const success = document.getElementById('reqFilterSuccess').value;
            if (success) params.set('success', success);
            const apiKeyId = document.getElementById('reqFilterApiKeyId').value.trim();
            if (apiKeyId) params.set('api_key_id', apiKeyId);
            if (document.getElementById('reqFilterStream').checked) params.set('is_stream', 'true');
            if (requestPage !== 1) params.set('page', requestPage);
            if (requestPageSize !== 10) params.set('page_size', requestPageSize);

            const query = params.toString();
            history.replaceState(null, '', '#requests' + (query ? '?' + query : ''));
        }

        function openRequestDetail(id) {
            const req = requestsData.find(r => r.id === id);
            if (!req) return;

            const content = document.getElementById('requestDetailContent');
            content.innerHTML = `
                <div class="grid grid-cols-2 gap-4">
                    <div><span class="text-ink-400">ID:</span> <span class="text-ink-900 font-mono">${req.id}</span></div>
                    <div><span class="text-ink-400">时间:</span> <span class="text-ink-900">${formatTimestamp(req.timestamp)}</span></div>
                    <div><span class="text-ink-400">模型:</span> <span class="text-ink-900">${esc(req.model)}</span></div>
                    <div><span class="text-ink-400">渠道:</span> <span class="text-ink-900">${esc(req.channel_name)}</span></div>
                    <div><span class="text-ink-400">渠道ID:</span> <span class="text-ink-900 font-mono">${esc(req.channel_id)}</span></div>
                    <div><span class="text-ink-400">API Key ID:</span> <span class="text-ink-900 font-mono">${esc(req.api_key_id || '-')}</span></div>
                    <div><span class="text-ink-400">流式:</span> <span class="text-ink-900">${req.is_stream ? '是' : '否'}</span></div>
                    <div><span class="text-ink-400">状态:</span> <span class="pill ${req.success ? 'pill-success' : 'pill-danger'}">${req.success ? '成功' : '失败'}</span></div>
                    <div><span class="text-ink-400">输入Token:</span> <span class="text-ink-900">${req.input_tokens || 0}</span></div>
                    <div><span class="text-ink-400">输出Token:</span> <span class="text-ink-900">${req.output_tokens || 0}</span></div>
                    <div><span class="text-ink-400">延迟:</span> <span class="text-ink-900">${req.latency_ms}ms</span></div>
                    <div><span class="text-ink-400">Lag:</span> <span class="text-ink-900">${req.lag_ms != null ? req.lag_ms + 'ms' : '-'}</span></div>
                    <div><span class="text-ink-400">Cost:</span> <span class="text-ink-900">${req.cost != null ? req.cost : '-'}</span></div>
                    <div><span class="text-ink-400">Finish Reason:</span> <span class="text-ink-900">${esc(req.finish_reason || '-')}</span></div>
                </div>
                <div class="mt-3">
                    <div class="text-ink-400 mb-1">Headers:</div>
                    <pre class="bg-ink-900 rounded-xl p-3 text-xs text-emerald-400 font-mono overflow-x-auto">${esc(JSON.stringify(req.headers || {}, null, 2))}</pre>
                </div>
                ${req.error_msg ? `
                <div class="mt-3">
                    <div class="text-ink-400 mb-1">错误信息:</div>
                    <div class="bg-rose-50 border border-rose-100 rounded-xl p-3 text-sm text-rose-700">${esc(req.error_msg)}</div>
                </div>
                ` : ''}
            `;
            document.getElementById('requestDetailModal').classList.remove('hidden');
        }

        function closeRequestDetailModal() {
            document.getElementById('requestDetailModal').classList.add('hidden');
        }

        // 初始化
        loadChannels();
        initTabFromHash();
```

- [x] **Step 8: Commit**

```bash
git add static/index.html
git commit -m "feat: add requests tab UI with search, pagination and detail modal"
```

---

### Task 4: 整合验证

- [x] **Step 1: 运行所有相关测试**

Run: `pytest tests/test_stats_pg.py tests/routers/test_admin.py -v`
Expected: ALL PASS

- [x] **Step 2: 手动验证前端**

启动服务：`uv run main.py`
打开浏览器访问 `http://localhost:8000/static/index.html`
验证：
1. 切换到「请求记录」TAB，数据加载正常
2. 搜索表单各字段过滤有效
3. 分页器翻页、每页条数切换正常
4. 点击表格行弹出详情模态框
5. URL hash 随搜索/分页同步
6. 刷新页面后状态和搜索结果保持

- [x] **Step 3: Commit（如有修复）**

```bash
git add -A
git commit -m "fix: requests tab integration fixes"
```

---

## Self-Review Checklist

**1. Spec coverage:**
- [x] 新增 TAB — Task 3
- [x] 搜索表单（模型、渠道、时间、状态、API Key ID、是否流式）— Task 3
- [x] 分页器（默认10，可选20/50/100）— Task 1 + Task 3
- [x] 详情模态框 — Task 3
- [x] URL hash 同步 — Task 3

**2. Placeholder scan:**
- [x] 无 TBD/TODO/"implement later"
- [x] 所有步骤包含完整代码和命令
- [x] 无模糊描述

**3. Type consistency:**
- [x] `list_requests()` 参数名与端点 Query 参数名一致
- [x] `page_size` 默认值前后一致（10）
- [x] URL hash 参数名与 buildRequestQuery 中一致
