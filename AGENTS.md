<!-- Generated: 2026-04-24 | Updated: 2026-04-24 -->

# llm-plug

## Purpose
LLM API 转换器 - 一个将不同大模型API格式互转的代理服务。支持 OpenAI Chat Completions、OpenAI Response 和 Anthropic 三种格式之间的相互转换，并提供负载均衡、故障转移和渠道管理功能。

## Key Files
| File | Description |
|------|-------------|
| `main.py` | FastAPI 应用入口，注册路由和静态文件服务 |
| `config.py` | 配置管理，从环境变量读取端口、存储路径、负载均衡参数等 |
| `storage.py` | JSON 文件读写封装，线程安全的数据持久化 |
| `client.py` | HTTP 客户端创建，支持 SOCKS5 代理 |
| `proxy_core.py` | 代理请求核心逻辑，负载均衡+故障转移+格式转换 |
| `pyproject.toml` | uv 项目配置与依赖定义 |
| `start.sh` | 启动脚本，支持普通模式和调试模式 |
| `.python-version` | Python 版本锁定 (3.14) |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `models/` | 数据模型定义 (see `models/AGENTS.md`) |
| `routers/` | FastAPI 路由模块 (see `routers/AGENTS.md`) |
| `converters/` | API 格式转换器 (see `converters/AGENTS.md`) |
| `balancer/` | 负载均衡器 (see `balancer/AGENTS.md`) |
| `static/` | 前端管理页面 (see `static/AGENTS.md`) |
| `data/` | JSON 数据存储 (see `data/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- 使用 `uv run python main.py` 或 `./start.sh` 启动服务
- 使用 `uv sync` 安装依赖
- 测试: `uv run pytest` (如果存在测试)
- 代码检查: `uv run ruff check .`

### Testing Requirements
- 修改核心逻辑后测试三种 API 格式的代理功能
- 测试负载均衡和故障转移逻辑
- 验证流式响应 (SSE) 转发

### Common Patterns
- Pydantic 模型用于数据验证
- FastAPI 路由模块化组织
- 异步 HTTP 请求使用 httpx
- 线程安全文件操作使用 threading.Lock

## Dependencies

### Internal
- `models/` - 数据模型被所有模块使用
- `config.py` - 配置被 storage、balancer、routers 使用
- `storage.py` - 数据持久化被 routers 和 proxy_core 使用

### External
- `fastapi` - Web 框架
- `uvicorn` - ASGI 服务器
- `httpx[socks]` - 异步 HTTP 客户端，支持 SOCKS5
- `pydantic` - 数据验证
- `python-socks[asyncio]` - SOCKS5 代理支持

<!-- MANUAL: -->
