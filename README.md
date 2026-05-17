# LLM-Plug — LLM API 格式转换代理

一个 LLM API 格式转换代理服务：客户端用一种 API 格式发请求，服务端自动转换后转发给不同格式的上游 LLM 提供商，再把响应转换回来——对客户端完全透明。

## 核心功能

- **三种 API 格式互转**：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages。对 Chat Completions 无法表达的 Responses 托管能力，代理会显式拒绝或按渠道能力降级，不做静默丢弃。
- **负载均衡与故障转移**：优先级分组 + 加权轮询 + 自动健康检查
- **SOCKS5 代理支持**：每个渠道可独立配置代理
- **Web 管理界面**：可视化配置渠道、API Key、模型组和业务设置
- **零配置启动**：无需 `.env`，默认监听 `0.0.0.0:55555`，业务设置写入 `data/settings.json`
- **请求记录与统计**：默认 SQLite，本地持久化，也可在前端切换请求记录到 PostgreSQL

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.11+ / FastAPI |
| 前端 | 原生 HTML + TailwindCSS (CDN) |
| 存储 | JSON 文件 / SQLite / PostgreSQL 请求记录 |
| HTTP | httpx[socks] |

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 启动服务
uv run python main.py

# 3. 访问管理页面
# http://localhost:55555/
```

Docker 部署时不需要传入配置环境变量；映射宿主机端口并挂载 `data/` 即可，例如 `-p 8000:55555 -v ./data:/app/data`。所有业务配置在前端设置页完成。

## 文档导航

| 文档 | 说明 |
|------|------|
| [快速上手](docs/getting-started.md) | 安装、配置、使用指南 |
| [架构设计](docs/architecture.md) | 核心概念、请求流程、模块划分 |
| [模块详解](docs/modules.md) | 各模块详细实现文档 |
| [部署指南](docs/deployment.md) | 零配置启动、Docker、生产部署 |
| [故障排查](docs/troubleshooting.md) | 常见问题与解决方案 |

## 支持的 API 格式

| 格式 | 代理端点 |
|------|----------|
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |

`GET /v1/responses/{id}` 和 `DELETE /v1/responses/{id}` 只读取或删除代理本地保存的 Responses 状态，不会转发到上游官方 Responses API。

## License

MIT
