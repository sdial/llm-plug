# LLM-Plug 项目结构文档

## 项目概述

LLM-Plug 是一个 LLM API 转换代理服务，核心功能：
- 统一管理多个 LLM API 渠道（支持 OpenAI Chat Completions、OpenAI Response、Anthropic Messages 三种格式）
- 在三种 API 格式之间进行双向转换
- 负载均衡与故障转移
- SOCKS5 代理支持

## 目录结构

```
llm-plug/
├── main.py                 # FastAPI 应用入口
├── config.py               # 配置管理（环境变量读取）
├── storage.py              # JSON 文件存储封装（线程安全）
├── client.py               # HTTP 客户端创建（支持 SOCKS5）
├── proxy_core.py           # 代理核心逻辑（负载均衡、格式转换、流式处理）
├── pyproject.toml          # 项目依赖配置
│
├── models/                 # 数据模型层
│   ├── __init__.py
│   ├── api_types.py        # APIType 枚举定义
│   └── channel.py          # Channel 数据模型（Pydantic）
│
├── routers/                # FastAPI 路由层
│   ├── __init__.py
│   ├── admin.py            # 管理接口（渠道 CRUD、测试）
│   ├── auth.py             # 代理 API 鉴权
│   ├── proxy_chat.py       # /v1/chat/completions 代理
│   ├── proxy_response.py   # /v1/responses 代理
│   ├── proxy_anthropic.py  # /v1/messages 代理
│   ├── proxy_models.py     # /v1/models 模型列表
│   └── proxy_errors.py     # 错误响应构建
│
├── converters/             # API 格式转换器
│   ├── __init__.py
│   ├── base.py             # 转换器抽象基类
│   ├── to_chat.py          # 转换为 OpenAI Chat Completions
│   ├── to_response.py      # 转换为 OpenAI Response
│   └── to_anthropic.py     # 转换为 Anthropic Messages
│
├── balancer/               # 负载均衡器
│   ├── __init__.py
│   └── load_balancer.py    # 加权轮询 + 优先级分组 + 健康检查
│
├── static/                 # 静态前端文件
│   ├── index.html          # 管理页面（TailwindCSS CDN）
│   └── AGENTS.md           # 静态目录说明
│
├── data/                   # 数据存储目录（gitignored）
│   └── channels.json       # 渠道配置存储
│
├── logs/                   # 调试日志目录（gitignored）
│
└── docs/                   # 文档目录
    └── STRUCT.md           # 本文档
```

## 核心模块说明

### 1. 入口与配置

| 文件 | 职责 |
|------|------|
| `main.py` | FastAPI 应用创建、路由注册、静态文件挂载 |
| `config.py` | 环境变量读取：HOST、PORT、DATA_DIR、MAX_FAIL_COUNT、COOLDOWN_SECONDS、API_KEY |

### 2. 数据层

| 文件 | 职责 |
|------|------|
| `models/api_types.py` | `APIType` 枚举：`OPENAI_CHAT`、`OPENAI_RESPONSE`、`ANTHROPIC` |
| `models/channel.py` | `Channel` Pydantic 模型：渠道配置（id, name, api_type, base_url, api_key, models, weight, priority, socks5_proxy） |
| `storage.py` | JSON 文件读写封装，使用 `threading.Lock` 保证线程安全，原子写入（tempfile + os.replace） |

### 3. 路由层

| 文件 | 端点 | 功能 |
|------|------|------|
| `routers/admin.py` | `/admin/channels` | 渠道 CRUD、启用/禁用、连通性测试 |
| `routers/proxy_chat.py` | `/v1/chat/completions` | OpenAI Chat Completions 格式代理 |
| `routers/proxy_response.py` | `/v1/responses` | OpenAI Response 格式代理 |
| `routers/proxy_anthropic.py` | `/v1/messages` | Anthropic Messages 格式代理 |
| `routers/proxy_models.py` | `/v1/models`, `/v1/anthropic/models` | 模型列表查询 |
| `routers/auth.py` | - | `check_proxy_authorization()` 鉴权函数 |
| `routers/proxy_errors.py` | - | 错误响应构建：`unauthorized()`、`invalid_request()`、`bad_gateway()`、`gateway_timeout()` |

### 4. 转换器

所有转换器继承 `BaseConverter`，实现三个抽象方法：

| 方法 | 说明 |
|------|------|
| `convert_request(source_data, source_type)` | 将入口请求转换为上游 API 格式 |
| `convert_response(target_response, source_type)` | 将上游响应转换为入口 API 格式 |
| `convert_stream_chunk(chunk, source_type)` | 流式响应单块转换 |

转换矩阵：

| 目标格式 \ 源格式 | openai-chat-completions | openai-response | anthropic |
|-------------------|-------------------------|-----------------|-----------|
| **to_chat** | - | `_response_*` | `_anthropic_*` |
| **to_response** | `_chat_*` | - | `_anthropic_*` |
| **to_anthropic** | `_chat_*` | `_response_*` | - |

### 5. 负载均衡器

`balancer/load_balancer.py` - 全局单例 `load_balancer`

**选择策略**：
1. 过滤：排除禁用、不健康、已尝试的渠道
2. 分组：按 `priority` 排序，取最高优先级组
3. 选择：组内使用平滑加权轮询（Smooth Weighted Round-Robin）

**健康检查**：
- `ChannelHealth` 跟踪 `fail_count` 和 `last_fail_time`
- 连续失败 `MAX_FAIL_COUNT` 次后标记不健康
- 冷却 `COOLDOWN_SECONDS` 秒后恢复探测

### 6. 代理核心

`proxy_core.py` - 核心代理逻辑

**关键函数**：
- `proxy_request(model, request_data, target_api_type, is_stream)` → 主入口
- `_get_channels_for_model(model)` → 按模型名筛选可用渠道
- `_do_request(...)` → 执行单次请求（含格式转换）
- `_do_stream_request(...)` → 流式请求处理（SSE）
- `_log_debug(...)` → 调试日志记录（请求/响应完整记录）

**请求流程**：
```
客户端请求 → 路由层 → proxy_request()
                         ↓
              选择渠道（负载均衡）→ 转换请求格式 → 发送上游
                         ↓
              上游响应 → 转换响应格式 → 返回客户端
                         ↓
              失败 → 记录失败 → 重试下一渠道
```

## 数据流

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Client    │────▶│   Router     │────▶│ Proxy Core  │
│ (任一格式)   │     │ (鉴权+路由)   │     │ (负载均衡)   │
└─────────────┘     └──────────────┘     └──────┬──────┘
                                                │
                    ┌───────────────────────────┼───────────────────────────┐
                    ▼                           ▼                           ▼
           ┌───────────────┐          ┌───────────────┐          ┌───────────────┐
           │   Converter   │          │   Converter   │          │   Converter   │
           │  (格式转换)    │          │  (格式转换)    │          │  (格式转换)    │
           └───────┬───────┘          └───────┬───────┘          └───────┬───────┘
                   │                          │                          │
                   ▼                          ▼                          ▼
           ┌───────────────┐          ┌───────────────┐          ┌───────────────┐
           │   Upstream    │          │   Upstream    │          │   Upstream    │
           │ OpenAI Chat   │          │ OpenAI Resp   │          │   Anthropic   │
           └───────────────┘          └───────────────┘          └───────────────┘
```

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `DATA_DIR` | `./data` | 数据存储目录 |
| `CHANNELS_FILE` | `./data/channels.json` | 渠道配置文件 |
| `MAX_FAIL_COUNT` | `5` | 连续失败剔除阈值 |
| `COOLDOWN_SECONDS` | `60` | 渠道冷却恢复时间 |
| `ADMIN_API_KEY` | (空) | 管理 API 密钥 |
| `PROXY_API_KEY` | (空) | 代理 API 密钥 |
| `DEBUG` | `false` | 调试模式开关 |
| `DEBUG_LOG_DIR` | `./logs` | 调试日志目录 |

## 依赖

- `fastapi` - Web 框架
- `uvicorn` - ASGI 服务器
- `httpx[socks]` - 异步 HTTP 客户端（支持 SOCKS5）
- `pydantic` - 数据验证
- `python-socks[asyncio]` - SOCKS5 代理支持
- `ruff` - 开发依赖，代码检查

## 启动方式

```bash
uv sync                    # 安装依赖
uv run python main.py      # 启动服务（开发模式，热重载）
# 或
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

## API 端点汇总

### 管理接口
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/channels` | 获取所有渠道 |
| POST | `/admin/channels` | 添加渠道 |
| PUT | `/admin/channels/{id}` | 更新渠道 |
| DELETE | `/admin/channels/{id}` | 删除渠道 |
| PATCH | `/admin/channels/{id}/toggle` | 启用/禁用渠道 |
| POST | `/admin/channels/{id}/test` | 测试渠道连通性 |

### 代理接口
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat Completions 格式 |
| POST | `/v1/responses` | OpenAI Response 格式 |
| POST | `/v1/messages` | Anthropic Messages 格式 |
| GET | `/v1/models` | OpenAI 风格模型列表 |
| GET | `/v1/anthropic/models` | Anthropic 风格模型列表 |

### 其他
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 重定向到管理页面 |
| GET | `/static/*` | 静态文件 |
| GET | `/docs` | FastAPI 自动生成的 API 文档 |
