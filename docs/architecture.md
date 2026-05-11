# 架构设计

本文档介绍 LLM-Plug 的核心概念、系统架构和模块划分。

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

渠道代表一个上游 LLM 提供商的连接配置：

```json
{
  "id": "ch_xxxx",
  "name": "OpenAI 官方",
  "api_type": "openai-chat-completions",
  "base_url": "https://api.openai.com",
  "api_key": "sk-xxx",
  "models": ["gpt-4o", "gpt-4o-mini"],
  "enabled": true,
  "weight": 1,
  "priority": 1,
  "socks5_proxy": null
}
```

| 字段 | 说明 |
|------|------|
| `api_type` | 上游 API 格式 |
| `models` | 支持的模型列表 |
| `weight` | 负载均衡权重，数值越大分配越多请求 |
| `priority` | 优先级，数字越小优先级越高 |
| `socks5_proxy` | 可选 SOCKS5 代理地址 |

### 模型路由

请求到达时，根据请求体中的 `model` 字段匹配渠道：
1. 筛选所有 `enabled=true` 且 `models` 包含该模型的渠道
2. 按 `priority` 分组，优先使用高优先级组
3. 组内按 `weight` 加权轮询选择

### 负载均衡策略

```
1. 按 priority 分组（数字越小越优先）
2. 最高优先级组内加权轮询（weight）
3. 失败渠道自动冷却恢复（max_fail_count + cooldown_seconds）
4. 当前渠道失败时，降级到下一优先级组重试
```

## 请求处理流程

```
┌──────────┐  POST /v1/chat/completions  ┌──────────────────────┐
│  Client  │ ──────────────────────────▶  │  Router (proxy_base) │
│ (OpenAI) │                              │  1. 鉴权              │
└──────────┘                              │  2. 提取 model/stream │
                                          └──────────┬───────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │    proxy_core.py     │
                                          │  proxy_request()     │
                                          │                      │
                                          │  循环:               │
                                          │  1. 渠道选择(均衡器) │
                                          │  2. 请求体转换       │
                                          │  3. 发送上游         │
                                          │  4. 响应体转换       │
                                          │  5. 失败则重试下一个 │
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

1. **路由入口**：`proxy_base.py` 的工厂函数生成处理器，完成鉴权 → 解析请求体 → 调用 `proxy_request()`
2. **渠道选择**：从 storage 加载匹配 model 的已启用渠道，通过 `LoadBalancer.select_channel()` 选择
3. **请求转换**：根据客户端 API 类型与上游渠道类型的差异，选择对应 converter 转换请求体
4. **上游请求**：通过 httpx.AsyncClient 发送请求（支持 SOCKS5 代理）
5. **响应转换**：converter 将上游响应转换回客户端格式（非流式 JSON 或流式 SSE chunks）
6. **故障转移**：请求失败时记录故障，排除已试渠道后重新选择，直到成功或所有渠道耗尽

## 模块职责

```
llm-plug/
├── main.py              # FastAPI 入口：路由注册、静态文件、中间件
├── config.py            # 配置管理：环境变量读取
├── storage.py           # 存储层：JSON 文件读写（线程安全 + 缓存）
├── client.py            # HTTP 客户端：httpx 缓存池 + SOCKS5
├── proxy_core.py        # 代理核心：负载均衡调度、格式转换协调
├── stats.py             # 统计模块：PostgreSQL 请求记录
│
├── models/              # 数据模型
│   ├── api_types.py     # APIType 枚举
│   ├── channel.py       # Channel Pydantic 模型
│   ├── api_key.py       # ApiKey Pydantic 模型
│   └── model_group.py   # ModelGroup Pydantic 模型
│
├── routers/             # 路由层
│   ├── proxy_base.py    # 代理路由工厂（核心）
│   ├── proxy_*.py       # 三种代理端点
│   ├── admin.py         # 管理 API
│   └── auth.py          # 代理鉴权
│
├── converters/          # 格式转换器
│   ├── base.py          # 抽象基类
│   ├── to_chat.py       # → OpenAI Chat Completions
│   ├── to_response.py   # → OpenAI Response
│   └── to_anthropic.py  # → Anthropic Messages
│
├── balancer/            # 负载均衡
│   └── load_balancer.py # 优先级分组 + 加权轮询 + 健康检查
│
└── static/              # 管理界面
    └── index.html       # TailwindCSS 单页应用
```

## 转换矩阵

| 客户端格式 \ 上游格式 | openai-chat-completions | openai-response | anthropic |
|---|---|---|---|
| **openai-chat-completions** | 直通 | `ToResponseConverter` | `ToAnthropicConverter` |
| **openai-response** | `ToChatCompletionsConverter` | 直通 | `ToAnthropicConverter` |
| **anthropic** | `ToChatCompletionsConverter` | `ToResponseConverter` | 直通 |

**转换器特性**：
- 支持非流式（JSON）和流式（SSE）转换
- 流式转换通过内部状态机逐 chunk 处理
- 同格式请求直接透传，不做转换

### Responses → Chat Completions 支持边界

当客户端使用 `openai-response`，但上游渠道是 `openai-chat-completions` 时，代理会把可表达能力转换为 Chat Completions 请求，并把 Chat 响应转换回 Responses 结构。Chat Completions 无法承载的 Responses 托管能力不会被静默忽略。

| 能力 | 支持等级 | 说明 |
|------|----------|------|
| 文本输入/输出 | 完整 | `input`、`instructions`、`output_text` 双向转换 |
| function tools | 完整 | `tools[].function`、`tool_choice`、`function_call`、`function_call_output` 转换 |
| 图片输入 | 按上游能力 | `input_image` 转为 Chat `image_url` |
| 文件/音频输入 | 按上游能力 | 转为 Chat 内容块；上游不支持时应拒绝或按渠道能力处理 |
| 托管工具 | 不支持，显式错误 | `web_search`、`file_search`、`code_interpreter`、`computer_use`、`mcp` 等不会静默丢弃 |
| 状态历史 | 按上游能力处理 | 同格式 `openai-response` 上游透明透传 `previous_response_id`；Chat/Anthropic 等无原生 Responses 状态能力的上游由代理本地加载历史并展开 |

## API 端点

### 代理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat Completions 格式代理 |
| POST | `/v1/responses` | OpenAI Response 格式代理 |
| POST | `/v1/messages` | Anthropic Messages 格式代理 |
| GET | `/v1/models` | OpenAI 风格模型列表 |
| GET | `/v1/anthropic/models` | Anthropic 风格模型列表 |

`GET /v1/responses/{response_id}` 和 `DELETE /v1/responses/{response_id}` 是本地状态接口：它们只访问代理在 `data/responses_session/` 中保存的 response，不会按官方 Responses retrieve/delete 语义转发到上游。

### 管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST/PUT/DELETE | `/admin/channels[/{id}]` | 渠道 CRUD |
| PATCH | `/admin/channels/{id}/toggle` | 启用/禁用渠道 |
| POST | `/admin/channels/{id}/test` | 测试连通性 |
| GET/POST/PUT/DELETE | `/admin/api-keys[/{id}]` | API Key CRUD |
| GET | `/admin/stats` | 统计数据 |
| GET | `/admin/requests` | 请求记录查询 |

## 数据模型概览

### Channel

```python
class Channel(BaseModel):
    id: str
    name: str
    api_type: APIType
    base_url: str
    api_key: str
    models: list[str]
    enabled: bool = True
    weight: int = 1
    priority: int = 1
    socks5_proxy: str | None = None
    created_at: datetime
```

### ApiKey

```python
class ApiKey(BaseModel):
    id: str
    name: str
    key: str
    allowed_models: list[str] = []
    notes: str = ""
    request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    created_at: datetime
```

### ModelGroup

```python
class ModelGroup(BaseModel):
    id: str
    name: str
    models: list[str]
    fallback_order: list[str] = []
    created_at: datetime
```

## 关键设计决策

### 缓存一致性

所有修改渠道数据的操作必须通过 `storage.save_data()` 写入，它会同步更新内存缓存。直接修改 `channels.json` 文件会导致缓存与磁盘不一致，代理请求最多延迟 5 秒才能感知变更。

### 流式客户端不缓存

流式请求的 httpx client 独立创建，在生成器的 `finally` 块中手动 `aclose()`，不可加入缓存池。

### 转换器状态机

每个 Converter 实例在流式转换时维护内部 `_stream_state`，一个实例只服务一次流式请求，不能复用。

### 线程安全

- `storage.py` 使用 `threading.RLock`（可重入锁）
- `load_balancer.py` 使用 `asyncio.Lock`（异步锁）

两者不可混用。

## 扩展点

### 添加新的 API 格式

1. 在 `models/api_types.py` 添加枚举值
2. 在 `converters/` 创建对应的转换器
3. 在 `proxy_core.py` 的 `CONVERTER_MAP` 注册转换关系
4. 在 `routers/` 创建新的代理路由

### 添加新的负载均衡策略

修改 `balancer/load_balancer.py` 的 `select_channel()` 方法。

### 添加统计存储后端

实现 `stats.py` 中的接口，替换 PostgreSQL 为其他存储。

---

**下一步**：[模块详解](modules.md) — 各模块的详细实现文档
