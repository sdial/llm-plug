# 模块详解

本文档详细介绍 LLM-Plug 各模块的实现细节，面向开发者。

## 目录

- [配置管理 (config.py)](#配置管理)
- [数据模型 (models/)](#数据模型)
- [存储层 (storage.py)](#存储层)
- [HTTP 客户端 (client.py)](#http-客户端)
- [路由层 (routers/)](#路由层)
- [代理核心 (proxy_core.py)](#代理核心)
- [转换器 (converters/)](#转换器)
- [负载均衡器 (balancer/)](#负载均衡器)

---

## 配置管理

> 对应文件：`config.py`（约 50 行）

### 模块定位

`config.py` 是项目配置的**单一来源**（Single Source of Truth），所有配置项通过环境变量读取，模块加载时即确定值。

### 配置项详解

#### 服务器配置

| 变量 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `HOST` | `HOST` | `"0.0.0.0"` | 监听地址 |
| `PORT` | `PORT` | `8000` | 监听端口 |

#### 数据存储

| 变量 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `DATA_DIR` | `DATA_DIR` | 项目根目录/data | 数据存储目录 |
| `CHANNELS_FILE` | `CHANNELS_FILE` | `DATA_DIR/channels.json` | 渠道配置文件 |
| `API_KEYS_FILE` | `API_KEYS_FILE` | `DATA_DIR/api_keys.json` | API Key 配置文件 |

#### 负载均衡

| 变量 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `MAX_FAIL_COUNT` | `MAX_FAIL_COUNT` | `5` | 连续失败 N 次后标记渠道不健康 |
| `COOLDOWN_SECONDS` | `COOLDOWN_SECONDS` | `60` | 不健康渠道冷却恢复时间（秒） |

#### 请求超时

| 变量 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `REQUEST_TIMEOUT` | `REQUEST_TIMEOUT` | `300` | 上游请求超时时间（秒） |

#### 鉴权

| 变量 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `PROXY_API_KEY` | `PROXY_API_KEY` | `""` (空) | 代理 API 密钥，空则不鉴权 |

#### 调试

| 变量 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `DEBUG` | `DEBUG` | `false` | 调试模式开关 |
| `DEBUG_LOG_DIR` | `DEBUG_LOG_DIR` | 项目根目录/logs | 调试日志目录 |

#### PostgreSQL 统计

| 变量 | 环境变量 | 默认值 | 说明 |
|------|----------|--------|------|
| `DATABASE_URL` | `DATABASE_URL` | `postgresql://localhost:5432/llmplug` | PostgreSQL 连接 URL |
| `STATS_TRACKED_HEADERS` | `STATS_TRACKED_HEADERS` | (空) | 统计追踪的请求头 |

### 注意事项

1. **不支持热加载**：修改环境变量后需重启服务
2. **PORT 是 int 转换**：如果环境变量不是数字会抛出 `ValueError`
3. **DEBUG 日志可能很大**：高流量下磁盘空间消耗快，建议仅调试时开启

---

## 数据模型

> 对应目录：`models/`

### APIType 枚举

```python
class APIType(str, Enum):
    OPENAI_CHAT = "openai-chat-completions"
    OPENAI_RESPONSE = "openai-response"
    ANTHROPIC = "anthropic"
```

继承 `str`，可直接当字符串使用。

### Channel 模型

```python
class Channel(BaseModel):
    id: str                           # ch_{uuid4_hex[:8]}
    name: str                         # 渠道名称
    api_type: APIType                 # 上游 API 格式
    base_url: str                     # 上游 API 地址
    api_key: str                      # 上游 API 密钥
    models: list[str]                 # 支持的模型列表
    enabled: bool = True              # 是否启用
    weight: int = 1                   # 负载均衡权重
    priority: int = 1                 # 优先级（数字越小越优先）
    socks5_proxy: str | None = None   # SOCKS5 代理地址
    created_at: str                   # 创建时间
```

### ApiKey 模型

```python
class ApiKey(BaseModel):
    id: str                           # key_{uuid4_hex[:8]}
    name: str                         # API Key 名称
    key: str                          # llmplug-api-{random}
    allowed_models: list[str] = []    # 允许访问的模型
    notes: str = ""                   # 备注
    request_count: int = 0            # 累计请求次数
    total_input_tokens: int = 0       # 累计输入 token
    total_output_tokens: int = 0      # 累计输出 token
    created_at: str                   # 创建时间
```

### ModelGroup 模型

```python
class ModelGroup(BaseModel):
    id: str                           # mg_{uuid4_hex[:8]}
    name: str                         # 模型组名称（用于请求中的 model 字段）
    models: list[str]                 # 包含的模型列表
    fallback_order: list[str] = []    # Fallback 顺序
    enabled: bool = True              # 是否启用
    created_at: str                   # 创建时间
```

---

## 存储层

> 对应文件：`storage.py`（约 305 行）

### 模块定位

负责数据持久化存储，使用 JSON 文件作为存储介质，提供异步安全的读写接口和内存缓存。

### 核心函数

#### `load_data() -> dict`

异步读取数据，优先从内存缓存读取，缓存 TTL 为 5 秒。

```python
async def load_data() -> dict[str, Any]
```

#### `save_data(data) -> None`

异步写入数据，使用原子写入确保数据安全。

```python
async def save_data(data: dict[str, Any]) -> None
```

**流程**：
1. 确保数据目录存在
2. 获取异步锁
3. 在线程池中执行磁盘 IO（临时文件 → 写入 → fsync → 原子替换）
4. 立即更新内存缓存
5. 触发保存回调

#### `load_api_keys() -> dict`

异步读取 API Keys 数据，使用相同的缓存机制。

#### `load_model_groups() -> list[ModelGroup]`

异步读取模型组列表。

#### `get_lb_config() -> LBConfig`

异步读取负载均衡配置。

### 缓存机制

```python
_cache: dict | None = None    # 内存缓存
_cache_ts: float = 0          # 缓存时间戳
_CACHE_TTL = 5.0              # 缓存有效期（秒）
```

### 为什么用 asyncio.Lock？

从 `threading.RLock` 迁移到 `asyncio.Lock`：
- 异步函数不会在同一线程上嵌套获取锁
- 懒加载创建锁，确保绑定到正确的事件循环

### 重要设计决策

1. **save_data() 后立即更新缓存**：写后读一致性保证
2. **不能直接修改 channels.json**：必须通过 `save_data()`
3. **缓存 TTL 是 5 秒**：权衡 IO 压力和变更感知延迟

---

## HTTP 客户端

> 对应文件：`client.py`（约 162 行）

### 模块定位

管理与上游 LLM API 的 HTTP 连接，区分普通请求和流式请求两类场景。

### 全局缓存

```python
_clients: dict[str, httpx.AsyncClient] = {}      # 普通请求客户端池
_stream_clients: dict[str, httpx.AsyncClient] = {} # 流式请求客户端池
_lock = asyncio.Lock()                           # 并发安全锁
```

**缓存键**：`f"{channel.base_url}|{channel.socks5_proxy or ''}"`

### 核心函数

#### `create_client(channel) -> httpx.AsyncClient`

创建或获取缓存的客户端，用于非流式请求。

```python
async def create_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient
```

#### `get_or_create_stream_client(channel) -> httpx.AsyncClient`

创建或获取缓存的流式客户端，是 proxy_core 获取流式客户端的主要方式。

```python
async def get_or_create_stream_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient
```

#### `create_stream_client(channel) -> httpx.AsyncClient`

创建独立的流式客户端，每次调用都新建，不缓存。

```python
def create_stream_client(channel: Channel) -> httpx.AsyncClient
```

#### `get_upstream_headers(channel) -> dict`

构建上游 API 认证头。

| 渠道类型 | 认证方式 |
|----------|----------|
| Anthropic | `x-api-key: {api_key}` + `anthropic-version` |
| OpenAI 系列 | `Authorization: Bearer {api_key}` |

### 三种客户端对比

| 特性 | 普通客户端 | 缓存流式客户端 | 独立流式客户端 |
|------|------|------|------|
| 创建方式 | 缓存复用 | 缓存复用 | 每次新建 |
| 关闭方式 | `close_all_clients()` | `close_all_clients()` | 手动 `aclose()` |
| 可用 async with | 不可 | 不可 | 可以 |

### 注意事项

1. **不要用 async with 包裹缓存的客户端**：会关闭共享连接
2. **独立流式客户端必须手动关闭**：在 `finally` 块中调用 `aclose()`

---

## 路由层

> 对应目录：`routers/`（8 个文件，约 520 行）

### 模块定位

负责 HTTP 请求的接入、鉴权和错误处理，核心处理委托给 `proxy_core.py`。

### 架构设计

三个代理端点使用**工厂模式**：

```python
# proxy_chat.py
router = make_proxy_router("/v1/chat/completions", APIType.OPENAI_CHAT)

# proxy_response.py
router = make_proxy_router("/v1/responses", APIType.OPENAI_RESPONSE)

# proxy_anthropic.py
router = make_proxy_router("/v1/messages", APIType.ANTHROPIC)
```

### proxy_base.py — 代理路由工厂

```python
def make_proxy_router(path: str, api_type: APIType) -> APIRouter
```

处理器流程：
1. 鉴权 → 401
2. 解析请求体 → 400
3. 提取 model/stream
4. 调用 `proxy_request()`
5. 返回响应（流式/非流式）

### auth.py — 鉴权

```python
def check_proxy_authorization(authorization: str | None) -> bool
```

- `PROXY_API_KEY` 为空 → 不鉴权
- 格式需为 `Bearer <token>`

### proxy_errors.py — 错误响应

所有错误使用 OpenAI 风格：

```json
{
  "error": {
    "message": "错误描述",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

### admin.py — 管理接口

| 端点 | 说明 |
|------|------|
| `/admin/channels` | 渠道 CRUD |
| `/admin/channels/{id}/test` | 测试连通性 |
| `/admin/api-keys` | API Key CRUD |
| `/admin/stats` | 统计数据 |
| `/admin/requests` | 请求记录查询 |

---

## 代理核心

> 对应文件：`proxy_core.py`（约 950 行）

### 模块定位

整个代理服务的**调度中心**，协调路由层、转换器、负载均衡器、HTTP 客户端、存储层和统计模块。

### 核心函数

#### `proxy_request(model, request_data, target_api_type, is_stream, ...)`

主入口，由路由层调用。

```python
async def proxy_request(
    model: str,
    request_data: dict,
    target_api_type: APIType,
    is_stream: bool = False,
    ...
) -> tuple[Any, Channel]
```

**流程**：
1. 检查模型组，若有则按 fallback 顺序尝试
2. 获取匹配的已启用渠道
3. 进入故障转移循环：选择渠道 → 执行请求 → 成功返回 / 失败重试

#### `_do_request(channel, request_data, ...)`

单次请求执行，包含格式转换 + 发送流程。

```python
async def _do_request(channel, request_data, target_api_type, is_stream, ...)
```

**流程**：
1. 获取转换器和上游类型
2. 转换请求体
3. 构建上游 URL 和请求头
4. 发送请求（流式/非流式）
5. 转换响应
6. 记录成功/统计

#### `_do_stream_request(...)`

流式请求处理，异步生成器，逐行解析上游 SSE 并转换。

### 辅助函数

| 函数 | 说明 |
|------|------|
| `_get_channels_for_model(model)` | 筛选匹配模型的已启用渠道 |
| `_get_converter_and_upstream_type(channel, target_api_type)` | 获取转换器 |
| `_get_upstream_url(channel)` | 拼接上游 URL |
| `_log_debug(...)` | 调试日志记录 |

### 注意事项

1. **非流式请求使用缓存客户端**：不能 `async with` 关闭
2. **流式请求使用缓存流式客户端**：同样不能手动关闭
3. **故障转移循环**：`all_tried` 集合保证同一渠道不会重试
4. **流式中途错误**：通过 SSE 错误事件通知客户端

---

## 转换器

> 对应目录：`converters/`（4 个文件，约 1700 行）

### 模块定位

负责三种 LLM API 格式之间的请求/响应转换。

### BaseConverter 抽象基类

```python
class BaseConverter(ABC):
    @abstractmethod
    def convert_request(self, source_data: dict, source_type: str) -> dict:
        """将入口请求体转为上游 API 所需 JSON"""

    @abstractmethod
    def convert_response(self, target_response: dict, source_type: str) -> dict:
        """将上游非流式 JSON 转为入口格式"""

    @abstractmethod
    def convert_stream_chunk(self, chunk: dict, source_type: str) -> dict | None:
        """将上游 SSE 单条 JSON 转为入口格式"""
```

### 三种 API 格式对比

#### OpenAI Chat Completions

```json
{
  "model": "gpt-4",
  "messages": [{"role": "user", "content": "Hello"}],
  "tools": [{"type": "function", "function": {"name": "get_weather"}}]
}
```

#### OpenAI Response

```json
{
  "model": "gpt-4",
  "instructions": "You are helpful.",
  "input": [{"role": "user", "content": "Hello"}],
  "tools": [{"type": "function", "name": "get_weather"}]
}
```

#### Anthropic Messages

```json
{
  "model": "claude-3",
  "system": "You are helpful.",
  "messages": [{"role": "user", "content": "Hello"}],
  "tools": [{"name": "get_weather", "input_schema": {}}]
}
```

### 转换矩阵

| 客户端格式 \ 上游格式 | openai-chat | openai-response | anthropic |
|---|---|---|---|
| **openai-chat** | 直通 | `ToResponseConverter` | `ToAnthropicConverter` |
| **openai-response** | `ToChatCompletionsConverter` | 直通 | `ToAnthropicConverter` |
| **anthropic** | `ToChatCompletionsConverter` | `ToResponseConverter` | 直通 |

### 关键字段映射

**Anthropic → Chat**：

| Anthropic | Chat Completions |
|-----------|------------------|
| `system` | `messages[role=system]` |
| `content[].type=thinking` | `message.reasoning_content` |
| `content[].type=tool_use` | `message.tool_calls[]` |
| `stop_reason: end_turn` | `finish_reason: stop` |

**Chat → Anthropic**：

| Chat Completions | Anthropic |
|------------------|-----------|
| `messages[role=system]` | `system` 字段 |
| `message.tool_calls[]` | `content[type=tool_use]` |
| `messages[role=tool]` | `content[type=tool_result]` |

### 流式状态机

每个 Converter 实例维护 `_stream_state`，一个实例只服务一次流式请求，不可复用。

### 注意事项

1. **Tool 消息合并**：Anthropic 要求 `tool_result` 放在 `user` 角色消息中
2. **流式转换返回列表**：一个上游 chunk 可能产生多个下游事件
3. **Thinking/Reasoning 支持**：支持 OpenAI 的 `reasoning_content` 和 Anthropic 的 `thinking` 互转

---

## 负载均衡器

> 对应文件：`balancer/load_balancer.py`（约 119 行）

### 模块定位

从多个可用渠道中选择一个来处理请求，实现优先级分组 + 加权轮询 + 健康检查。

### ChannelHealth 类

```python
class ChannelHealth:
    fail_count: int = 0        # 连续失败次数
    last_fail_time: float = 0  # 上次失败时间戳
    current_weight: int = 0    # 当前轮询权重（SWRR 内部状态）
```

### LoadBalancer 核心方法

#### `select_channel(channels, exclude_ids)`

```python
async def select_channel(
    channels: list[Channel],
    exclude_ids: set[str] | None = None,
) -> Optional[Channel]
```

**流程**：
1. 读取负载均衡配置
2. 过滤：排除 disabled、已试、不健康的渠道
3. 分组：按 `priority` 升序排序，取最小 priority 组
4. 选择：组内加权轮询

#### `_weighted_round_robin(channels)`

**平滑加权轮询算法**（SWRR）：

```
1. 每个 channel 的 current_weight += weight
2. 选择 current_weight 最大的 channel
3. 被选中 channel 的 current_weight -= total_weight
```

### 配置项

| 配置字段 | 默认值 | 说明 |
|----------|--------|------|
| `lb_config.max_fail_count` | 5 | 连续失败 N 次后标记不健康 |
| `lb_config.cooldown_seconds` | 60 | 冷却恢复时间（秒） |

### 线程安全

使用 `asyncio.Lock`，整个选择过程在锁内完成。

### 注意事项

1. **priority 数字越小优先级越高**：1 比 2 优先
2. **冷却期只是恢复探测**：冷却过后允许再试一次
3. **current_weight 不重置**：保证长时间运行的流量分配比例精确
