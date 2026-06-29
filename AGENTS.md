# llm-plug

## Purpose
LLM API 转换代理 — 把 OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 三种格式在客户端与上游之间互转，配负载均衡、故障转移、模型组 Fallback 和可视化渠道管理。零 `.env` 配置，默认监听 `0.0.0.0:55555`。

## Commands

| Command | Description |
|---------|-------------|
| `uv sync` | 安装项目依赖 |
| `uv run python main.py` | 启动服务（默认热重载） |
| `uv run python main.py --no-reload` | Windows 推荐，避免热重载退出后端口短暂占用 |
| `./start.sh run` | 生产式启动（`uvicorn` 无 reload；首次自动 `uv sync`） |
| `./start.sh debug` | 调试模式（reload + uvicorn trace） |
| `./kill_port.sh 55555` | 强制释放端口（Windows 用 `taskkill`，否则用 `lsof`） |
| `uv run pytest` | 跑全部测试 |
| `uv run pytest tests/converters/test_converter_matrix.py -v` | 跑单个测试文件 |
| `uv run pytest -k test_name` | 按名字匹配跑测试 |
| `uv run ruff check .` / `uv run ruff check . --fix` | lint / 自动修复 |
| `uv run ruff format .` / `uv run ruff format . --check` | 格式化 / 检查（不修改） |

## 数据存储

所有运行时数据落在项目根 `data/` 下，由 `config.DATA_DIR` 指向：

| 文件 | 用途 |
|------|------|
| `data/channels.json` | 渠道 + 模型组 (`model_groups`) + 已废弃 `lb_config`（启动时迁移到 `settings.json`） |
| `data/api_keys.json` | 客户端访问 key（Bearer 或 `x-api-key` 头） |
| `data/settings.json` | 前端设置页保存的业务配置（超时、体积上限、LB 阈值、时区等） |
| `data/admin_auth.json` | 管理员密码哈希（PBKDF2-SHA256 260k 轮）+ 已撤销会话 |
| `data/whitelist.csv` | IP 白名单（`path_pattern,methods,cidr,desc` 四列 CSV） |
| `data/stats.db` | SQLite 统计聚合（按渠道/模型/天） |
| `data/request_logs.db` | SQLite 请求记录（按月分库） |
| `data/responses_session/` | Responses API 的会话状态文件（`previous_response_id` 展开依赖） |
| `logs/images/` | 请求中的图片文件（需开启 `save_images` 设置） |
| `logs/audios/` | 请求中的音频文件（需开启 `save_audios` 设置） |
| `logs/files/` | 请求中的其他文件（需开启 `save_files` 设置） |

`storage.load_data()` / `load_api_keys()` 内置 5 秒 TTL 缓存。**修改渠道或 API Key 数据时必须走 `atomic_update_data(mutator)` / `atomic_update_api_keys(mutator)`**（在锁内完成 read-modify-write 并同步缓存）；直接覆盖 `channels.json` 文件会导致缓存与磁盘不一致，请求最多延迟 5 秒才能感知变更。`save_data()` 仅在你已自行持锁或确定无竞态时使用。

`config._CONFIG_SCHEMA` 中标 `requires_restart: True` 的项（`host` / `port` / `log_level`）保存后会立刻在响应里 `needs_restart: true`，但内存里 `LOG_LEVEL` 需重启或下次 `--log-level` 启动才生效。`request_timeout` 修改会自动调用 `client.invalidate_all_clients()` 重建连接池。`max_body_size` 和 `request_timeout` 均通过 `config.MAX_BODY_SIZE` / `config.REQUEST_TIMEOUT` 动态读取，设置页修改后立即生效，无需重启。

## 架构

### 请求流程
1. **入口** — `main.py:CombinedMiddleware`（纯 ASGI，不是 `BaseHTTPMiddleware`，避免流式 bug）：IP 白名单 → 鉴权 → 解析 body → 校验 `model` 是否在 `allowed_models` 列表里 → 写 `scope["state"]`
2. **代理路由** — `/v1/chat/completions` 和 `/v1/messages` 由 `routers/proxy_base.py:make_proxy_router()` 工厂生成，仅做格式分发；`/v1/responses` 在 `routers/proxy_response.py` 中有独立实现，额外处理 `previous_response_id` 历史展开和响应状态保存（`_save_response_state`）。路由层通过 `routers/auth.py:check_proxy_authorization()` 检查 `scope["state"]["proxy_auth_checked"]`，实际鉴权逻辑在 `CombinedMiddleware` 完成
3. **核心** — `proxy_core.proxy_request()`：解析模型组 → `_get_channels_for_model()` 拉取已启用渠道 → `_filter_channels_by_conversion()` 按目标格式与 `allow_format_conversion` 过滤 → `LoadBalancer.select_channel()` 选渠道 → `_do_request()` 执行
4. **转换** — `CONVERTER_MAP[(source, target)]` 选择 request/response converter；同格式直通
5. **能力过滤** — `capability_manager.apply_capability_filter()` 在 converter 之后、发送之前运行（作用于真实发往上游的 payload，同格式透传也必须应用）
6. **上游请求** — 非流式用 `client.create_client()` 缓存的 `httpx.AsyncClient`；流式用 `client.create_stream_client()` 新建客户端并在生成器 `finally` 中 `aclose()`（**绝不可加入 `_clients` 缓存池**，否则连接会被提前关闭或泄漏）
7. **故障转移** — 失败时 `load_balancer.record_failure()`，加进 `all_tried` 排除集重选。首包前切换条件：`_is_retryable_exception()`（网络异常、`_UpstreamStreamErrorEvent`、`_EmptyStreamError`、5xx/429、`ConverterError`）或 `_is_channel_config_error()`（上游 401/403/404，视为渠道配置问题）

**代理鉴权**：`api_keys.json` 为空时跳过 key 校验但仍设置 `proxy_auth_checked=True`（开放代理模式）；有 key 时须 `Authorization: Bearer xxx` 或 `x-api-key: xxx`，并按 `allowed_models` 限制模型。

### 转换矩阵
行 = 入口格式，列 = 上游格式。`ToXxxConverter` 把上游转回入口格式。

| 入口 \ 上游 | openai-chat-completions | openai-response | anthropic |
|---|---|---|---|
| openai-chat-completions | 直通 | `ToChatCompletionsConverter` | `ToChatCompletionsConverter` |
| openai-response | `ToResponseConverter` | 直通 | `ToResponseConverter` |
| anthropic | `ToAnthropicConverter` | `ToAnthropicConverter` | 直通 |

流式：converter 内部维护 `_stream_state`（消息 ID、tool_call index 等），一个上游 chunk 可能产出多个下游事件，通过 `_pending_extra_events` + `get_extra_events()` 取出。Anthropic 输出 SSE 含 `event:` 行，OpenAI 仅含 `data:` 行。

### 模型组 Fallback
`storage.get_model_group_by_name(model)` 在代理请求入口判断；若 `model` 是组名，`_proxy_model_group_request()` 按 `group.models` 顺序逐个模型尝试（每个模型内部仍走正常 LB + 故障转移）。模型组与渠道是两层 Fallback：先模型后渠道。

### Capability 管理
`capability_manager.infer_capabilities(channel, model_name)` 按 `base_url` 关键词识别提供商：含 `deepseek` → 关闭 `parallel_tool_calls` + 过滤 `💭` 思考块；含 `minimax` → 合并多条 system 消息为单条。`channel.capabilities` 字段可显式覆盖渠道级默认。`channel.model_capabilities` 字典可为单个模型覆盖多模态能力（`supports_image_content` / `supports_audio_content` / `supports_file_content`），解析优先级：`model_capabilities[model]` > `channel.capabilities` > vendor 推断 > 默认 False。`apply_capability_filter()` 在过滤多模态 content 时记录包含渠道名、模型名和被过滤类型的 warn 日志。`filter_think_content` 走 `think_filter.ThinkFilter` 状态机（流式逐 chunk 过滤，跨 chunk 保留 `💭...💭` 块边界）。

### 模块要点

- **`main.py`** — `lifespan` 启动时初始化 settings、stats DB、request log backend、stats/request-log worker；后台循环：Responses 会话清理、请求日志过期清理、HTTP 客户端 stale 清理；关闭时停 worker 并释放连接池
- **`proxy_core.py`** — `_do_stream_request()` 是异步生成器，逐行解析 SSE，由 `_prime_stream()` 消费首个 chunk 触发首包前错误进故障转移。`_EmptyStreamError` 处理"上游连接成功但无任何 SSE 输出"的情况。`_do_request()` 内的 `latency_ms` 起点是 `create_client` 返回之后（避免把连接建立时间算进去）
- **`client.py`** — 普通客户端按 `base_url|socks5_proxy` 缓存。`get_upstream_headers()` 对 Anthropic 发 `x-api-key` + `anthropic-version`（默认 `2023-06-01`）+ 可选 `anthropic-beta`；版本/beta 走 `AnthropicVersionPolicy` / `AnthropicBetaPolicy`（`channel` / `client` / `channel_if_missing` / `merge`）。`_apply_anthropic_headers()` 会从客户端 `extra_headers` 里 `pop` 走 `anthropic-version` 和 `anthropic-beta` 再按策略写回
- **`storage.py`** — 原子写：临时文件 + `os.replace()`。提供 `register_save_callback()` / `register_api_keys_save_callback()` 给缓存失效逻辑订阅；`proxy_core._schedule_invalidate_model_channels_cache()` 和 `storage._invalidate_model_groups_cache_sync()` 都通过它串联
- **`request_logs.py`** — 异步 worker 写入请求记录；SQLite 后端按月分库并支持 legacy DB 迁移；RAW 字段（headers/body/附件）受 settings 开关控制
- **`stats.py`** — SQLite 日聚合 + 后台 worker；管理端 `/admin/stats` 读聚合表，缺失时可 fallback 到 request_logs 实时统计
- **`routers/admin.py`** — `AdminAuthRoute` 在路由级加会话校验 + CSRF 校验（写操作要 `X-CSRF-Token` 头）。上游 URL 创建前走 `_validate_outbound_url()` 防 SSRF（拒绝非公网、内网、本机地址）
- **`balancer/load_balancer.py`** — 平滑加权轮询（SWRR，类似 Nginx）：`current_weight += weight` → 选最大 → 减去 `total_weight`。`ChannelHealth.fail_count` 内存存储，进程重启清零
- **`response_state.py`** — `FileStore`（`state_store.py`）基于 `data/responses_session/` 提供 LRU + TTL 淘汰；`reload_responses_store()` 在 `response_state_*` 设置变更后被 `config.update_settings()` 调用。代理通过 `previous_response_id` 把历史展开为 `input` 后再发上游（仅对不支持原生 Responses 状态的上游生效）
- **`whitelist.py`** — `WhitelistCache` 用文件 `mtime` 判断是否重新加载；`fnmatchcase` 匹配路径，`ipaddress.ip_network(strict=False)` 解析 CIDR。路径无匹配规则时默认放行；有规则则须 IP + 方法均匹配

### 前端与日志查看
- `static/` 原生 HTML + TailwindCSS（本地 `tailwind.min.js`）+ htmx（本地 `htmx.min.js`），零 CDN 依赖，支持离线部署。`/admin` 走 `index.html`，`/admin/login` 走 `admin-login.html`
- `static/fragments/admin/` 存放 htmx 局部刷新片段（`channels.html` / `apikeys.html` / `stats.html` / `requests.html` / `settings.html` / `whitelist.html` / `model-groups.html`），由 `/admin/ui/{section}` 返回
- 独立工具页：`/admin/request-analyzer`（请求分析）、`static/stream-test.html`、`static/json-viewer.html`、`static/session-viewer.html`
- `serve_viewer.py` 独立的会话查看服务（默认端口 8080，监听 `127.0.0.1`），与主服务分离运行
- 告警/错误日志落 `logs/{warning,error,critical}.log`（loguru 10MB 轮转），每条代理请求通过 `CombinedMiddleware` 写入 `[REQ]` / `[RES]` 文本日志

## API 端点

**代理（请求体与官方 API 一致，converter 自动按上游格式转换）**
- `POST /v1/chat/completions` — OpenAI Chat Completions
- `POST /v1/responses` — OpenAI Responses
- `POST /v1/messages` — Anthropic Messages
- `GET /v1/models` / `GET /v1/anthropic/models` — 聚合模型列表
- `GET /v1/responses/{id}` / `DELETE /v1/responses/{id}` — 读/删代理本地保存的 Responses 状态（**不**转发到上游官方 Responses API）

**管理（前缀 `/admin`，需登录会话，写操作需 `X-CSRF-Token`）**
- `/admin/auth/{status,csrf,setup,login,logout,setup-login}` — 鉴权流程（`setup-login` 合并首次设置与登录）
- `/admin/channels`（`GET`/`POST`）、`/admin/channels/{id}`（`PUT`/`DELETE`）、`/admin/channels/{id}/toggle`（`PATCH`）、`/admin/channels/{id}/test`（`POST`）、`/admin/channels/fetch-models`（`POST`）
- `/admin/api-keys` CRUD；`/admin/api-keys/{id}/key`（`GET` 查看明文）；`/admin/api-keys/{id}/regenerate`（`PATCH`）
- `/admin/model-groups` CRUD；`/admin/model-groups/{id}/toggle`（`PATCH`）
- `/admin/whitelist` 读/写 IP 白名单
- `/admin/settings` 读/写业务配置；`/admin/restart`（`POST` 触发重启）
- `/admin/lb-config` 读/写（兼容旧 API，数据已迁到 `settings.json` 的 `max_fail_count` / `cooldown_seconds`）
- `/admin/stats`（`GET`，返回 `overall` + `daily`）；`/admin/stats/today`（`GET` 当日实时）；`/admin/stats/refresh`（`POST`）；`/admin/stats/refresh/daily`（`POST`）；`/admin/stats/aggregate/daily`（`POST`）
- `/admin/requests`、`/admin/requests/{id}/{field}` — 日志查询 / 读 RAW 字段；`/admin/request-logs/cleanup`（`POST` 手动清理）
- `/admin/logs`、`/admin/logs/{filename}` — 查看 `logs/*.log`
- `/admin/ui/{section}` — htmx 片段

## 测试约定

- `tests/conftest.py` 提供 `e2e_mock_server`（session 级，启 `tests/mock_server.py` 在 19999 端口）和 `e2e_client`（TestClient + 清除 storage / proxy_core 缓存）两套 fixture
- `tests/admin_auth_utils.py:login_admin(client)` 一次性完成 setup + login + CSRF；管理端点测试必须先调用它
- 流式测试需 `LOG_LEVEL=debug` 才能在 `_do_stream_request` 看到 chunk 日志（前 20 个事件，`_STREAM_LOG_MAX = 20`）
- `tests/fixtures/` 含 `anthropic_request.json` / `openai_chat_request.json` / `openai_response_request.json` / `mock_channels.json`；`ANTHROPIC_STREAM_DATA` / `OPENAI_STREAM_DATA` 在 `mock_server.py`
- 异步测试依赖 `pytest-asyncio`（`pyproject.toml` dev 依赖）；`conftest.py` 提供 session 级 `event_loop` fixture，当前未显式配置 `asyncio_mode`
- `data/` 在测试期间可能被改：直接修改文件而不是通过 `atomic_update_data` 即可（仅在测试场景）

## 不要踩的坑

- **流式 httpx 客户端不要缓存** — `_do_stream_request()` 创建的 `create_stream_client()` 必须由生成器 `finally: await stream.aclose()` 释放；缓存到 `_clients` 会导致连接提前关闭
- **不要直接写 `channels.json`** — 走 `atomic_update_data()`，否则缓存最多 5 秒才更新
- **`CombinedMiddleware` 是纯 ASGI** — 显式选它是为了规避 `BaseHTTPMiddleware` 的流式 bug，新功能继续走 ASGI 中间件或路由级依赖
- **同格式 Anthropic 直通不走 converter** — 设计如此。Anthropic→Anthropic 时 URL 拼接、版本/beta 头由 `client._apply_anthropic_headers()` + `url_builder.build_upstream_url()` 直接处理；不要把同类型场景硬塞进 `ToAnthropicConverter`
- **能力过滤发生在转换之后** — `apply_capability_filter()` 作用于实际发往上游的格式，同格式透传时仍需应用
- **请求体大小** — 默认 10MB（`config.py`），`CombinedMiddleware` 提前校验并返回 413；设置页修改 `max_body_size` 后立即生效（中间件通过 `config.MAX_BODY_SIZE` 动态读取）
- **无 API Key 即开放代理** — `api_keys.json` 为空时不校验客户端 key，部署生产环境前务必配置 API Key
- **流式首包空** — `_prime_stream()` 触发 `_EmptyStreamError` 走故障转移；非流式空响应走另一条路径，参见 `proxy_core.py` 内注释
- **Windows 端口占用** — 热重载退出后端口可能短时间占用，研发改用 `--no-reload`；残留进程用 `./kill_port.sh 55555`
- **`LOG_LEVEL` 模块级** — `config.LOG_LEVEL` 不会在 `update_settings()` 后热生效，只在 `--log-level` 启动参数时赋值
