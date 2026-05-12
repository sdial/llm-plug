<!-- Generated: 2026-04-24 | Updated: 2026-04-27 -->

# llm-plug

## Purpose
LLM API 转换器 - 一个将不同大模型API格式互转的代理服务。支持 OpenAI Chat Completions、OpenAI Response 和 Anthropic 三种格式之间的相互转换，并提供负载均衡、故障转移和渠道管理功能。

## Commands

| Command | Description |
|---------|-------------|
| `uv sync` | 安装项目依赖 |
| `uv run python main.py` | 启动服务（带热重载），默认监听 0.0.0.0:8000 |
| `./start.sh run` | 通过启动脚本启动（首次自动执行 `uv sync`） |
| `./start.sh debug` | 调试模式启动（热重载 + uvicorn trace 日志） |
| `uv run ruff check .` | 代码检查 |
| `uv run ruff check . --fix` | 代码检查并自动修复 |
| `uv run pytest` | 运行测试（`tests/` 目录下已有单元及集成测试） |

### 环境变量
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `DATA_DIR` | 项目根目录下 `data/` | 数据目录（基于 `os.path.dirname(__file__)` 解析） |
| `CHANNELS_FILE` | 项目根目录下 `data/channels.json` | 渠道存储文件（基于 `DATA_DIR` 解析） |
| `MAX_FAIL_COUNT` | `5` | 连续失败剔除阈值 |
| `COOLDOWN_SECONDS` | `60` | 渠道冷却恢复时间(秒) |
| `PROXY_API_KEY` | (空) | 代理API密钥，空则不鉴权 |

## Architecture

### 请求处理流程
客户端请求到达代理端点后，经过以下流程：

1. **路由入口** — `routers/proxy_base.py` 的 `make_proxy_router()` 工厂函数为三种代理端点生成统一处理器：鉴权 → 提取 `model`/`stream` → 调用 `proxy_core`
2. **渠道选择** — `proxy_core.proxy_request()` 从 storage 加载匹配 model 的已启用渠道，通过 `balancer.LoadBalancer` 选择渠道（优先级分组 + 加权轮询）
3. **格式转换** — 根据入口 API 类型与上游渠道类型的差异，选择对应 converter 进行请求体转换；同类型则直通
4. **上游请求** — 通过 `client.py` 创建的 httpx.AsyncClient（支持 SOCKS5 代理）发送请求
5. **响应转换** — converter 将上游响应（非流式 JSON 或流式 SSE chunks）转换回入口格式
6. **故障转移** — 请求失败时记录故障到 balancer，排除已试渠道后重新选择

### 核心模块

**`proxy_core.py`** — 代理核心，协调所有其他模块。`proxy_request()` 实现故障转移循环：不断选择渠道并尝试请求，直到成功或所有渠道耗尽。`_do_stream_request()` 是异步生成器，逐行解析 SSE 并通过 converter 逐 chunk 转换。**注意**：流式请求的 httpx client 是独立创建的（不经缓存池），在生成器 `finally` 块中手动 `aclose()`，不可加入 `_clients` 缓存池，否则会导致连接被提前关闭或泄漏。

**`converters/`** — 三种格式转换器，均继承 `base.BaseConverter`，实现 `convert_request`/`convert_response`/`convert_stream_chunk` 三个抽象方法。每个 converter 内部按 `source_type` 分派到具体的 `_xxx_request_to_yyy()` 私有方法。转换矩阵：

| 入口格式 \ 上游格式 | openai-chat-completions | openai-response | anthropic |
|---|---|---|---|
| **openai-chat-completions** | 直通 | `ToChatCompletionsConverter` | `ToChatCompletionsConverter` |
| **openai-response** | `ToResponseConverter` | 直通 | `ToResponseConverter` |
| **anthropic** | `ToAnthropicConverter` | `ToAnthropicConverter` | 直通 |

流式转换时，converter 内部维护 `_stream_state` 状态机跟踪消息 ID、tool call index 等，一个上游 chunk 可能产生多个下游事件（通过 `_extra_events` 字段传递，由 `get_extra_events()` 取出）。Anthropic SSE 格式有 `event:` 行，OpenAI 格式仅有 `data:` 行。

**`routers/`** — `proxy_base.py` 定义 `make_proxy_router(path, api_type)` 工厂函数，三个代理路由模块各调用一次生成端点。`admin.py` 提供渠道 CRUD + 测试连通性 + 日志查看。`proxy_models.py` 聚合已启用渠道的模型列表。`auth.py` 校验 Bearer Token。`proxy_errors.py` 将 httpx 异常映射为 HTTP 错误响应。

**`balancer/load_balancer.py`** — `LoadBalancer` 单例实现优先级分组 + 平滑加权轮询（类似 Nginx SWRR）。`ChannelHealth` 跟踪每个渠道的连续失败次数，超过 `MAX_FAIL_COUNT` 则标记不健康，冷却 `COOLDOWN_SECONDS` 后恢复探测。

**`storage.py`** — JSON 文件读写，5 秒 TTL 内存缓存 + `threading.Lock` 线程安全。写入使用原子操作（先写临时文件再 `os.replace`）。`save_data()` 后自动更新缓存。**重要**：所有修改渠道数据的操作必须通过 `save_data()` 写入（它会同步更新内存缓存），切勿直接写 `channels.json` 文件，否则缓存与磁盘不一致，代理请求最多延迟 5 秒才能感知变更。

**`client.py`** — httpx.AsyncClient 管理器。普通请求使用带缓存的 `create_client()`（按 base_url+proxy 缓存）；流式请求每次新建客户端（`create_stream_client()`，不缓存）。`get_upstream_headers()` 根据 api_type 构造上游认证头（Anthropic 用 `x-api-key` + `anthropic-version`，OpenAI 用 `Authorization: Bearer`）。

**`models/`** — `APIType` 枚举定义三种 API 格式；`Channel`/`ChannelCreate`/`ChannelUpdate` Pydantic 模型定义渠道数据结构（含 weight、priority、socks5_proxy 字段）。

### 管理页面
`static/` 目录下为原生 HTML + TailwindCSS (CDN) 的管理页面，根路径 `/` 重定向到 `/static/index.html`。`serve_viewer.py` 是独立的日志查看服务（端口 8080），与主服务分离运行。

### API 端点

**代理 API**（请求体与对应官方 API 一致，转换器自动根据渠道类型进行格式转换）：
- `POST /v1/chat/completions` — OpenAI Chat Completions 格式
- `POST /v1/responses` — OpenAI Response 格式
- `POST /v1/messages` — Anthropic Messages 格式
- `GET /v1/models` — 聚合模型列表（OpenAI 格式）
- `GET /v1/anthropic/models` — 聚合模型列表（Anthropic 格式，带分页）

**管理 API**（前缀 `/admin`）：
- `GET /admin/channels` / `POST /admin/channels` / `PUT /admin/channels/{id}` / `DELETE /admin/channels/{id}` — 渠道 CRUD
- `PATCH /admin/channels/{id}/toggle` — 启用/禁用切换
- `POST /admin/channels/{id}/test` — 测试渠道连通性
- `GET /admin/logs` / `GET /admin/logs/{filename}` — 日志查看

<!-- MANUAL: -->
