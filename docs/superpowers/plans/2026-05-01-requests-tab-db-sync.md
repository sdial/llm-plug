# 请求记录标签页适配数据库变更 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [x]`）语法来跟踪进度。

**目标：** 适配前端请求记录标签页以展示数据库新增的 request_headers、response_headers、request_body、response_body 字段。

**架构：** 后端新增 4 个按需获取单个 JSONB 字段的 API 端点，优化 list_requests 不再传输大字段；前端详情弹窗改为 4 个链接点击在新标签页打开独立 JSON 查看器。

**技术栈：** Python/FastAPI/asyncpg, 原生 HTML/JS

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `stats.py` | 新增 `get_request_field()`；修改 `list_requests()` SELECT 移除 4 个大 JSONB 字段 |
| `routers/admin.py` | 新增 4 个 GET 端点 |
| `static/index.html` | 详情弹窗替换 headers 展示为 4 个链接；新增 `openJsonInNewTab()` |
| `static/json-viewer.html` | 新文件 — 独立 JSON 查看器页面 |
| `tests/test_stats_pg.py` | 新增 `get_request_field` 和 `list_requests` 不含大字段的测试 |
| `tests/routers/test_admin.py` | 新增 4 个端点的 API 测试 |

---

### 任务 1：stats.py — 新增 get_request_field 函数

**文件：**
- 修改：`stats.py:691`（在 `list_requests` 函数前插入）
- 测试：`tests/test_stats_pg.py`

- [x] **步骤 1：编写失败的测试**

在 `tests/test_stats_pg.py` 末尾新增类 `TestGetRequestField`：

```python
class TestGetRequestField:
    async def test_get_request_headers(self):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            request_headers={"X-App-Name": "TestApp"},
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        result = await stats.get_request_field(req_id, "request_headers")
        assert result["data"]["X-App-Name"] == "TestApp"

    async def test_get_request_body(self):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            request_body={"messages": [{"role": "user", "content": "hi"}]},
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        result = await stats.get_request_field(req_id, "request_body")
        assert result["data"]["messages"][0]["content"] == "hi"

    async def test_get_null_field(self):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        result = await stats.get_request_field(req_id, "response_body")
        assert result["data"] is None

    async def test_nonexistent_id(self):
        result = await stats.get_request_field(999999, "request_headers")
        assert result is None

    async def test_invalid_field(self):
        result = await stats.get_request_field(1, "invalid_field")
        assert result is None
```

- [x] **步骤 2：运行测试验证失败**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/test_stats_pg.py::TestGetRequestField -v`
预期：FAIL，报错 `AttributeError: module 'stats' has no attribute 'get_request_field'`

- [x] **步骤 3：编写实现代码**

在 `stats.py` 的 `list_requests` 函数之前（约第 691 行）插入：

```python
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
```

- [x] **步骤 4：运行测试验证通过**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/test_stats_pg.py::TestGetRequestField -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add stats.py tests/test_stats_pg.py
git commit -m "feat: add get_request_field() for on-demand JSONB field queries"
```

---

### 任务 2：stats.py — 优化 list_requests 移除大 JSONB 字段

**文件：**
- 修改：`stats.py:749-753`
- 测试：`tests/test_stats_pg.py`

- [x] **步骤 1：编写失败的测试**

在 `tests/test_stats_pg.py` 的 `TestListRequests` 类中新增：

```python
    async def test_list_requests_no_jsonb_fields(self):
        """list_requests 不应返回 request_headers, response_headers, request_body, response_body"""
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            request_headers={"X-App": "Test"},
            request_body={"messages": []},
            response_headers={"X-Resp": "Test"},
            response_body={"choices": []},
        )
        result = await stats.list_requests(page=1, page_size=10)
        assert result["total"] == 1
        item = result["items"][0]
        assert "request_headers" not in item
        assert "response_headers" not in item
        assert "request_body" not in item
        assert "response_body" not in item
        assert "model" in item
        assert "channel_id" in item
```

- [x] **步骤 2：运行测试验证失败**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/test_stats_pg.py::TestListRequests::test_list_requests_no_jsonb_fields -v`
预期：FAIL，因为 `request_headers` 等字段仍然出现在 item 中

- [x] **步骤 3：修改 list_requests 的 SELECT**

修改 `stats.py` 第 749-753 行，将：

```python
            SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                   request_headers, response_headers, request_body, response_body,
                   is_stream, input_tokens, output_tokens, cost, latency_ms, lag_ms,
                   finish_reason, success, error_msg
```

改为：

```python
            SELECT id, timestamp, model, channel_id, channel_name, api_key_id,
                   is_stream, input_tokens, output_tokens, cost, latency_ms, lag_ms,
                   finish_reason, success, error_msg
```

- [x] **步骤 4：运行测试验证通过**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/test_stats_pg.py::TestListRequests -v`
预期：全部 PASS

- [x] **步骤 5：Commit**

```bash
git add stats.py tests/test_stats_pg.py
git commit -m "perf: remove large JSONB fields from list_requests SELECT"
```

---

### 任务 3：routers/admin.py — 新增 4 个 GET 端点

**文件：**
- 修改：`routers/admin.py:495`（在 `list_requests_endpoint` 之后）
- 测试：`tests/routers/test_admin.py`

- [x] **步骤 1：编写失败的测试**

在 `tests/routers/test_admin.py` 末尾新增类：

```python
class TestRequestFieldEndpoints:
    async def test_get_request_headers(self, client):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            request_headers={"X-App-Name": "TestApp"},
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/request-headers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["X-App-Name"] == "TestApp"

    async def test_get_request_body(self, client):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            request_body={"messages": [{"role": "user", "content": "hi"}]},
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/request-body")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["messages"][0]["content"] == "hi"

    async def test_get_response_headers(self, client):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            response_headers={"X-RateLimit": "100"},
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-headers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["X-RateLimit"] == "100"

    async def test_get_response_body(self, client):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
            response_body={"choices": [{"message": {"content": "hello"}}]},
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-body")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["choices"][0]["message"]["content"] == "hello"

    async def test_nonexistent_id_returns_404(self, client):
        resp = await client.get("/admin/requests/999999/request-headers")
        assert resp.status_code == 404

    async def test_invalid_field_returns_400(self, client):
        resp = await client.get("/admin/requests/1/invalid-field")
        assert resp.status_code == 400

    async def test_null_field_returns_null_data(self, client):
        await stats.record_request(
            channel_id="ch_1", channel_name="Test", model="gpt-4",
            is_stream=False, input_tokens=10, output_tokens=5, latency_ms=100, success=True,
        )
        all_reqs = await stats.list_requests(page=1, page_size=1)
        req_id = all_reqs["items"][0]["id"]
        resp = await client.get(f"/admin/requests/{req_id}/response-body")
        assert resp.status_code == 200
        assert resp.json()["data"] is None
```

- [x] **步骤 2：运行测试验证失败**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/routers/test_admin.py::TestRequestFieldEndpoints -v`
预期：FAIL，404 因为端点不存在

- [x] **步骤 3：编写实现代码**

在 `routers/admin.py` 中，先在文件顶部的 import 区域添加 `get_request_field`：

```python
from stats import (
    get_daily_stats, get_daily_stats_from_requests,
    get_overall_stats, get_hourly_stats, get_hourly_stats_from_requests,
    aggregate_hourly_stats, aggregate_daily_stats, list_requests,
    refresh_missing_daily_stats, get_request_field,
)
```

然后在 `list_requests_endpoint` 函数之后（第 495 行之后）添加：

```python
# URL 路径字段名 → stats.get_request_field 的 field 参数名
_FIELD_PATH_MAP = {
    "request-headers": "request_headers",
    "request-body": "request_body",
    "response-headers": "response_headers",
    "response-body": "response_body",
}


@router.get("/requests/{request_id}/{field_name}")
async def get_request_field_endpoint(request_id: int, field_name: str):
    """获取单个请求的单个 JSONB 字段（请求/返回的 Header 或 Body）"""
    field = _FIELD_PATH_MAP.get(field_name)
    if field is None:
        raise HTTPException(status_code=400, detail=f"不支持的字段: {field_name}")
    result = await get_request_field(request_id, field)
    if result is None:
        raise HTTPException(status_code=404, detail="请求记录不存在")
    return result
```

- [x] **步骤 4：运行测试验证通过**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/routers/test_admin.py::TestRequestFieldEndpoints -v`
预期：PASS

- [x] **步骤 5：运行全部现有测试确认无回归**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/routers/test_admin.py tests/test_stats_pg.py -v`
预期：全部 PASS

- [x] **步骤 6：Commit**

```bash
git add routers/admin.py tests/routers/test_admin.py
git commit -m "feat: add 4 GET endpoints for on-demand request JSONB fields"
```

---

### 任务 4：static/json-viewer.html — 创建独立 JSON 查看器

**文件：**
- 创建：`static/json-viewer.html`

- [x] **步骤 1：创建文件**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JSON 查看器</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
            background: #1a1a1a;
            color: #a9b7c6;
            padding: 20px;
            font-size: 13px;
            line-height: 1.6;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .loading {
            color: #6b6b6b;
            font-style: italic;
        }
        .error {
            color: #e11d48;
            background: #fff1f2;
            padding: 16px;
            border-radius: 8px;
            font-size: 14px;
        }
        .key { color: #9876aa; }
        .string { color: #6a8759; }
        .number { color: #6897bb; }
        .boolean { color: #cc7832; }
        .null { color: #808080; }
    </style>
</head>
<body>
    <div class="loading">加载中...</div>
    <script>
        function escapeHtml(s) {
            const d = document.createElement('div');
            d.textContent = s;
            return d.innerHTML;
        }

        function syntaxHighlight(json) {
            if (json === null || json === undefined) return '<span class="null">null</span>';
            const str = JSON.stringify(json, null, 2);
            return str.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, function(match) {
                let cls = 'number';
                if (/^"/.test(match)) {
                    if (/:$/.test(match)) {
                        cls = 'key';
                    } else {
                        cls = 'string';
                    }
                } else if (/true|false/.test(match)) {
                    cls = 'boolean';
                } else if (/null/.test(match)) {
                    cls = 'null';
                }
                return '<span class="' + cls + '">' + escapeHtml(match) + '</span>';
            });
        }

        (async function() {
            const params = new URLSearchParams(window.location.search);
            const url = params.get('url');
            const title = params.get('title') || 'JSON 查看器';
            document.title = title;

            if (!url) {
                document.body.innerHTML = '<div class="error">缺少 url 参数</div>';
                return;
            }

            try {
                const resp = await fetch(url);
                if (!resp.ok) {
                    document.body.innerHTML = '<div class="error">请求失败: ' + resp.status + ' ' + escapeHtml(await resp.text()) + '</div>';
                    return;
                }
                const result = await resp.json();
                document.body.innerHTML = syntaxHighlight(result.data);
            } catch (e) {
                document.body.innerHTML = '<div class="error">加载失败: ' + escapeHtml(e.message) + '</div>';
            }
        })();
    </script>
</body>
</html>
```

- [x] **步骤 2：手动验证**

启动服务后访问 `/static/json-viewer.html?url=/admin/requests/1/request-headers&title=Request%20Headers`，确认页面能正常显示 JSON。

- [x] **步骤 3：Commit**

```bash
git add static/json-viewer.html
git commit -m "feat: add standalone JSON viewer page"
```

---

### 任务 5：static/index.html — 修改详情弹窗和新增链接函数

**文件：**
- 修改：`static/index.html:1224-1258`（`openRequestDetail` 函数）

- [x] **步骤 1：修改 openRequestDetail 函数**

将 `static/index.html` 第 1246-1249 行的 Headers 展示区块：

```html
                <div class="mt-3">
                    <div class="text-ink-400 mb-1">Headers:</div>
                    <pre class="bg-ink-900 rounded-xl p-3 text-xs text-emerald-400 font-mono overflow-x-auto">${esc(JSON.stringify(req.headers || {}, null, 2))}</pre>
                </div>
```

替换为：

```html
                <div class="mt-3">
                    <div class="text-ink-400 mb-2">请求/返回数据:</div>
                    <div class="flex flex-wrap gap-2">
                        <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'request-headers')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">请求 Header</a>
                        <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'request-body')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">请求 Body</a>
                        <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'response-headers')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">返回 Header</a>
                        <a href="javascript:void(0)" onclick="openJsonInNewTab(${req.id}, 'response-body')" class="pill pill-brand hover:opacity-80 transition cursor-pointer">返回 Body</a>
                    </div>
                </div>
```

- [x] **步骤 2：新增 openJsonInNewTab 函数**

在 `static/index.html` 的 `<script>` 区域中，`openRequestDetail` 函数之前，新增：

```javascript
        function openJsonInNewTab(requestId, field) {
            const url = '/static/json-viewer.html?url=' + encodeURIComponent('/admin/requests/' + requestId + '/' + field) + '&title=' + encodeURIComponent(field);
            window.open(url, '_blank');
        }
```

- [x] **步骤 3：手动验证**

启动服务，进入请求记录标签页，点击某条记录查看详情，确认 4 个链接正常显示。点击每个链接确认新标签页能正确展示 JSON 数据。

- [x] **步骤 4：Commit**

```bash
git add static/index.html
git commit -m "feat: update request detail modal with 4 JSON field links"
```

---

### 任务 6：最终验证

- [x] **步骤 1：运行全部测试**

运行：`cd /d h:\temp\temp\llm-plug && .venv\Scripts\python -m pytest tests/ -v`
预期：全部 PASS

- [x] **步骤 2：端到端手动验证**

1. 启动服务
2. 访问 `#requests` 标签页
3. 点击某条请求记录查看详情弹窗
4. 确认弹窗显示 4 个链接（请求Header、请求Body、返回Header、返回Body）
5. 逐一点击，确认新标签页正确展示 JSON
6. 确认列表页加载速度正常（无大 JSONB 传输）