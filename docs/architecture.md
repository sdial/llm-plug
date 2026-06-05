# 架构设计

本文档介绍 LLM-Plug 的核心概念、系统架构和模块划分。面向刚加入团队的开发者，帮你快速理解整个系统"是什么"和"怎么跑"。

## 一句话概述

LLM-Plug 是一个 **LLM API 格式转换代理服务**：客户端用一种 API 格式发请求，服务端自动转换后转发给不同格式的上游 LLM 提供商，再把响应转换回来——对客户端完全透明。

## 核心概念

### 三种 API 格式

| 格式名 | 枚举值 | 代理端点 | 说明 |
|--------|--------|----------|------|
| OpenAI Chat Completions | `openai-chat-completions` | `POST /v1/chat/completions` | 最常见的 OpenAI 对话格式 |
| OpenAI Response | `openai-response` | `POST /v1/responses` | OpenAI 新版 Responses API |
| Anthropic Messages | `anthropic` | `POST /v1/messages` | Anthropic Claude 对话格式 |

### 渠道 (Channel)

渠道代表一个上游 LLM 提供商的连接配置。一个渠道对应一个上游 API Key + 一组模型：

```json
{
  "id": "ch_xxxx",
  "name": "OpenAI 官方",
  "api_type": "openai-chat-completions",
  "base_url": "https://api.openai.com",
  "endpoint_url": null,
  "api_key": "sk-xxx",
  "models": ["gpt-4o", "gpt-4o-mini"],
  "enabled": true,
  "weight": 1,
  "priority": 1,
  "socks5_proxy": null,
  "capabilities": null,
  "allow_format_conversion": null,
  "anthropic_version": null,
  "anthropic_version_policy": "channel",
  "anthropic_beta": null,
  "anthropic_beta_policy": "channel"
}
```

| 字段 | 说明 |
|------|------|
| `api_type` | 上游 API 格式（三种之一） |
| `base_url` | 上游 API 基础地址 |
| `endpoint_url` | 可选，指定完整上游端点 URL，优先于 `base_url` 拼接 |
| `models` | 该渠道支持的模型列表 |
| `weight` | 负载均衡权重，数值越大分配越多请求 |
| `priority` | 优先级，数字越小优先级越高（1 比 2 优先） |
| `socks5_proxy` | 可选 SOCKS5 代理地址 |
| `capabilities` | 可选，手动覆盖上游提供商的能力推断（详见"能力管理"章节） |
| `allow_format_conversion` | 可选，是否允许跨格式转换；为 `null` 时读取全局设置 |
| `anthropic_version` / `anthropic_beta` | Anthropic 渠道专用：自定义 API 版本和 Beta 标记 |

### 模型路由

请求到达时，根据请求体中的 `model` 字段匹配渠道：
1. 先检查是否为"模型组"名称（详见"模型组 Fallback"）
2. 筛选所有 `enabled=true` 且 `models` 包含该模型的渠道
3. 按 `allow_format_conversion` 过滤跨格式渠道
4. 按 `priority` 分组，优先使用高优先级组
5. 组内按 `weight` 加权轮询选择

### 负载均衡策略

```
1. 按 priority 分组（数字越小越优先）
2. 最高优先级组内加权轮询（weight）
3. 失败渠道自动冷却恢复（max_fail_count + cooldown_seconds）
4. 当前渠道失败时，降级到下一优先级组重试
```

负载均衡使用**平滑加权轮询算法**（SWRR，类似 Nginx）：每个渠道维护一个 `current_weight`，每轮选择时所有渠道 `current_weight += weight`，选最大的，被选中后 `current_weight -= total_weight`。这样能保证流量分配比例精确且分布均匀。

### 模型组 Fallback

模型组把多个模型组成一个"虚拟模型"，客户端请求模型组名称时，代理按组内模型顺序逐个尝试。每个模型内部仍走正常的负载均衡 + 故障转移。

**两层 Fallback 关系**：先在模型组层面按模型顺序 Fallback，再在每个模型内部按渠道优先级 + 加权轮询做故障转移。

## 请求处理流程

```
┌──────────┐  POST /v1/chat/completions  ┌──────────────────────────────┐
│  Client  │ ──────────────────────────▶  │  CombinedMiddleware (ASGI)  │
│ (OpenAI) │                              │  1. IP 白名单检查            │
└──────────┘                              │  2. 管理员会话校验（/admin） │
                                          │  3. 代理鉴权（API Key）      │
                                          │  4. 解析 body、校验 model    │
                                          │  5. 写 scope["state"]        │
                                          └──────────┬───────────────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │  Router (proxy_base) │
                                          │  格式分发             │
                                          └──────────┬───────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │    proxy_core.py     │
                                          │  proxy_request()     │
                                          │                      │
                                          │  循环:               │
                                          │  1. 模型组解析       │
                                          │  2. 渠道选择(均衡器) │
                                          │  3. 请求体转换       │
                                          │  4. 能力过滤         │
                                          │  5. 发送上游         │
                                          │  6. 响应体转换       │
                                          │  7. 思考内容过滤     │
                                          │  8. 失败则重试下一个 │
                                          └──────────┬───────────┘
                                                     │
                          ┌──────────────────────────┼──────────────────────────┐
                          ▼                          ▼                          ▼
                   ┌─────────────┐           ┌─────────────┐           ┌─────────────┐
                   │  Converter  │           │  Converter  │           │  Converter  │
                   │  to_chat    │           │  to_response│           │  to_anthropic│
                   └──────┬──────┘           └──────┬──────┘           └──────┬──────┘
                          │                         │                         │
                          ▼                         ▼                         ▼
                   ┌─────────────┐           ┌─────────────┐           ┌─────────────┐
                   │  Upstream   │           │  Upstream   │           │  Upstream   │
                   │ OpenAI Chat │           │ OpenAI Resp │           │  Anthropic  │
                   └─────────────┘           └─────────────┘           └─────────────┘
```

**处理步骤**：

1. **中间件入口**：`main.py` 的 `CombinedMiddleware`（纯 ASGI 实现，不是 `BaseHTTPMiddleware`，专门为了避免后者的流式 bug）。依次执行：
   - **IP 白名单**：读取 `data/whitelist.csv`，按路径模式 + HTTP 方法 + CIDR 匹配，无规则时默认放行
   - **管理员鉴权**：`/admin` 页面请求检查会话 Cookie，未登录重定向到登录页
   - **代理鉴权**：从 `Authorization: Bearer xxx` 或 `x-api-key: xxx` 头提取 Token，匹配 API Key，校验 `allowed_models`
   - **请求体解析**：缓冲 body，校验体积（默认上限 10MB），解析 `model` 和 `stream` 字段
   - **状态写入**：将解析结果写入 `scope["state"]`，供下游路由使用

2. **路由分发**：`proxy_base.py` 的工厂函数 `make_proxy_router()` 为三个端点生成处理器，仅做格式分发
3. **渠道选择**：从 storage 加载匹配 model 的已启用渠道，按 `allow_format_conversion` 过滤，通过 `LoadBalancer.select_channel()` 选择
4. **请求转换**：根据客户端 API 类型与上游渠道类型，从 `CONVERTER_MAP` 选取对应 converter 转换请求体
5. **能力过滤**：`capability_manager.apply_capability_filter()` 在转换之后、发送之前运行，移除上游不支持的参数（详见"能力管理"）
6. **上游请求**：非流式用缓存的 `httpx.AsyncClient`；流式用独立的 `create_stream_client()` 新建客户端，在生成器 `finally` 中 `aclose()`
7. **响应转换**：converter 将上游响应转换回客户端格式（非流式 JSON 或流式 SSE chunks）
8. **思考过滤**：如果上游是 DeepSeek 等需要过滤 `💭` 标记的提供商，用 `ThinkFilter` 状态机过滤思考内容
9. **故障转移**：请求失败时记录故障，排除已试渠道后重新选择，直到成功或所有渠道耗尽

## 转换矩阵

行 = 入口格式（客户端），列 = 上游格式。`ToXxxConverter` 把**上游格式转回入口格式**（响应方向）。

| 入口 \ 上游 | openai-chat-completions | openai-response | anthropic |
|---|---|---|---|
| **openai-chat-completions** | 直通 | `ToChatCompletionsConverter` | `ToChatCompletionsConverter` |
| **openai-response** | `ToResponseConverter` | 直通 | `ToResponseConverter` |
| **anthropic** | `ToAnthropicConverter` | `ToAnthropicConverter` | 直通 |

**转换器特性**：
- 支持非流式（JSON）和流式（SSE）转换
- 流式转换通过内部状态机逐 chunk 处理，每个 Converter 实例维护 `_stream_state`，一个实例只服务一次流式请求
- 同格式请求直接透传，不走 converter
- Anthropic 输出 SSE 含 `event:` 行，OpenAI 仅含 `data:` 行
- 一个上游 chunk 可能产出多个下游事件，通过 `_extra_events` + `get_extra_events()` 取出

### Responses → Chat Completions 支持边界

当客户端使用 `openai-response`，但上游渠道是 `openai-chat-completions` 时，代理会把可表达能力转换为 Chat Completions 请求，再把响应转换回 Responses 结构。Chat Completions 无法承载的 Responses 托管能力不会被静默忽略。

| 能力 | 支持等级 | 说明 |
|------|----------|------|
| 文本输入/输出 | 完整 | `input`、`instructions`、`output_text` 双向转换 |
| function tools | 完整 | `tools[].function`、`tool_choice`、`function_call`、`function_call_output` 转换 |
| 图片输入 | 按上游能力 | `input_image` 转为 Chat `image_url` |
| 文件/音频输入 | 按上游能力 | 转为 Chat 内容块；上游不支持时应拒绝或按渠道能力处理 |
| 托管工具 | 不支持，显式错误 | `web_search`、`file_search`、`code_interpreter`、`computer_use`、`mcp` 等不会静默丢弃 |
| 状态历史 | 按上游能力处理 | 同格式上游透明透传 `previous_response_id`；Chat/Anthropic 等无原生 Responses 状态的上游由代理本地加载历史并展开 |

## 能力管理

`capability_manager.py` 根据渠道的 `base_url` 关键词自动推断上游提供商的能力限制，在请求发送前自动过滤不支持的参数。

### 自动推断规则

| 提供商 | base_url 关键词 | 能力限制 |
|--------|-----------------|----------|
| DeepSeek | `deepseek` | 关闭 `parallel_tool_calls`；过滤 `💭` 思考块 |
| MiniMax | `minimax` | 要求单条 system 消息（多条自动合并） |
| 其他 | — | 默认全部支持 |

### 手动覆盖

在渠道配置的 `capabilities` 字段中可显式指定能力，覆盖自动推断。支持的能力键：

| 键 | 默认值 | 说明 |
|----|--------|------|
| `supports_parallel_tool_calls` | `true` | 是否支持并行工具调用 |
| `supports_tool_choice_auto` | `true` | 是否支持 `tool_choice: "auto"` |
| `supports_tool_choice_required` | `true` | 是否支持 `tool_choice: "required"` |
| `supports_response_format` | `true` | 是否支持 `response_format` |
| `supports_reasoning_effort` | `true` | 是否支持 `reasoning_effort` |
| `supports_strict_tools` | `true` | 是否支持 function 的 `strict` 字段 |
| `supports_file_content` | `false` | 是否支持文件内容输入 |
| `supports_audio_content` | `false` | 是否支持音频内容输入 |
| `requires_single_system_message` | `false` | 是否要求单条 system 消息 |
| `filter_think_content` | `false` | 是否过滤 `💭` 思考标记 |

### 思考内容过滤

`think_filter.py` 的 `ThinkFilter` 是一个流式增量状态机，支持过滤 `<think>...</think>` 和 `💭...💭` 两种格式。跨 chunk 时保留标签边界，不会误伤正常内容。非流式场景使用 `filter_think_content_static()` 正则一次性过滤。

## URL 构建

`url_builder.py` 负责拼接发给上游的完整 URL：

- **`build_upstream_url(channel)`**：如果渠道设置了 `endpoint_url`，直接使用；否则用 `base_url` + `/v1` + API 路径（如 `/chat/completions`）自动拼接。智能检测 URL 是否已包含路径后缀，避免重复拼接。
- **`build_models_url(base_url, models_url)`**：构造模型列表 URL，`models_url` 优先。
- **`append_query(url, query_string)`**：合并客户端透传的 query 参数和 URL 已有的 query，避免手写 `?` 破坏 URL。

## IP 白名单

`whitelist.py` 基于 CSV 文件的 IP 访问控制：

### CSV 格式

```csv
path_pattern,methods,cidr,desc
/v1/chat/completions,POST,10.0.0.0/8,内网访问 Chat
/admin/*,,192.168.1.100/32,管理员 IP
```

| 列 | 说明 |
|----|------|
| `path_pattern` | 路径模式，支持 `fnmatch` 通配符（如 `/v1/*`） |
| `methods` | HTTP 方法，用 `\|` 分隔多个；空或 `*` 表示允许所有 |
| `cidr` | IP 地址或 CIDR 范围 |
| `desc` | 规则描述 |

### 工作机制

- `WhitelistCache` 用文件 `mtime` 判断是否重新加载，修改文件后自动生效
- `fnmatchcase` 匹配路径，`ipaddress.ip_network(strict=False)` 解析 CIDR
- **无任何规则时默认放行**（不是默认拒绝）
- 有匹配路径的规则但 IP 不在范围内时拒绝

## Responses 状态管理

`response_state.py` + `state_store.py` 提供 OpenAI Responses API 的会话状态持久化：

- `FileStore` 基于 `data/responses_session/` 目录，每个 response 存为独立 JSON 文件
- 支持 **LRU 淘汰**（按文件 mtime）+ **TTL 过期**，容量和过期时间可在前端设置页调整
- 代理通过 `previous_response_id` 把历史展开为 `input` 后再发上游（仅对不支持原生 Responses 状态的上游生效）
- 定期后台清理过期文件

## Anthropic 版本与 Beta 策略

Anthropic 渠道支持自定义 API 版本和 Beta 标记，通过策略枚举控制优先级：

### 版本策略 (`AnthropicVersionPolicy`)

| 策略 | 说明 |
|------|------|
| `channel`（默认） | 始终使用渠道配置的版本 |
| `client` | 优先使用客户端传入的版本 |
| `channel_if_missing` | 客户端未传时用渠道版本，传了就用客户端的 |

### Beta 策略 (`AnthropicBetaPolicy`)

| 策略 | 说明 |
|------|------|
| `channel`（默认） | 始终使用渠道配置的 beta |
| `client` | 优先使用客户端传入的 beta |
| `merge` | 合并渠道和客户端的 beta（去重） |
| `channel_if_missing` | 客户端未传时用渠道 beta |

## 模块职责

```
llm-plug/
├── main.py              # FastAPI 入口：路由注册、静态文件、CombinedMiddleware
├── config.py            # 配置管理：_CONFIG_SCHEMA 定义、settings.json 读写、热更新
├── storage.py           # 存储层：JSON 文件读写（asyncio.Lock + 缓存 + 原子写入）
├── client.py            # HTTP 客户端：httpx 缓存池 + SOCKS5 + Anthropic 头管理
├── proxy_core.py        # 代理核心：负载均衡调度、格式转换协调、故障转移
├── capability_manager.py # 能力管理：提供商推断 + 请求参数过滤
├── think_filter.py      # 思考内容过滤：<think>/💭 状态机（流式 + 静态）
├── url_builder.py       # URL 构建：上游地址拼接、query 合并
├── whitelist.py         # IP 白名单：CSV 解析 + 路径/CIDR 匹配
├── admin_auth.py        # 管理员鉴权：密码哈希、会话管理、CSRF
├── request_logs.py      # 请求记录：SQLite/PostgreSQL 后端 + 异步队列写入
├── response_state.py    # Responses 状态入口：FileStore 实例管理
├── state_store.py       # 文件状态存储：LRU + TTL 淘汰
├── stats.py             # 统计模块：SQLite 聚合统计
├── serve_viewer.py      # 独立日志查看服务（端口 8080）
│
├── models/              # 数据模型
│   ├── api_types.py     # APIType 枚举
│   ├── channel.py       # Channel Pydantic 模型 + Anthropic 策略枚举
│   ├── api_key.py       # ApiKey Pydantic 模型
│   └── model_group.py   # ModelGroup + LBConfig Pydantic 模型
│
├── routers/             # 路由层
│   ├── proxy_base.py    # 代理路由工厂（核心）
│   ├── proxy_chat.py    # /v1/chat/completions 端点
│   ├── proxy_response.py # /v1/responses 端点
│   ├── proxy_anthropic.py # /v1/messages 端点
│   ├── proxy_models.py  # /v1/models 端点
│   ├── admin.py         # 管理 API（含 SSRF 防护）
│   ├── auth.py          # 代理鉴权
│   └── proxy_errors.py  # 统一错误响应格式
│
├── converters/          # 格式转换器
│   ├── base.py          # 抽象基类
│   ├── to_chat.py       # → OpenAI Chat Completions
│   ├── to_response.py   # → OpenAI Response
│   ├── to_anthropic.py  # → Anthropic Messages
│   └── usage.py         # Token 用量提取
│
├── balancer/            # 负载均衡
│   └── load_balancer.py # 优先级分组 + 平滑加权轮询 + 健康检查
│
└── static/              # 管理界面
    ├── index.html       # TailwindCSS 单页应用（/admin）
    ├── admin-login.html # 登录页
    └── fragments/admin/ # htmx 局部刷新片段
```

## API 端点

### 代理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat Completions 格式代理 |
| POST | `/v1/responses` | OpenAI Response 格式代理 |
| POST | `/v1/messages` | Anthropic Messages 格式代理 |
| GET | `/v1/models` | OpenAI 风格模型列表 |
| GET | `/v1/anthropic/models` | Anthropic 风格模型列表 |
| GET | `/v1/responses/{id}` | 读取本地保存的 Responses 状态 |
| DELETE | `/v1/responses/{id}` | 删除本地保存的 Responses 状态 |

`GET /v1/responses/{id}` 和 `DELETE /v1/responses/{id}` 是本地状态接口：只访问代理在 `data/responses_session/` 中保存的 response，不会转发到上游官方 API。

### 管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST/PUT/DELETE | `/admin/channels[/{id}]` | 渠道 CRUD |
| PATCH | `/admin/channels/{id}/toggle` | 启用/禁用渠道 |
| POST | `/admin/channels/{id}/test` | 测试连通性 |
| GET/POST/PUT/DELETE | `/admin/api-keys[/{id}]` | API Key CRUD |
| GET/POST/PUT/DELETE | `/admin/model-groups[/{id}]` | 模型组 CRUD |
| GET/POST | `/admin/settings` | 读/写业务配置 |
| GET | `/admin/stats[/daily\|/today\|/overall\|/refresh]` | 统计数据 |
| GET | `/admin/requests` | 请求记录查询 |
| GET | `/admin/requests/{id}/{field}` | 读取请求 RAW 字段 |
| GET/POST | `/admin/whitelist` | 读/写 IP 白名单 |
| GET | `/admin/logs[/{filename}]` | 查看日志文件 |
| GET | `/admin/ui/{section}` | htmx 局部刷新片段 |

## 数据模型概览

### Channel

```python
class Channel(BaseModel):
    id: str                           # ch_{uuid4_hex[:8]}
    name: str                         # 渠道名称
    api_type: APIType                 # 上游 API 格式
    base_url: str                     # 上游 API 基础地址
    endpoint_url: str | None = None   # 可选，完整端点 URL（优先于 base_url）
    models_url: str | None = None     # 可选，模型列表 URL
    api_key: str                      # 上游 API 密钥
    models: list[str]                 # 支持的模型列表
    enabled: bool = True              # 是否启用
    weight: int = 1                   # 负载均衡权重
    priority: int = 1                 # 优先级（数字越小越优先）
    socks5_proxy: str | None = None   # SOCKS5 代理地址
    capabilities: dict | None = None  # 手动覆盖提供商能力
    anthropic_version: str | None = None           # Anthropic API 版本
    anthropic_version_policy: AnthropicVersionPolicy = "channel"
    anthropic_beta: str | None = None              # Anthropic Beta 标记
    anthropic_beta_policy: AnthropicBetaPolicy = "channel"
    allow_format_conversion: bool | None = None    # 是否允许跨格式转换
    created_at: str                   # 创建时间
```

### ApiKey

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

### ModelGroup

```python
class ModelGroup(BaseModel):
    id: str                           # mg_{uuid4_hex[:8]}
    name: str                         # 模型组名称（用于请求中的 model 字段）
    models: list[str]                 # 包含的模型列表
    fallback_order: list[str] = []    # Fallback 顺序
    enabled: bool = True              # 是否启用
    created_at: str                   # 创建时间
```

## 关键设计决策

### 缓存一致性

- `load_data()` / `load_api_keys()` 内置 5 秒 TTL 缓存
- 修改渠道或 API Key 数据时**必须走** `atomic_update_data(mutator)` / `atomic_update_api_keys(mutator)`：在锁内完成 read-modify-write 并同步缓存
- 直接覆盖 `channels.json` 文件会导致缓存与磁盘不一致，请求最多延迟 5 秒才能感知变更
- `save_data()` 仅在你已自行持锁或确定无竞态时使用

### 流式客户端不缓存

流式请求的 httpx client 通过 `create_stream_client()` 独立创建，在生成器的 `finally` 块中手动 `aclose()`，绝不可加入 `_clients` 缓存池，否则连接会被提前关闭或泄漏。

### 转换器状态机

每个 Converter 实例在流式转换时维护内部 `_stream_state`，一个实例只服务一次流式请求，不能复用。

### 线程安全

- `storage.py` 使用 `asyncio.Lock`（异步锁），懒加载创建确保绑定正确的事件循环
- `load_balancer.py` 使用 `asyncio.Lock`（异步锁），整个选择过程在锁内完成
- `config.py` 使用 `asyncio.Lock` 保护 `_settings` 字典

### 首包前错误处理

流式请求通过 `_prime_stream()` 消费首个 chunk，让连接建立阶段和首包前的错误能进入故障转移循环。如果上游连接成功但没有任何 SSE 输出，`_EmptyStreamError` 也会触发故障转移。

### 配置热更新

`config._CONFIG_SCHEMA` 中标 `requires_restart: True` 的项（`host` / `port` / `log_level`）保存后会在响应里返回 `needs_restart: true`，需重启才生效。其余配置保存后立即生效：
- `request_timeout` 变更会自动调用 `client.invalidate_all_clients()` 重建连接池
- `response_state_*` 变更会自动调用 `reload_responses_store()` 刷新
- `max_fail_count` / `cooldown_seconds` 变更会自动调用 `load_balancer.update_config()`

## 扩展点

### 添加新的 API 格式

1. 在 `models/api_types.py` 添加枚举值
2. 在 `converters/` 创建对应的转换器
3. 在 `proxy_core.py` 的 `CONVERTER_MAP` 注册转换关系
4. 在 `routers/` 创建新的代理路由
5. 在 `url_builder.py` 的 `_UPSTREAM_PATHS` 注册路径

### 添加新的提供商能力

1. 在 `capability_manager.py` 的 `ProviderCapabilities` 添加字段
2. 在 `infer_capabilities()` 添加 base_url 关键词匹配
3. 在 `apply_capability_filter()` 添加过滤逻辑

### 添加统计存储后端

实现 `stats.py` 中的接口，替换 SQLite 为其他存储。

---

**下一步**：[模块详解](modules.md) — 各模块的详细实现文档
