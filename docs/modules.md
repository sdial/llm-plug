# 模块详解

本文档详细介绍 LLM-Plug 各模块的实现细节，面向开发者日常维护和二次开发参考。

## 目录

- [配置管理 (config.py)](#配置管理)
- [数据模型 (models/)](#数据模型)
- [存储层 (storage.py)](#存储层)
- [HTTP 客户端 (client.py)](#http-客户端)
- [路由层 (routers/)](#路由层)
- [代理核心 (proxy_core.py / proxy/)](#代理核心)
- [转换器 (converters/)](#转换器)
- [负载均衡器 (balancer/)](#负载均衡器)
- [能力管理 (capability_manager.py)](#能力管理)
- [URL 构建 (url_builder.py)](#url-构建)
- [思考过滤 (think_filter.py)](#思考过滤)
- [IP 白名单 (whitelist.py)](#ip-白名单)
- [管理员鉴权 (admin_auth.py)](#管理员鉴权)
- [请求记录 (request_logs.py)](#请求记录)
- [Responses 状态 (response_state.py / state_store.py)](#responses-状态)

---

## 配置管理

> 对应文件：`config.py`

### 模块定位

`config.py` 是项目配置的**单一来源**（Single Source of Truth）。项目不读取 `.env`：启动常量使用内置默认值，业务配置从 `data/settings.json` 读取，前端「设置」页负责写入。

### 配置架构

配置分两层：

1. **模块级常量**：`HOST`、`PORT`、`CHANNELS_FILE`、`API_KEYS_FILE` 等，启动时从 `_CONFIG_SCHEMA` 默认值初始化，一般不热变
2. **业务设置**：通过 `_CONFIG_SCHEMA` 定义，运行时从 `data/settings.json` 读取，前端可修改

### _CONFIG_SCHEMA 完整配置项

| 键 | 类型 | 默认值 | 需重启 | 说明 |
|----|------|--------|--------|------|
| `host` | str | `"0.0.0.0"` | 是 | 监听地址（只读，前端不可改） |
| `port` | int | `55555` | 是 | 监听端口（只读，前端不可改） |
| `request_timeout` | int | `300` | 否 | 上游请求超时（秒），变更后自动重建连接池 |
| `max_body_size` | int | `10485760`（10MB） | 否 | 请求体最大体积 |
| `stats_sqlite_path` | str | `data/stats.db` | 否 | 统计 SQLite 路径 |
| `request_log_sqlite_path` | str | `data/request_logs.db` | 否 | 请求记录 SQLite 路径 |
| `save_request_headers` | bool | `false` | 否 | 是否保存请求头到请求记录 |
| `save_response_headers` | bool | `false` | 否 | 是否保存响应头到请求记录 |
| `save_request_body` | bool | `false` | 否 | 是否保存请求体到请求记录 |
| `save_response_body` | bool | `false` | 否 | 是否保存响应体到请求记录 |
| `save_files` | bool | `false` | 否 | 是否保存请求中的文件到 `logs/files/` |
| `save_images` | bool | `false` | 否 | 是否保存请求中的图片到 `logs/images/` |
| `save_audios` | bool | `false` | 否 | 是否保存请求中的音频到 `logs/audios/` |
| `max_log_body_size` | int | `65536`（64KB） | 否 | 请求记录 body 截断上限（字节），0=不截断 |
| `allow_format_conversion` | bool | `true` | 否 | 全局是否允许跨格式转换 |
| `max_fail_count` | int | `5` | 否 | 连续失败 N 次后标记渠道不健康 |
| `cooldown_seconds` | int | `60` | 否 | 不健康渠道冷却恢复时间（秒） |
| `response_state_max_entries` | int | `1000` | 否 | Responses 状态最大条目数 |
| `response_state_ttl_minutes` | int | `60` | 否 | Responses 状态过期时间（分钟） |
| `response_state_cleanup_interval_minutes` | int | `30` | 否 | Responses 状态清理间隔（分钟） |
| `aggregation_timezone` | str | `""` | 否 | 统计聚合时区（IANA 格式，空=UTC） |
| `request_log_retention_days` | int | `0` | 否 | 请求记录保留天数（0=不清理） |
| `request_log_raw_retention_days` | int | `0` | 否 | RAW 字段（headers/body）保留天数（0=不清理） |

> **注意**：`log_level` 不在 `_CONFIG_SCHEMA` 中，仅通过 `--log-level` CLI 参数设置（默认 `info`），前端设置页不可修改。

### 配置校验 (_CONFIG_CONSTRAINTS)

每个配置项可定义约束规则，在 `update_settings()` 时自动校验：

- `min` / `max`：数值范围
- `choices`：枚举值集合
- `validator`：自定义校验函数（如 `aggregation_timezone` 用 `iana_timezone` 校验是否为合法 IANA 时区名）

### 热更新机制

`update_settings(updates)` 是前端设置页的后端入口：

1. 加锁遍历更新项，跳过 `readonly` 字段
2. 类型转换 + 校验
3. 写入磁盘（原子写入：临时文件 + `os.replace()`）
4. 同步模块级变量（`_sync_module_vars()`）
5. 按变更项触发级联操作：
   - `request_timeout` → `client.invalidate_all_clients()`
   - `response_state_*` → `reload_responses_store()`
   - `max_fail_count` / `cooldown_seconds` → `load_balancer.update_config()`
6. 返回 `{updated: [...], needs_restart: bool}`

### 负载均衡配置迁移

启动时 `init_settings()` 会调用 `_migrate_lb_config()`，检查 `channels.json` 是否残留旧版 `lb_config` 字段，自动迁移到 `settings.json` 并删除旧字段。

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
    base_url: str                     # 上游 API 基础地址
    endpoint_url: str | None = None   # 可选，完整端点 URL（优先）
    models_url: str | None = None     # 可选，模型列表 URL
    api_key: str                      # 上游 API 密钥
    models: list[str]                 # 支持的模型列表
    enabled: bool = True              # 是否启用
    weight: int = 1                   # 负载均衡权重（ge=1）
    priority: int = 1                 # 优先级（ge=1，数字越小越优先）
    socks5_proxy: str | None = None   # SOCKS5 代理地址
    capabilities: dict | None = None  # 手动覆盖提供商能力
    model_capabilities: dict[str, ModelCapabilities] | None = None  # 按模型 ID 配置多模态能力
    anthropic_version: str | None = None
    anthropic_version_policy: AnthropicVersionPolicy = "channel"
    anthropic_beta: str | None = None
    anthropic_beta_policy: AnthropicBetaPolicy = "channel"
    allow_format_conversion: bool | None = None
    created_at: str                   # ISO 8601 时间
```

### ModelCapabilities 模型

```python
class ModelCapabilities(BaseModel):
    supports_image_content: bool = False  # 是否支持图片输入
    supports_audio_content: bool = False  # 是否支持音频输入
    supports_file_content: bool = False   # 是否支持文件输入
```

用于 `Channel.model_capabilities` 字典中的值，按模型 ID 配置多模态能力覆盖。

**Anthropic 策略枚举**：

```python
class AnthropicVersionPolicy(str, Enum):
    CHANNEL = "channel"              # 始终使用渠道版本
    CLIENT = "client"                # 优先客户端版本
    CHANNEL_IF_MISSING = "channel_if_missing"  # 客户端未传时用渠道版本

class AnthropicBetaPolicy(str, Enum):
    CHANNEL = "channel"
    CLIENT = "client"
    MERGE = "merge"                  # 合并渠道和客户端 beta
    CHANNEL_IF_MISSING = "channel_if_missing"
```

同时提供 `ChannelCreate` 和 `ChannelUpdate` 用于管理 API 的创建/更新请求体。

### ApiKey 模型

```python
class ApiKey(BaseModel):
    id: str                           # key_{uuid4_hex[:8]}
    name: str                         # API Key 名称
    key: str                          # llmplug-api-{random}
    allowed_models: list[str] = []    # 允许访问的模型（空=全部）
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

### LBConfig 模型

```python
class LBConfig(BaseModel):
    max_fail_count: int = 5
    cooldown_seconds: int = 60
```

兼容接口，实际存储已迁移到 `settings.json`。

---

## 存储层

> 对应文件：`storage.py`

### 模块定位

负责数据持久化存储，使用 JSON 文件作为存储介质，提供异步安全的读写接口和内存缓存。

### 缓存机制

```python
_cache: dict | None = None    # 渠道数据内存缓存
_cache_ts: float = 0          # 缓存时间戳
_CACHE_TTL = 5.0              # 缓存有效期（秒）

_keys_cache: dict | None = None  # API Keys 内存缓存
_keys_cache_ts: float = 0

_MODEL_GROUPS_CACHE: list[ModelGroup] | None = None  # 模型组缓存
_MODEL_GROUPS_CACHE_TS: float = 0
```

三类数据（渠道、API Keys、模型组）各自独立缓存和锁，TTL 均为 5 秒。

### 核心函数

#### `load_data() -> dict`

异步读取渠道数据，优先从内存缓存读取。

#### `save_data(data) -> None`

异步写入渠道数据，使用原子写入（临时文件 → fsync → `os.replace()`）。**仅在你已自行持锁或确定无竞态时使用**。

#### `atomic_update_data(mutator)` ★推荐

在 channels 锁内完成 read-modify-write，消除 lost-update 竞态。`mutator` 接收最新 data 字典，可原地修改或返回新字典。

```python
async def atomic_update_data(mutator: Callable[[dict], Any]):
    # 锁内: 读磁盘 → mutator(data) → 写磁盘 → 更新缓存 → 触发回调
```

#### `atomic_update_api_keys(mutator)`

API Keys 版本的原子 read-modify-write，用法同上。

#### `load_api_keys() -> dict` / `save_api_keys(data)` / `load_model_groups() -> list[ModelGroup]`

类似的读写接口，模型组数据存在 `channels.json` 的 `model_groups` 键中。

#### `get_model_group_by_name(name) -> ModelGroup | None`

按名称查找启用的模型组，代理入口用来判断请求的 model 是否为模型组。

### 保存回调机制

```python
register_save_callback(callback)       # 渠道数据保存时触发
register_api_keys_save_callback(callback)  # API Keys 保存时触发
```

用于缓存失效订阅。例如：
- `proxy/channel_registry.py:schedule_invalidate_model_channels_cache()` 通过 `register_save_callback` 注册，渠道变更时自动清理渠道缓存；`proxy_core._schedule_invalidate_model_channels_cache()` 保留为兼容入口
- `storage._invalidate_model_groups_cache_sync()` 也通过此机制串联
- `main._invalidate_api_key_index()` 通过 `register_api_keys_save_callback` 注册

### 注意事项

1. **不要直接修改 channels.json** — 走 `atomic_update_data()`，否则缓存最多 5 秒才更新
2. **原子写入** — 所有写操作使用临时文件 + `os.replace()`，防止写入中途崩溃导致文件损坏
3. **锁是 asyncio.Lock** — 异步函数不会在同一线程上嵌套获取锁，懒加载创建确保绑定正确的事件循环

---

## HTTP 客户端

> 对应文件：`client.py`

### 模块定位

管理与上游 LLM API 的 HTTP 连接，区分普通请求和流式请求两类场景。

### 全局缓存

```python
_clients: dict[str, httpx.AsyncClient] = {}   # 普通请求客户端池
_cache_ts: dict[str, float] = {}              # 每个客户端的最后使用时间
_lock = asyncio.Lock()                        # 并发安全锁
```

**缓存键**：`f"{channel.base_url}|{channel.socks5_proxy or ''}"`

### 核心函数

#### `create_client(channel) -> httpx.AsyncClient`

创建或获取缓存的客户端，用于**非流式**请求。内部调用 `get_or_create_client()`。

#### `create_stream_client(channel) -> httpx.AsyncClient`

创建**独立的流式客户端**，每次调用都新建，**不缓存**。由 `_do_stream_request()` 在 `finally` 块中手动 `aclose()`。

#### `get_upstream_headers(channel, extra_headers) -> dict`

构建上游 API 认证头：

| 渠道类型 | 认证方式 |
|----------|----------|
| Anthropic | `x-api-key` + `anthropic-version`（默认 `2023-06-01`）+ 可选 `anthropic-beta` |
| OpenAI 系列 | `Authorization: Bearer {api_key}` |

Anthropic 版本/Beta 走策略处理（`_apply_anthropic_headers()`）：从客户端 `extra_headers` 里 `pop` 走 `anthropic-version` 和 `anthropic-beta`，再按 `AnthropicVersionPolicy` / `AnthropicBetaPolicy` 策略写回。

### 客户端生命周期管理

#### `cleanup_stale_clients(max_age=300.0)`

定期清理超过 `max_age` 秒未使用的客户端连接，由 `main.py` 的后台任务每 300 秒调用一次。

#### `invalidate_all_clients()`

关闭并清除所有缓存的普通客户端。`config.update_settings()` 在 `request_timeout` 变更时调用，以应用新超时。

#### `remove_channel_client(channel)`

从缓存中移除指定渠道的客户端，用于渠道配置变更后刷新连接。

#### `close_all_clients()`

关闭所有缓存客户端，在应用关闭时（`lifespan` 的 `finally` 块）调用。

### 三种客户端对比

| 特性 | 普通客户端 | 独立流式客户端 |
|------|------|------|
| 创建方式 | 缓存复用 | 每次新建 |
| 关闭方式 | `close_all_clients()` / `cleanup_stale_clients()` | 手动 `aclose()`（finally 块） |
| 可用 async with | 不可（会关闭共享连接） | 可以 |

### 注意事项

1. **不要用 async with 包裹缓存的客户端**：会关闭共享连接
2. **独立流式客户端必须手动关闭**：在 `finally` 块中调用 `aclose()`
3. **连接池配置**：`max_connections=200`，`max_keepalive_connections=50`，`keepalive_expiry=60s`

---

## 路由层

> 对应目录：`routers/`

### 模块定位

负责 HTTP 请求的接入和错误处理，核心处理委托给 `proxy_core.py`。

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
1. 从 `scope["state"]` 获取中间件预解析的 body
2. 提取 model/stream
3. 调用 `proxy_core.proxy_request()`
4. 返回响应（流式 StreamingResponse / 非流式 JSONResponse）

### auth.py — 鉴权

代理鉴权主要由 `CombinedMiddleware` 完成，结果写入 `scope["state"]["proxy_auth_checked"]`。

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

管理 API 路由使用 `AdminAuthRoute` 自定义路由类，在路由级自动完成：
- 会话校验（从 Cookie 提取 session token 并验证）
- CSRF 校验（写操作需要 `X-CSRF-Token` 头）
- SSRF 防护（`_validate_outbound_url()` 拒绝非公网、内网、本机地址）

---

## 代理核心

> 对应文件：`proxy_core.py`（兼容门面）和 `proxy/`（实际实现包）

### 模块定位

`proxy_core.py` 是兼容门面，导入时会返回 `proxy.core`，用于保留旧 import、测试 monkeypatch 和模块级状态赋值行为。实际代理核心在 `proxy/` 包内拆分维护：`proxy/core.py` 负责调度主流程，其他子模块承载清晰的协议、缓存和转换职责。

### 文件分工

| 文件 | 职责 |
|------|------|
| `proxy_core.py` | 兼容门面，禁止新增业务实现 |
| `proxy/core.py` | `proxy_request()`、单模型/模型组 Fallback、`_do_request()`、`_do_stream_request()` 主流程和旧私有 API 兼容 wrapper |
| `proxy/channel_registry.py` | `_get_channels_for_model()` 的真实实现、模型渠道缓存、保存回调、删除渠道后的 LB 清理 |
| `proxy/conversion.py` | `CONVERTER_MAP`、转换器选择、跨格式渠道过滤、Responses `previous_response_id` 历史展开 |
| `proxy/stream_sse.py` | SSE block 解析、SSE 格式化、Anthropic 事件合成、非 SSE JSON 转流式事件 |
| `proxy/stream_reconstruct.py` | 从流式 chunks 重建完整响应体，供请求日志保存 |
| `proxy/errors.py` / `routing.py` / `non_stream_executor.py` / `stream_executor.py` / `media.py` | 窄 re-export 模块，用于表达边界并保留旧导入面 |

### 核心函数

#### `proxy_request(model, request_data, target_api_type, is_stream, ...)`

主入口，由路由层调用。

**流程**：
1. 检查模型组（`storage.get_model_group_by_name(model)`）
2. 若是模型组 → `_proxy_model_group_request()`：按 `group.models` 顺序逐个模型尝试
3. 若是单模型 → `_proxy_single_model_request()`：获取渠道 → 过滤 → 故障转移循环

#### `_do_request(channel, request_data, ...)`

单次请求执行，包含完整的转换 + 发送 + 统计流程：

1. 获取转换器（`_get_converter_and_upstream_type()`）
2. 展开 Responses 本地历史（`_prepare_openai_response_request_for_upstream()`）
3. 转换请求体（客户端格式 → 上游格式）
4. **能力过滤**（`infer_capabilities()` + `apply_capability_filter()`）
5. MiniMax 特殊处理：合并多条 system 消息
6. 构建上游 URL 和请求头
7. 发送请求（流式/非流式）
8. 转换响应（上游格式 → 客户端格式）
9. 思考内容过滤（非流式）
10. 提取 token 用量
11. 记录统计和请求日志（异步入队，不阻塞响应）

#### `_do_stream_request(...)`

流式请求处理，异步生成器。核心流程：

1. 创建独立流式客户端（`create_stream_client()`）
2. `client.stream("POST", ...)` 发起流式请求
3. `proxy/stream_sse.py:iter_sse_blocks()` 逐块解析上游 SSE
4. 逐块转换 + ThinkFilter 过滤
5. 增量提取 token 用量和 finish_reason
6. 处理 `[DONE]` 信号 + finalize_stream
7. 非 SSE 响应兜底：由 `proxy/stream_sse.py` 把整块 JSON 拆成流式事件序列
8. `finally` 块：由 `proxy/stream_reconstruct.py` 构建流式响应体，随后记录统计、关闭客户端

### 异常体系

| 异常类 | 说明 | 是否可重试 |
|--------|------|-----------|
| `ConverterError` | 格式转换失败 | 是 |
| `_EmptyStreamError` | 上游连接成功但无任何 SSE 输出 | 是 |
| `_UpstreamStreamErrorEvent` | 上游流式事件中包含错误 | 是 |
| `_StreamPreflightError` | 首包前错误（包装原始异常） | 是 |

`_is_retryable_exception()` 判断可重试条件：网络异常、上述自定义异常、5xx/429 HTTP 状态码。

### 渠道过滤

`_filter_channels_by_conversion()` 按"是否允许跨格式转换"过滤渠道：
- 同格式渠道（透传）始终通过
- 跨格式渠道按 `channel.allow_format_conversion` 决定，`None` 时回退全局 `settings.allow_format_conversion`

### 首包前错误处理

`_prime_stream()` 消费首个 chunk，让连接和首包前错误进入故障转移循环：
- `StopAsyncIteration`（空流）→ `_EmptyStreamError`
- `_StreamPreflightError` → 解包原始异常重新抛出

`_raise_preflight_stream_errors()` 包裹生成器，在首个 yield 前捕获异常并转为 `_StreamPreflightError`。

### 辅助函数

| 函数 | 说明 |
|------|------|
| `_get_channels_for_model(model)` | 兼容 wrapper；真实实现位于 `proxy/channel_registry.py`，筛选匹配模型的已启用渠道（带缓存） |
| `_get_converter_and_upstream_type(channel, target_api_type)` | 兼容 wrapper；真实实现位于 `proxy/conversion.py`，从 `CONVERTER_MAP` 获取转换器 |
| `_get_upstream_url(channel)` | 调用 `url_builder.build_upstream_url()` |
| `_build_upstream_headers(channel, client_headers)` | 构建上游请求头（含转发客户端头） |
| `_build_stream_response_body(chunks, ...)` | 兼容导出；真实实现位于 `proxy/stream_reconstruct.py`，从流式 chunks 拼装完整响应体（用于请求记录） |
| `_record_request(**kwargs)` | 写统计 + 请求记录（不阻塞响应） |

### 注意事项

1. **非流式请求使用缓存客户端**：不能 `async with` 关闭
2. **流式请求使用独立客户端**：`finally` 中手动 `aclose()`
3. **故障转移循环**：`all_tried` 集合保证同一渠道不会重试
4. **流式中途错误**：已通过 SSE 输出则发送错误事件并正常结束；未输出则抛异常走故障转移
5. **MAX_STREAM_CHUNKS=2000**：流式记录 chunk 数量上限，防止内存溢出

---

## 转换器

> 对应目录：`converters/`

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

每个 Converter 实例维护 `_stream_state`，一个实例只服务一次流式请求，不可复用。一个上游 chunk 可能产出多个下游事件，通过 `_extra_events` + `get_extra_events()` 取出。

### usage.py — Token 用量提取

`cache_token_details(usage)` 从 usage 字典中提取 `cache_read_input_tokens` 和 `cache_creation_input_tokens`（兼容不同 API 的命名差异）。

### 注意事项

1. **Tool 消息合并**：Anthropic 要求 `tool_result` 放在 `user` 角色消息中
2. **流式转换返回列表**：一个上游 chunk 可能产生多个下游事件
3. **Thinking/Reasoning 支持**：支持 OpenAI 的 `reasoning_content` 和 Anthropic 的 `thinking` 互转

---

## 负载均衡器

> 对应文件：`balancer/load_balancer.py`

### 模块定位

从多个可用渠道中选择一个来处理请求，实现优先级分组 + 加权轮询 + 健康检查。

### ChannelHealth 类

```python
class ChannelHealth:
    fail_count: int = 0        # 连续失败次数
    last_fail_time: float = 0  # 上次失败时间戳
    current_weight: int = 0    # 当前轮询权重（SWRR 内部状态）
```

健康判断：`fail_count < max_fail_count` 或 `当前时间 - last_fail_time > cooldown_seconds`。成功一次即重置 `fail_count`。

### LoadBalancer 核心方法

#### `select_channel(channels, exclude_ids)`

```python
async def select_channel(
    channels: list[Channel],
    exclude_ids: set[str] | None = None,
) -> Optional[Channel]
```

**流程**：
1. 过滤：排除 disabled、已试（`exclude_ids`）、不健康的渠道
2. 分组：按 `priority` 升序排序，取最小 priority 组
3. 选择：组内加权轮询（单渠道直接返回）

#### `_weighted_round_robin(channels)`

**平滑加权轮询算法**（SWRR，类似 Nginx）：

```
1. 每个 channel 的 current_weight += weight
2. 选择 current_weight 最大的 channel
3. 被选中 channel 的 current_weight -= total_weight
```

### 配置热更新

```python
async def update_config(max_fail_count: int = 5, cooldown_seconds: int = 60)
```

由 `config.update_settings()` 在负载均衡相关设置变更时自动调用。

### 渠道清理

```python
async def cleanup_removed_channels(active_channel_ids: set[str])
```

从健康状态字典中移除已删除的渠道，防止内存泄漏。由 `proxy/channel_registry.py:schedule_invalidate_model_channels_cache()` 在渠道保存后触发，并通过 `proxy_core._schedule_invalidate_model_channels_cache()` 兼容暴露。

### 注意事项

1. **priority 数字越小优先级越高**：1 比 2 优先
2. **冷却期只是恢复探测**：冷却过后允许再试一次，不等于恢复
3. **current_weight 不重置**：保证长时间运行的流量分配比例精确
4. **ChannelHealth 内存存储**：进程重启清零
5. **线程安全**：整个选择过程在 `asyncio.Lock` 内完成

---

## 能力管理

> 对应文件：`capability_manager.py`

### 模块定位

根据渠道配置和模型名称推断上游提供商的能力限制，在请求发送前过滤不支持的参数。支持渠道级和模型级两级能力覆盖。

### ProviderCapabilities 数据类

```python
@dataclass
class ProviderCapabilities:
    supports_parallel_tool_calls: bool = True
    supports_tool_choice_auto: bool = True
    supports_response_format: bool = True
    supports_reasoning_effort: bool = True
    supports_file_content: bool = False
    supports_audio_content: bool = False
    supports_image_content: bool = False
    supports_tool_choice_required: bool = True
    supports_strict_tools: bool = True
    requires_single_system_message: bool = False
    filter_think_content: bool = False
```

多模态能力（`supports_file_content` / `supports_audio_content` / `supports_image_content`）默认为 `False`，需要通过渠道级或模型级覆盖开启。

### 核心函数

#### `infer_capabilities(channel, model_name="") -> ProviderCapabilities`

推断逻辑：
1. 优先使用 `channel.capabilities` 字段（渠道级覆盖）
2. 否则按 `base_url` 关键词匹配（`deepseek` / `minimax`）
3. 都不匹配则返回默认（全部支持，多模态除外）
4. 最后检查 `channel.model_capabilities[model_name]`（模型级覆盖），仅作用于多模态能力

**解析优先级**（仅多模态能力）：
```
model_capabilities[model] > channel.capabilities > vendor 推断 > 默认值
```

#### `apply_capability_filter(request_data, caps, channel_name="", model_name="") -> dict`

根据能力逐项过滤请求参数，不支持的参数会被静默移除并记录 WARNING 日志。

多模态内容过滤支持：
- `image_url` / `image`：图片内容（OpenAI / Anthropic 格式）
- `input_audio`：音频内容
- `file`：文件内容

过滤时记录包含渠道名、模型名和被过滤类型的 warn 日志。

#### `merge_system_messages(messages) -> list`

合并多条 system 消息为单条，MiniMax 等要求单 system 的提供商使用。将多条 system 消息的文本用 `\n\n` 连接，放在消息列表最前面。

### 多模态文件保存

`proxy/core.py` 中的 `_save_multimodal_files()` 函数可在请求发送前保存请求中的多模态文件到磁盘，并通过 `proxy_core._save_multimodal_files()` 兼容暴露。保存行为由设置页的三个开关控制：

| 设置项 | 说明 |
|--------|------|
| `save_images` | 保存图片到 `logs/images/` |
| `save_audios` | 保存音频到 `logs/audios/` |
| `save_files` | 保存文件到 `logs/files/` |

文件名格式：`{timestamp}_{model}_{hash}.{ext}`，例如 `20260612_143052_gpt-4o_a7f3.png`。写入使用异步线程（`asyncio.to_thread`），不阻塞请求主路径。

---

## URL 构建

> 对应文件：`url_builder.py`

### 模块定位

负责构造发给上游的完整 URL，处理各种 base_url 格式的兼容性。

### 核心函数

#### `build_upstream_url(channel) -> str`

1. 如果 `channel.endpoint_url` 非空，直接使用
2. 否则用 `base_url` + API 路径（如 `/chat/completions`）自动拼接
3. `append_api_path()` 智能处理：如果 base_url 已包含 `/v1`，直接追加路径；如果已包含目标路径后缀，不重复拼接

#### `build_models_url(base_url, models_url) -> str`

构造模型列表 URL，`models_url` 优先。

#### `append_query(url, query_string) -> str`

合并客户端透传的 query 参数和 URL 已有的 query，使用 `urllib.parse` 处理，避免手写 `?` 破坏 URL。

### API 路径映射

```python
_UPSTREAM_PATHS = {
    "openai-chat-completions": "/chat/completions",
    "openai-response": "/responses",
    "anthropic": "/messages",
}
```

---

## 思考过滤

> 对应文件：`think_filter.py`

### 模块定位

过滤模型输出中的思考过程内容，支持 `<think>...</think>` 标签和 `💭...💭` emoji 两种格式。

### 两种模式

#### 静态过滤：`filter_think_content_static(content)`

正则一次性替换，适用于非流式响应。

#### 流式过滤：`ThinkFilter` 类

增量状态机，逐 chunk 处理：

```python
tf = ThinkFilter()
for chunk_text in stream:
    filtered = tf.feed(chunk_text)  # 返回过滤后的文本
remaining = tf.flush()              # 流结束时取残余内容
```

**跨 chunk 处理**：
- 标签可能横跨多个 chunk，状态机通过 buffer 缓存未匹配的部分
- `_partial_start_tag_len()` 检测 buffer 末尾是否为标签前缀（如 `<thi`），避免误输出
- `flush()` 时如果在 `<think>` 块内则丢弃，在 `💭` 块内则输出残余（因为 `💭` 既作开始也作结束标记）

---

## IP 白名单

> 对应文件：`whitelist.py`

### 模块定位

基于 CSV 文件的 IP 访问控制，对请求路径 + HTTP 方法 + 客户端 IP 做规则匹配。

### 规则格式

CSV 文件 `data/whitelist.csv`，四列：

```csv
path_pattern,methods,cidr,desc
/v1/chat/completions,POST,10.0.0.0/8,内网 Chat
/admin/*,,192.168.1.100/32,管理员
```

- `path_pattern`：`fnmatchcase` 通配符匹配请求路径
- `methods`：用 `|` 分隔多个方法，空或 `*` 表示允许所有
- `cidr`：`ipaddress.ip_network(strict=False)` 解析
- `desc`：规则描述

以 `#` 开头的行和空行被忽略。

### WhitelistCache

```python
class WhitelistCache:
    def get_rules(self) -> list[WhitelistRule]:
        # 用文件 mtime 判断是否需要重新加载
```

修改 CSV 文件后自动生效，无需重启。

### check_request(rules, path, method, client_ip)

返回 `(allow: bool, reason: str)`：
- 路径无匹配规则 → 放行
- 路径有规则但 IP 不在范围 → 拒绝
- IP 在范围但方法不允许 → 拒绝
- 全部匹配 → 放行
- **无任何规则时默认放行**

### validate_rules_text(text)

前端白名单编辑页的校验入口，解析 CSV 文本并返回 `(valid, error_message, rules)`。

---

## 管理员鉴权

> 对应文件：`admin_auth.py`

### 模块定位

管理后台的密码管理、会话验证和 CSRF 防护。

### 密码存储

- 存储位置：`data/admin_auth.json`
- 哈希算法：PBKDF2-SHA256，260,000 轮迭代
- 格式：`algo|iterations|salt_hex|digest_hex`

### 会话机制

- Session token 结构：`{expiry}.{nonce}.{hmac_signature}`
- 签名密钥：密码哈希值（HMAC-SHA256）
- TTL：24 小时
- Cookie 名称：`admin_session`，属性 `HttpOnly; SameSite=Lax`
- 登出时将 token 的 SHA256 摘要加入 `revoked_sessions`，定期清理过期的撤销记录

### CSRF 防护

- 写操作（POST/PUT/DELETE/PATCH）需要 `X-CSRF-Token` 头
- CSRF token = `HMAC-SHA256(password_hash, SHA256(session_token))`
- 与 session 绑定，session 失效则 CSRF 也失效

### 核心函数

| 函数 | 说明 |
|------|------|
| `setup_admin_password(password)` | 首次设置密码 |
| `setup_and_login(password)` | 原子操作：未设置则先设置，然后验证并创建会话 |
| `validate_admin_session(token)` | 验证会话 token（检查过期 + 撤销 + 签名） |
| `create_admin_csrf_token(session_token)` | 生成 CSRF token |
| `validate_admin_csrf_token(session_token, csrf_token)` | 验证 CSRF token |
| `clear_admin_session(token)` | 撤销会话（登出） |

---

## 请求记录

> 对应文件：`request_logs.py`

### 模块定位

记录每条代理请求的详细信息（token 用量、延迟、错误、请求/响应头等），仅支持 SQLite3 后端。

### 架构

```
proxy.core._record_request()
    → record_request()              # 入队
        → asyncio.Queue (max=1000)
            → _request_log_worker() # 2 个后台 worker
                → backend.write_record()
```

### 后端实现

| 后端 | 说明 |
|------|------|
| `SQLiteRequestLogBackend` | 默认，使用 WAL 模式 + 64MB mmap |
`SQLiteRequestLogBackend` 提供 `init()` / `close()` / `write_record()` / `list_requests()` / `get_request_field()` / `cleanup_old_records()`。

### RAW 字段

可选保存的原始数据字段（受 `save_request_headers` 等配置控制）：
- `request_headers` / `response_headers`
- `request_body` / `response_body`

超过 `max_log_body_size`（默认 64KB）时自动截断，保留 `_preview` + 元信息。

### 队列溢出保护

当队列满（1000 条）时，记录会被写入 `data/request_logs_overflow.jsonl` 文件，防止丢失。

### 数据清理

`cleanup_old_records()` 支持两级保留策略：
- `request_log_raw_retention_days`：超过此天数的记录清除 RAW 字段（释放空间）
- `request_log_retention_days`：超过此天数的记录整行删除

启动时 + 每 24 小时自动执行清理。

---

## Responses 状态

> 对应文件：`response_state.py`、`state_store.py`

### 模块定位

为 OpenAI Responses API 提供会话状态持久化。当客户端使用 `previous_response_id` 引用历史对话时，代理需要从本地存储加载并展开。

### response_state.py

入口模块，管理全局 `FileStore` 实例：

```python
_responses_store = FileStore(
    data_dir="data/responses_session/",
    max_entries=1000,    # 可前端设置
    ttl_minutes=60,      # 可前端设置
)

def get_responses_store() -> FileStore:
    return _responses_store

def reload_responses_store():
    """设置变更后刷新配置"""
```

### state_store.py — FileStore

基于文件系统的状态存储，每个 response 存为独立 JSON 文件。

#### 核心方法

| 方法 | 说明 |
|------|------|
| `get_response(response_id)` | 获取响应数据，过期返回 None，读取时更新 mtime（LRU） |
| `get_conversation(response_id)` | 获取对话消息列表（用于展开 `previous_response_id`） |
| `put(response_id, conversation, response)` | 存储会话记录 |
| `delete(response_id)` | 删除会话记录 |
| `cleanup_expired()` | 清理过期文件 |
| `evict_lru()` | 按 mtime 淘汰超出容量的最旧文件 |

#### 淘汰策略

- **TTL 过期**：`expires_at` 字段与当前时间比较
- **LRU 容量淘汰**：按文件 `mtime` 排序，删除最旧的，保留最新的 `max_entries` 个
- 读取文件时用 `os.utime(path, None)` 更新 mtime，实现 LRU 跟踪
- 后台定期执行 `_cleanup_if_needed()`（过期 + LRU）

#### 文件结构

```json
{
  "response_id": "resp_xxxx",
  "conversation": {"messages": [...], "instructions": "..."},
  "response": {...},
  "created_at": 1234567890,
  "expires_at": 1234571490,
  "last_access_at": 1234567890
}
```

所有文件写入使用原子写入（临时文件 + `os.replace()`）。
