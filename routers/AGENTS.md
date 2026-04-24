<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-24 | Updated: 2026-04-24 -->

# routers

## Purpose
FastAPI 路由模块，包含管理 API 和三种代理 API 的路由实现。

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | 模块初始化 (空文件) |
| `admin.py` | 管理API：渠道CRUD、启用/禁用、连通性测试 |
| `proxy_chat.py` | OpenAI Chat Completions 代理路由 `/v1/chat/completions` |
| `proxy_response.py` | OpenAI Response 代理路由 `/v1/responses` |
| `proxy_anthropic.py` | Anthropic Messages 代理路由 `/v1/messages` |
| `proxy_models.py` | 模型列表 API：`/v1/models` 和 `/v1/anthropic/models` |

## Subdirectories
无

## For AI Agents

### Working In This Directory
- 新增代理路由需在 `main.py` 中注册
- 代理路由共享 `_check_auth` 鉴权函数
- 管理路由使用 `/admin` 前缀
- 流式响应使用 StreamingResponse + SSE 格式

### Testing Requirements
- 测试各路由的请求/响应格式
- 验证鉴权逻辑 (PROXY_API_KEY)
- 测试流式响应的正确传输

### Common Patterns
- APIRouter 模块化组织路由
- Header 参数获取 Authorization
- 异常处理返回统一错误格式

## Dependencies

### Internal
- `models/` - Channel, ChannelCreate, ChannelUpdate, APIType
- `proxy_core.py` - proxy_request 核心代理逻辑
- `storage.py` - load_data, save_data 数据持久化
- `config.py` - PROXY_API_KEY 鉴权配置
- `client.py` - create_client, get_upstream_headers

### External
- `fastapi` - APIRouter, Header, Request, StreamingResponse

<!-- MANUAL: -->
