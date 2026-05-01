# 请求记录标签页适配数据库变更 — 设计方案

## 背景

PostgreSQL `requests` 表结构发生变更：
1. `headers` 列重命名为 `request_headers`
2. 新增 `response_headers` (JSONB)
3. 新增 `request_body` (JSONB)
4. 新增 `response_body` (JSONB)

前端 `#requests` 标签页未适配这些变更：
- 详情弹窗仍引用旧字段 `req.headers`
- 新增的 `request_body`、`response_body`、`response_headers` 未展示

## 需求

| 项目 | 说明 |
|------|------|
| 范围 | 仅更新请求记录标签页（列表+详情弹窗），统计标签页不动 |
| 展示方式 | 详情弹窗中 4 个链接，点击在新标签页打开独立 JSON 查看器 |
| JSON 查看器 | 独立静态 HTML 文件，深色背景等宽字体，第一步纯文本展示 |
| 性能 | 列表查询不再传输 4 个大 JSONB 字段，按需从独立端点获取 |

## 方案

采用 4 个独立 API 端点 + 独立 JSON 查看器页面。

### 后端

#### 新增 4 个 API 端点

```
GET /admin/requests/{id}/request-headers   → { "data": <json> | null }
GET /admin/requests/{id}/request-body       → { "data": <json> | null }
GET /admin/requests/{id}/response-headers   → { "data": <json> | null }
GET /admin/requests/{id}/response-body      → { "data": <json> | null }
```

- 每个端点只 SELECT 单个 JSONB 字段
- 字段为 NULL 时返回 `{ "data": null }`
- 记录不存在时返回 404

#### 新增 stats.py 查询函数

```python
async def get_request_field(request_id: int, field: str) -> dict | None:
    """查询单个请求的单个字段"""
```

URL 路径使用连字符（`request-headers`），SQL 列名使用下划线（`request_headers`）。内部维护允许的 field 映射：

| URL 路径字段 | SQL 列名 |
|-------------|---------|
| `request-headers` | `request_headers` |
| `request-body` | `request_body` |
| `response-headers` | `response_headers` |
| `response-body` | `response_body` |

不在映射中的 field 返回 400 错误，防止 SQL 注入。

#### 优化 list_requests 查询

`list_requests()` 的 SELECT 移除 `request_headers, response_headers, request_body, response_body`，列表查询不再传输大 JSONB 字段。

### 前端

#### 详情弹窗修改

移除当前 `req.headers` 展示区块，替换为 4 个链接按钮：

```
请求 Header | 请求 Body | 返回 Header | 返回 Body
```

点击任一链接调用 `openJsonInNewTab(requestId, field)`。

#### 新增 openJsonInNewTab 函数

```javascript
function openJsonInNewTab(requestId, field) {
    const url = `/static/json-viewer.html?url=/admin/requests/${requestId}/${field}&title=${field}`;
    window.open(url, '_blank');
}
```

#### 新文件：static/json-viewer.html

独立 JSON 查看器页面：
- 接收 URL 参数：`url`（API 地址）、`title`（页面标题）
- 页面加载时 fetch 指定 URL 获取 JSON 数据
- 用 `JSON.stringify(data, null, 2)` 美化展示
- 深色背景 `#1a1a1a`，等宽字体 `monospace`，浅色文字
- `white-space: pre-wrap` 自动换行
- HTML 转义防止 XSS
- 加载中/加载失败状态提示

## 文件改动汇总

| 文件 | 改动 |
|------|------|
| `stats.py` | 新增 `get_request_field()` 函数；`list_requests()` SELECT 移除 4 个大 JSONB 字段 |
| `routers/admin.py` | 新增 4 个 GET 端点 |
| `static/index.html` | 详情弹窗移除 `req.headers`，替换为 4 个链接；新增 `openJsonInNewTab()` |
| `static/json-viewer.html` | 新文件 — 独立 JSON 查看器页面 |

## 不需要修改的文件

- 统计标签页（聚合表结构无变化）
- `proxy_core.py`（`record_request()` 签名不变）
- 数据库迁移（`init_db()` 已有 `ADD COLUMN IF NOT EXISTS` 逻辑）