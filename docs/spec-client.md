# spec-client — HTTP 客户端管理

> 对应文件：`client.py`（约 103 行）

## 模块定位

`client.py` 负责管理与上游 LLM API 的 HTTP 连接，包括客户端的创建、缓存、复用和清理。核心设计是区分**普通请求**（可缓存复用）和**流式请求**（不可缓存）两种场景。

## 全局缓存

```python
_clients: dict[str, httpx.AsyncClient] = {}   # 缓存的客户端池
_cache_ts: dict[str, float] = {}               # 每个客户端的最后使用时间
```

**缓存键**：`f"{channel.base_url}|{channel.socks5_proxy or ''}"`

同一 base_url + 同一 proxy 的渠道共享同一个客户端连接。

## 核心函数

### `create_client(channel, timeout) -> httpx.AsyncClient`

**创建或获取缓存的客户端**，用于非流式请求。

```python
def create_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient
```

**流程**：

1. 计算缓存键 `_cache_key(channel)`
2. 查找缓存：如果存在且未关闭 → 更新时间戳，直接返回
3. 创建新客户端：
   - 有 `socks5_proxy` → `httpx.AsyncClient(proxy=proxy, ...)`
   - 无代理 → `httpx.AsyncClient(...)`
4. 存入缓存，记录时间戳
5. 返回客户端

**超时配置**：`httpx.Timeout(timeout, connect=10.0)`，连接超时固定 10 秒。

> ⚠️ **重要**：返回的客户端是**共享的**，调用方**不能**使用 `async with` 包裹，否则会关闭共享连接。客户端的生命周期由 `close_all_clients()` 和 `cleanup_stale_clients()` 管理。

### `create_stream_client(channel) -> httpx.AsyncClient`

**创建独立的流式客户端**，每次调用都新建。

```python
def create_stream_client(channel: Channel) -> httpx.AsyncClient
```

- 超时配置：`httpx.Timeout(timeout, connect=10.0, read=timeout)`
- **不存入缓存**
- 调用方需在使用完后手动 `await client.aclose()`

> ⚠️ **重要**：流式客户端**绝不能**加入 `_clients` 缓存池。因为流式请求的响应是异步生成器，在 `proxy_core.py` 的 `finally` 块中关闭客户端。如果缓存了，其他请求可能复用这个客户端，导致连接被提前关闭。

### `get_upstream_headers(channel, extra_headers) -> dict`

**构建上游 API 认证头**。

```python
def get_upstream_headers(channel: Channel, extra_headers: dict | None = None) -> dict
```

| 渠道类型 | 认证方式 |
|----------|----------|
| Anthropic | `x-api-key: {api_key}` + `anthropic-version: 2023-06-01` + `anthropic-beta: ...` |
| OpenAI 系列 | `Authorization: Bearer {api_key}` |

**Anthropic 额外 Headers**：
- `anthropic-version: 2023-06-01` — API 版本
- `anthropic-beta: prompt-caching-2024-07-31,interleaved-thinking-2025-05-14` — Beta 功能标志

### `close_all_clients()`

**关闭并清空所有缓存的客户端**。在 FastAPI lifespan 的 shutdown 阶段调用。

```python
async def close_all_clients():
```

### `cleanup_stale_clients(max_age=300.0)`

**清理超过 max_age 秒未使用的客户端**。

```python
async def cleanup_stale_clients(max_age: float = 300.0):
```

- 默认 5 分钟未使用的客户端会被关闭并移除
- 目前未在定时任务中调用，作为工具方法预留

### `remove_channel_client(channel)`

**移除指定渠道的客户端缓存**。在渠道配置变更时调用（admin 路由的 PUT/DELETE/TOGGLE 操作）。

```python
def remove_channel_client(channel: Channel):
```

- 从 `_clients` 和 `_cache_ts` 中移除
- 尝试异步关闭客户端（`asyncio.get_event_loop().create_task(client.aclose())`）
- 如果不在异步上下文中则忽略（依赖 `cleanup_stale_clients()` 兜底）

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `REQUEST_TIMEOUT` | 300 | 上游请求超时时间（秒） |

## 两种客户端对比

| 特性 | 普通客户端 (`create_client`) | 流式客户端 (`create_stream_client`) |
|------|------|------|
| 创建方式 | 缓存复用 | 每次新建 |
| 缓存 | ✅ 存入 `_clients` | ❌ 不缓存 |
| 关闭方式 | `close_all_clients()` | 手动 `await client.aclose()` |
| 超时 | `timeout, connect=10.0` | `timeout, connect=10.0, read=timeout` |
| 使用场景 | 非流式 POST 请求 | 流式 POST 请求 |
| 可用 async with | ❌ 不可（会关闭共享连接） | ✅ 可以（但 proxy_core 手动管理） |

## 连接池设计

```
_clients 缓存池:
  ┌────────────────────────────────────────────┐
  │  "https://api.openai.com|"     → Client A  │ ← 无代理
  │  "https://api.openai.com|socks5://..." → Client B  │ ← 有代理
  │  "https://api.anthropic.com|"  → Client C  │
  └────────────────────────────────────────────┘

流式请求: 每次新建独立客户端，用完即关
  Client D (stream) → aclose()
  Client E (stream) → aclose()
  ...
```

## SOCKS5 代理支持

所有客户端都支持 SOCKS5 代理，通过 `channel.socks5_proxy` 字段配置。httpx 的 `proxy` 参数接受标准代理 URL：

```
socks5://user:pass@host:port
socks5h://user:pass@host:port   # DNS 也走代理
```

底层依赖 `python-socks[asyncio]` 包（通过 `httpx[socks]` 引入）。

## 注意事项

1. **不要用 async with 包裹缓存的客户端**：`create_client()` 返回的是共享客户端，`async with` 会在退出时关闭连接，导致后续请求失败。
2. **流式客户端必须手动关闭**：`proxy_core.py` 在 `_do_stream_request()` 的 `finally` 块中调用 `await client.aclose()`。
3. **remove_channel_client 是同步函数**：它无法 `await` 关闭客户端，只能尝试 `create_task()` 安排关闭。如果失败，连接会泄漏直到进程退出或 `cleanup_stale_clients()` 清理。
4. **缓存键只包含 base_url 和 proxy**：相同 base_url + proxy 的不同渠道共享连接，即使 api_key 不同。这是合理的，因为连接是 HTTP/2 多路复用的，认证信息在请求头中传递，不在连接级别。
