# spec-routers — 路由层

> 对应目录：`routers/`（8 个文件）

## 模块定位

路由层负责 HTTP 请求的接入、鉴权和错误处理，是客户端与代理核心之间的桥梁。它不包含业务逻辑，核心处理全部委托给 `proxy_core.py`。

## 架构设计

三个代理端点使用**工厂模式**，通过 `proxy_base.py` 的 `make_proxy_router()` 统一生成，避免重复代码：

```python
# proxy_chat.py
router = make_proxy_router("/v1/chat/completions", APIType.OPENAI_CHAT)

# proxy_response.py
router = make_proxy_router("/v1/responses", APIType.OPENAI_RESPONSE)

# proxy_anthropic.py
router = make_proxy_router("/v1/messages", APIType.ANTHROPIC)
```

## proxy_base.py — 代理路由工厂

### `make_proxy_router(path, api_type, tags)`

```python
def make_proxy_router(path: str, api_type: APIType, tags: list[str] | None = None) -> APIRouter
```

**生成一个包含单个 POST 端点的 APIRouter**，处理器流程：

1. **鉴权**：`check_proxy_authorization(authorization)` → 不通过则返回 401
2. **解析请求体**：`await request.json()` → 失败则返回 400
3. **提取参数**：`model = body.get("model")`、`is_stream = body.get("stream")`
4. **调用核心**：`await proxy_request(model, body, api_type, is_stream, query_string)`
5. **返回响应**：
   - 流式 → `StreamingResponse(result, media_type="text/event-stream")`
   - 非流式 → 直接返回 JSON

**流式响应额外 Headers**：

```python
{
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",   # 防止 Nginx 缓冲 SSE
}
```

## auth.py — 鉴权

### `check_proxy_authorization(authorization)`

```python
def check_proxy_authorization(authorization: str | None) -> bool
```

**逻辑**：
- `PROXY_API_KEY` 为空 → 不鉴权，返回 True
- `authorization` 为空 → 返回 False
- 格式需为 `Bearer <token>`，token 需与 `PROXY_API_KEY` 完全匹配

> 注意：管理接口（`/admin/*`）无需认证，可直接访问。

## proxy_errors.py — 错误响应

所有错误响应使用 OpenAI 风格的 JSON 格式：

```json
{
  "error": {
    "message": "错误描述",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

### 错误函数

| 函数 | HTTP 状态码 | 说明 |
|------|-------------|------|
| `unauthorized()` | 401 | API Key 无效 |
| `invalid_request(message)` | 400 | 请求参数错误 |
| `bad_gateway(message)` | 502 | 上游错误 |
| `gateway_timeout(message)` | 504 | 上游超时 |
| `response_from_proxy_exception(exc)` | 502/504 | 根据 httpx 异常类型自动映射 |

**异常映射逻辑**（`response_from_proxy_exception`）：

| 异常类型 | HTTP 状态码 | 说明 |
|----------|-------------|------|
| `httpx.HTTPStatusError` | 502 | 上游返回非 2xx（截断 body 至 800 字符） |
| `httpx.TimeoutException` | 504 | 上游超时 |
| `httpx.RequestError` | 502 | 网络错误 |
| 其他 | 502 | 兜底 |

## proxy_models.py — 模型列表

### `GET /v1/models`（OpenAI 风格）

```python
async def list_models_openai(authorization)
```

- 鉴权后，从所有已启用渠道中聚合模型列表（去重）
- 返回格式：

```json
{
  "object": "list",
  "data": [
    {"id": "gpt-4", "object": "model", "created": 0, "owned_by": "proxy"}
  ]
}
```

### `GET /v1/anthropic/models`（Anthropic 风格）

```python
async def list_models_anthropic(authorization, limit, before, after)
```

- 鉴权后，优先返回 `api_type=anthropic` 的模型；无则返回全部
- 支持 `limit`（1-100）、`before`、`after` 分页参数
- 返回格式：

```json
{
  "data": [{"id": "claude-3", "type": "model", "display_name": "claude-3", "created_at": ""}],
  "has_more": false,
  "first_id": "claude-3",
  "last_id": "claude-3"
}
```

## admin.py — 管理接口

### 渠道 CRUD

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin/channels` | GET | 获取所有渠道（API Key 脱敏，只显示前 4 位 + `***`） |
| `/admin/channels` | POST | 添加渠道（自动生成 `ch_xxxxxxxx` 格式的 ID） |
| `/admin/channels/{id}` | PUT | 更新渠道（仅更新传入的字段，`exclude_unset=True`） |
| `/admin/channels/{id}` | DELETE | 删除渠道 |
| `/admin/channels/{id}/toggle` | PATCH | 切换 enabled/disabled |

**线程安全**：所有写操作使用 `with get_lock():` 包裹，确保不会并发写入。

**客户端缓存刷新**：更新、删除、切换渠道时调用 `remove_channel_client(ch)` 刷新 HTTP 客户端缓存。

### 渠道连通性测试

`POST /admin/channels/{id}/test?model=xxx`

- 根据 `api_type` 构造最简请求（`messages: [{"role": "user", "content": "Hi"}]`, `max_tokens: 5`）
- 使用独立 httpx 客户端（非缓存），30 秒超时
- 返回：

```json
{
  "success": true,
  "message": "测试通过",
  "model": "gpt-4",
  "latency_ms": 1234,
  "reply": "Hello! How"
}
```

### 日志查看

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin/logs` | GET | 列出所有 JSONL 日志文件（按时间倒序） |
| `/admin/logs/{filename}` | GET | 返回指定日志文件内容（防路径遍历） |

## 文件一览

| 文件 | 行数 | 核心内容 |
|------|------|----------|
| `proxy_base.py` | ~50 | `make_proxy_router()` 工厂函数 |
| `proxy_chat.py` | ~4 | 一行调用工厂 |
| `proxy_response.py` | ~4 | 一行调用工厂 |
| `proxy_anthropic.py` | ~4 | 一行调用工厂 |
| `proxy_models.py` | ~97 | 两个模型列表端点 |
| `admin.py` | ~224 | 渠道 CRUD + 测试 + 日志 |
| `auth.py` | ~16 | Bearer Token 鉴权 |
| `proxy_errors.py` | ~59 | 错误响应构建 |

## 注意事项

1. **管理接口无鉴权保护**：管理接口（`/admin/*`）无需认证即可访问，适用于本地部署场景。
2. **GET /admin/channels 的 API Key 脱敏**：列表接口只返回 `sk-x***`，但单个渠道的 PUT/POST 响应中包含完整 API Key。
3. **path traversal 防护**：日志文件读取使用 `is_relative_to()` 防止路径遍历攻击。
4. **query_string 透传**：`proxy_base.py` 会将原始请求的 query string 透传给上游，这对 Anthropic 的 beta 参数等场景很重要。
