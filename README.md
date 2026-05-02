# 一个 LLM API 转换器

## 用途
- 通过 web 添加大模型 API 渠道和模型列表，支持 openai-chat-completions、openai-response、anthropic
- 通过转换器转换成三种不同的 API 服务（openai-chat-completions、openai-response、anthropic）
- 从而让任一格式的 API 都能变成三种不同的 API

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端 | Python 3.11+ / FastAPI | 异步高性能API框架，支持SOCKS5代理,使用`uv run python main.py`启动 |
| 前端 | 原生HTML + TailwindCSS (CDN) | 零构建依赖，轻量配置页面 |
| 存储 | JSON文件 | 零数据库依赖，部署简单 |
| HTTP客户端 | httpx[socks] | 异步HTTP请求，支持SOCKS5代理 |
| 进程管理 | uvicorn | ASGI服务器 |
| SOCKS5支持 | httpx-socks / python-socks | SOCKS5代理连接 |

## 项目结构

```
/workspace
├── main.py                  # 入口文件，FastAPI应用
├── config.py                # 配置管理（端口、存储路径等）
├── storage.py               # JSON文件读写封装
├── client.py                # HTTP客户端创建（含SOCKS5代理支持）
├── proxy_core.py            # 代理请求核心逻辑（负载均衡+故障转移+格式转换）
├── models/
│   ├── __init__.py
│   ├── channel.py           # 渠道数据模型
│   └── api_types.py         # API类型枚举与定义
├── routers/
│   ├── __init__.py
│   ├── admin.py             # 管理API（渠道CRUD、模型管理）
│   ├── proxy_chat.py        # OpenAI Chat Completions 代理
│   ├── proxy_response.py    # OpenAI Response 代理
│   └── proxy_anthropic.py   # Anthropic 代理
├── converters/
│   ├── __init__.py
│   ├── base.py              # 转换器基类
│   ├── to_chat.py           # 任意格式 → OpenAI Chat Completions
│   ├── to_response.py       # 任意格式 → OpenAI Response
│   └── to_anthropic.py      # 任意格式 → Anthropic
├── balancer/
│   ├── __init__.py
│   └── load_balancer.py     # 负载均衡策略（轮询/加权/最少连接）
├── static/
│   └── index.html           # 管理页面（内嵌TailwindCSS）
├── data/
│   ├── channels.json        # 渠道与模型存储文件
│   └── api_keys.json        # API Keys 存储文件
└── pyproject.toml           # uv项目配置与依赖
```

## 数据模型

### 渠道 (Channel)
```json
{
  "id": "ch_xxxx",
  "name": "我的OpenAI渠道",
  "api_type": "openai-chat-completions",
  "base_url": "https://api.openai.com",
  "api_key": "sk-xxx",
  "models": ["gpt-4o", "gpt-4o-mini"],
  "enabled": true,
  "weight": 1,
  "priority": 1,
  "socks5_proxy": "socks5://user:pass@127.0.0.1:1080",
  "created_at": "2026-01-01T00:00:00Z"
}
```

`api_type` 枚举值：`openai-chat-completions` | `openai-response` | `anthropic`

| 字段 | 类型 | 说明 |
|------|------|------|
| `weight` | int | 负载均衡权重，默认1，数值越大分配越多请求 |
| `priority` | int | 优先级，数字越小优先级越高，同优先级内按权重负载均衡 |
| `socks5_proxy` | string? | 可选，SOCKS5代理地址，格式 `socks5://[user:pass@]host:port`，为空则直连 |

### 模型路由规则
- 请求到达时，根据 `model` 字段匹配渠道
- 支持同一模型名映射到多个渠道（负载均衡/故障转移）

### 负载均衡策略
同一模型匹配到多个可用渠道时，按以下规则选择：

1. **按优先级分组**：先按 `priority` 排序，优先使用高优先级（数字小）的渠道组
2. **组内加权轮询**：同一优先级内，按 `weight` 加权轮询分配请求
3. **故障转移**：当前渠道请求失败时，自动降级到下一优先级组重试
4. **健康检查**：渠道连续失败N次后自动暂时剔除，定时恢复探测

## 开发计划

### Phase 1: 基础框架
- [x] 初始化项目结构、依赖文件
- [x] 实现 `storage.py` JSON读写
- [x] 实现数据模型 `Channel`、`APIType`（含socks5_proxy/weight/priority字段）
- [x] 实现 `config.py` 配置管理

### Phase 2: 管理后台
- [x] 实现渠道CRUD API (`/admin/channels`)
- [x] 实现模型列表管理 API
- [x] 实现 `static/index.html` 管理页面
  - 渠道列表展示（启用/禁用切换）
  - 添加/编辑/删除渠道表单（含SOCKS5代理、权重、优先级配置）
  - 模型列表管理

### Phase 3: 负载均衡
- [x] 实现 `balancer/load_balancer.py` 负载均衡器
  - 加权轮询算法
  - 优先级分组选择
  - 渠道健康状态追踪（失败计数、冷却恢复）
- [x] 实现SOCKS5代理HTTP客户端创建（根据渠道socks5_proxy配置）

### Phase 4: API代理核心
- [x] 实现转换器基类与接口定义
- [x] 实现 `openai-chat-completions` → 三种格式转换
- [x] 实现 `openai-response` → 三种格式转换
- [x] 实现 `anthropic` → 三种格式转换
- [x] 实现流式响应(SSE)转发（兼容SOCKS5代理）

### Phase 5: 代理路由
- [x] 实现 `/v1/chat/completions` 代理路由
- [x] 实现 `/v1/responses` 代理路由
- [x] 实现 `/v1/messages` (Anthropic) 代理路由
- [x] 实现模型路由（按model匹配渠道 + 负载均衡选择）
- [x] 实现故障转移（主渠道失败自动切换备选渠道）

### Phase 6: 完善与部署
- [x] 错误处理与日志
- [x] 代理API Key认证（`PROXY_API_KEY` 已生效）
- [x] ~~管理API Key认证~~（已移除，管理接口无需认证）
- [x] Dockerfile
- [x] 基础测试（`tests/` 已覆盖转换器、路由、负载均衡等）

## API端点设计

### 管理API
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /admin/channels | 获取所有渠道 |
| POST | /admin/channels | 添加渠道 |
| PUT | /admin/channels/{id} | 更新渠道 |
| DELETE | /admin/channels/{id} | 删除渠道 |
| PATCH | /admin/channels/{id}/toggle | 启用/禁用渠道 |

### 代理API
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /v1/chat/completions | OpenAI Chat Completions 格式 |
| POST | /v1/responses | OpenAI Response 格式 |
| POST | /v1/messages | Anthropic Messages 格式 |

所有代理API的请求体与对应官方API一致，转换器自动根据渠道类型进行格式转换。

## 快速开始

```bash
# 安装依赖（使用uv）
uv sync

# 启动服务
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 访问管理页面
# http://localhost:8000/

# API文档（自动生成）
# http://localhost:8000/docs
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| HOST | 0.0.0.0 | 监听地址 |
| PORT | 8000 | 监听端口 |
| DATA_DIR | ./data | 数据目录 |
| CHANNELS_FILE | ./data/channels.json | 渠道存储文件 |
| API_KEYS_FILE | ./data/api_keys.json | API Keys 存储文件 |
| MAX_FAIL_COUNT | 5 | 连续失败剔除阈值 |
| COOLDOWN_SECONDS | 60 | 渠道冷却恢复时间 |
| REQUEST_TIMEOUT | 300 | 上游请求超时时间（秒） |
| LOG_LEVEL | info | 日志级别 |
| PROXY_API_KEY | (空) | 代理API密钥，空则不鉴权 |
| DEBUG | false | 调试日志开关 |
| DEBUG_LOG_DIR | ./logs | 调试日志目录，输出 JSONL 格式 |

## Cherry Studio 兼容性说明

本代理采用**透传模式**，请求体中的参数会原样传递给上游 API。以下参数的支持情况取决于上游服务商：

| 参数 | 说明 | 兼容性 |
|------|------|--------|
| 数组格式的 message content | 多模态消息（文本+图片等） | ✅ 透传，取决于上游 |
| Developer Message | `role: "developer"` 消息 | ✅ 透传，取决于上游 |
| stream_options | 流式选项（如 `include_usage`） | ✅ 透传，取决于上游 |
| service_tier | 服务层级参数 | ✅ 透传，取决于上游 |
| enable_thinking | 思考模式（Claude 等） | ✅ 透传，取决于上游 |
| verbosity | 输出详细程度 | ✅ 透传，取决于上游 |

**注意**：这些参数是否生效完全取决于您配置的上游渠道是否支持。例如：
- OpenAI 渠道支持 `stream_options`、`service_tier` 等
- Anthropic 渠道支持 `enable_thinking`（thinking 模式）
- 其他第三方渠道请参考其官方文档

### 配置建议

在 Cherry Studio 中配置时：
1. **API 地址**：`http://127.0.0.1:8000`
2. **API Key**：任意值（默认不鉴权），或设置 `PROXY_API_KEY` 环境变量后使用对应值
3. **模型名称**：必须与 `channels.json` 中配置的模型名称完全一致
