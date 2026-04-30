# LLM-Plug 项目结构文档

> 本文档面向新入职同学，帮助你快速建立对项目全貌的认知。建议先通读本文件，再按需阅读 `docs/spec-*.md` 系列详细模块文档。

## 一句话概述

LLM-Plug 是一个 **LLM API 格式转换代理服务**：客户端用一种 API 格式发请求，服务端自动转换后转发给不同格式的上游 LLM 提供商，再把响应转换回来——对客户端完全透明。

## 支持的三种 API 格式

| 格式名 | 枚举值 | 代理端点 | 说明 |
|--------|--------|----------|------|
| OpenAI Chat Completions | `openai-chat-completions` | `POST /v1/chat/completions` | 最常见的 OpenAI 对话格式 |
| OpenAI Response | `openai-response` | `POST /v1/responses` | OpenAI 新版 Responses API |
| Anthropic Messages | `anthropic` | `POST /v1/messages` | Anthropic Claude 对话格式 |

## 目录结构

```
llm-plug/
├── main.py                 # FastAPI 应用入口，注册路由、挂载静态文件
├── config.py               # 配置管理（全部通过环境变量读取）
├── storage.py              # JSON 文件存储封装（线程安全 + 内存缓存 + 原子写入）
├── client.py               # HTTP 客户端管理（httpx AsyncClient 缓存池 + SOCKS5）
├── proxy_core.py           # 代理核心逻辑（负载均衡调度、格式转换、流式处理、故障转移）
├── serve_viewer.py         # 独立日志查看服务（端口 8080，与主服务分离）
├── pyproject.toml          # 项目依赖配置（uv 管理）
├── start.sh                # 启动脚本（自动 sync + run/debug 模式）
│
├── models/                 # 数据模型层
│   ├── api_types.py        # APIType 枚举定义（三种 API 格式）
│   └── channel.py          # Channel / ChannelCreate / ChannelUpdate Pydantic 模型
│
├── routers/                # FastAPI 路由层
│   ├── proxy_base.py       # 代理路由工厂函数 make_proxy_router()——核心！
│   ├── proxy_chat.py       # /v1/chat/completions 代理（调用工厂生成）
│   ├── proxy_response.py   # /v1/responses 代理（调用工厂生成）
│   ├── proxy_anthropic.py  # /v1/messages 代理（调用工厂生成）
│   ├── proxy_models.py     # /v1/models、/v1/anthropic/models 模型列表
│   ├── admin.py            # /admin 渠道 CRUD、连通性测试、日志查看
│   ├── auth.py             # 代理 API 鉴权（Bearer Token 校验）
│   └── proxy_errors.py     # OpenAI 风格错误响应构建
│
├── converters/             # API 格式转换器
│   ├── base.py             # BaseConverter 抽象基类
│   ├── to_chat.py          # 任意格式 → OpenAI Chat Completions
│   ├── to_response.py      # 任意格式 → OpenAI Response
│   └── to_anthropic.py     # 任意格式 → Anthropic Messages
│
├── balancer/               # 负载均衡器
│   └── load_balancer.py    # 优先级分组 + 平滑加权轮询 + 健康检查
│
├── static/                 # 静态前端文件（管理页面）
│   ├── index.html          # 渠道管理页面（TailwindCSS CDN）
│   ├── session-viewer.html # 日志会话查看器
│   └── stream-test.html    # 流式请求测试页面
│
├── data/                   # 数据存储目录（gitignored）
│   └── channels.json       # 渠道配置持久化存储
│
├── logs/                   # 调试日志目录（gitignored，JSONL 格式）
│
├── tests/                  # 测试目录
│   ├── test_e2e.py         # 端到端测试
│   ├── test_integration.py # 集成测试
│   ├── balancer/           # 负载均衡器单元测试
│   ├── converters/         # 转换器矩阵测试
│   ├── routers/            # 路由层测试
│   ├── streaming/          # 流式转换测试
│   └── fixtures/           # 测试固件（mock 渠道、各格式请求示例）
│
└── docs/                   # 文档目录
    ├── STRUCT.md           # 本文件——项目结构总览
    ├── spec-proxy-core.md  # 代理核心详细文档
    ├── spec-converters.md  # 转换器详细文档
    ├── spec-balancer.md    # 负载均衡器详细文档
    ├── spec-routers.md     # 路由层详细文档
    ├── spec-storage.md     # 存储层详细文档
    ├── spec-client.md      # HTTP 客户端详细文档
    ├── spec-models.md      # 数据模型详细文档
    └── spec-config.md      # 配置项详细文档
```

## 请求处理全流程

这是理解项目的关键——一个请求从进来到返回，经历了什么？

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

**详细步骤说明：**

1. **路由入口**：`proxy_base.py` 的 `make_proxy_router()` 工厂为三种端点各生成一个处理器。处理器做三件事：鉴权 → 解析请求体提取 `model`/`stream` → 调用 `proxy_core.proxy_request()`
2. **渠道选择**：`proxy_request()` 从 storage 加载匹配 model 的已启用渠道，通过 `LoadBalancer.select_channel()` 选一个
3. **格式转换（请求）**：根据客户端 API 类型与上游渠道类型的差异，选择对应 converter 进行请求体转换；同类型则直通
4. **上游请求**：通过 `client.py` 创建的 httpx.AsyncClient 发送请求（支持 SOCKS5 代理）
5. **格式转换（响应）**：converter 将上游响应转换回客户端格式（非流式 JSON 或流式 SSE chunks）
6. **故障转移**：请求失败时记录故障到 balancer，排除已试渠道后重新选择，直到成功或所有渠道耗尽

## 核心模块速览

### 1. 入口与配置

| 文件 | 职责 | 详细文档 |
|------|------|----------|
| `main.py` | FastAPI 应用创建、路由注册、静态文件挂载、lifespan 管理 | - |
| `config.py` | 环境变量读取，集中管理所有配置项 | [spec-config.md](spec-config.md) |

### 2. 数据模型

| 文件 | 职责 | 详细文档 |
|------|------|----------|
| `models/api_types.py` | `APIType` 枚举：定义三种 API 格式 | [spec-models.md](spec-models.md) |
| `models/channel.py` | `Channel` / `ChannelCreate` / `ChannelUpdate` Pydantic 模型 | [spec-models.md](spec-models.md) |

### 3. 存储层

| 文件 | 职责 | 详细文档 |
|------|------|----------|
| `storage.py` | JSON 文件读写封装，线程安全（RLock），内存缓存（5s TTL），原子写入 | [spec-storage.md](spec-storage.md) |

### 4. HTTP 客户端

| 文件 | 职责 | 详细文档 |
|------|------|----------|
| `client.py` | httpx.AsyncClient 缓存池、流式客户端创建、上游认证头构建 | [spec-client.md](spec-client.md) |

### 5. 路由层

| 文件 | 端点 | 详细文档 |
|------|------|----------|
| `routers/proxy_base.py` | 工厂函数 `make_proxy_router()` | [spec-routers.md](spec-routers.md) |
| `routers/proxy_chat.py` | `/v1/chat/completions` | [spec-routers.md](spec-routers.md) |
| `routers/proxy_response.py` | `/v1/responses` | [spec-routers.md](spec-routers.md) |
| `routers/proxy_anthropic.py` | `/v1/messages` | [spec-routers.md](spec-routers.md) |
| `routers/proxy_models.py` | `/v1/models`、`/v1/anthropic/models` | [spec-routers.md](spec-routers.md) |
| `routers/admin.py` | `/admin/channels` CRUD + 测试 + 日志 | [spec-routers.md](spec-routers.md) |
| `routers/auth.py` | 代理 API Bearer Token 鉴权 | [spec-routers.md](spec-routers.md) |
| `routers/proxy_errors.py` | OpenAI 风格错误响应 | [spec-routers.md](spec-routers.md) |

### 6. 代理核心

| 文件 | 职责 | 详细文档 |
|------|------|----------|
| `proxy_core.py` | 协调所有模块：渠道筛选、负载均衡、格式转换、流式处理、故障转移、调试日志 | [spec-proxy-core.md](spec-proxy-core.md) |

### 7. 转换器

| 文件 | 目标格式 | 详细文档 |
|------|----------|----------|
| `converters/base.py` | 抽象基类 | [spec-converters.md](spec-converters.md) |
| `converters/to_chat.py` | → OpenAI Chat Completions | [spec-converters.md](spec-converters.md) |
| `converters/to_response.py` | → OpenAI Response | [spec-converters.md](spec-converters.md) |
| `converters/to_anthropic.py` | → Anthropic Messages | [spec-converters.md](spec-converters.md) |

**转换矩阵**（入口格式 → 上游格式需要哪个 Converter）：

| 客户端格式 \ 上游格式 | openai-chat-completions | openai-response | anthropic |
|---|---|---|---|
| **openai-chat-completions** | 直通 | `ToResponseConverter` | `ToAnthropicConverter` |
| **openai-response** | `ToChatCompletionsConverter` | 直通 | `ToAnthropicConverter` |
| **anthropic** | `ToChatCompletionsConverter` | `ToResponseConverter` | 直通 |

### 8. 负载均衡器

| 文件 | 职责 | 详细文档 |
|------|------|----------|
| `balancer/load_balancer.py` | 优先级分组 + 平滑加权轮询（SWRR）+ 渠道健康检查 | [spec-balancer.md](spec-balancer.md) |

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `DATA_DIR` | 项目根目录下 `data/` | 数据存储目录 |
| `CHANNELS_FILE` | `DATA_DIR/channels.json` | 渠道配置文件路径 |
| `MAX_FAIL_COUNT` | `5` | 连续失败 N 次后标记渠道不健康 |
| `COOLDOWN_SECONDS` | `60` | 不健康渠道冷却恢复时间（秒） |
| `REQUEST_TIMEOUT` | `300` | 上游请求超时时间（秒） |
| `PROXY_API_KEY` | (空) | 代理 API 密钥，空则不鉴权 |
| `DEBUG` | `false` | 调试日志开关 |
| `DEBUG_LOG_DIR` | 项目根目录下 `logs/` | 调试日志目录 |

## API 端点汇总

### 代理接口（客户端使用）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat Completions 格式代理 |
| POST | `/v1/responses` | OpenAI Response 格式代理 |
| POST | `/v1/messages` | Anthropic Messages 格式代理 |
| GET | `/v1/models` | OpenAI 风格模型列表 |
| GET | `/v1/anthropic/models` | Anthropic 风格模型列表（带分页） |

### 管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/channels` | 获取所有渠道（API Key 脱敏） |
| POST | `/admin/channels` | 添加渠道 |
| PUT | `/admin/channels/{id}` | 更新渠道 |
| DELETE | `/admin/channels/{id}` | 删除渠道 |
| PATCH | `/admin/channels/{id}/toggle` | 启用/禁用渠道 |
| POST | `/admin/channels/{id}/test` | 测试渠道连通性 |
| GET | `/admin/logs` | 列出日志文件 |
| GET | `/admin/logs/{filename}` | 获取日志文件内容 |

### 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 重定向到管理页面 `/static/index.html` |
| GET | `/static/*` | 静态文件 |
| GET | `/docs` | FastAPI 自动生成的 Swagger API 文档 |

## 依赖

| 包 | 用途 |
|----|------|
| `fastapi` | Web 框架 |
| `uvicorn` | ASGI 服务器 |
| `httpx[socks]` | 异步 HTTP 客户端（支持 SOCKS5 代理） |
| `pydantic` | 数据验证与序列化 |
| `python-socks[asyncio]` | SOCKS5 代理底层支持 |
| `ruff` | (开发) 代码检查 |
| `pytest` + `pytest-asyncio` | (测试) 测试框架 |

## 快速启动

```bash
# 1. 安装依赖
uv sync

# 2. 启动服务（开发模式，带热重载）
uv run python main.py

# 3. 或使用启动脚本
./start.sh run     # 正常启动
./start.sh debug   # 调试模式（热重载 + uvicorn trace 日志）

# 4. 打开管理页面
# 浏览器访问 http://localhost:8000/

# 5. 添加一个渠道后即可开始使用代理
```

## 关键注意事项

1. **缓存一致性**：所有修改渠道数据的操作必须通过 `storage.save_data()` 写入（它会同步更新内存缓存），切勿直接写 `channels.json` 文件，否则缓存与磁盘不一致，代理请求最多延迟 5 秒才能感知变更。
2. **流式客户端不缓存**：流式请求的 httpx client 是独立创建的（不经缓存池），在生成器 `finally` 块中手动 `aclose()`，不可加入 `_clients` 缓存池，否则会导致连接被提前关闭或泄漏。
3. **Converter 状态机**：每个 Converter 实例在流式转换时内部维护 `_stream_state`，一个实例只服务一次流式请求，不能复用。
4. **一个 chunk 可能产生多个事件**：转换器通过 `_extra_events` 字段传递额外事件，由 `get_extra_events()` 取出。Anthropic SSE 格式有 `event:` 行，OpenAI 格式仅有 `data:` 行。
5. **Thread Safety**：`storage.py` 使用 `threading.RLock`（可重入锁）；`load_balancer.py` 使用 `asyncio.Lock`（异步锁）。注意两者不可混用。
