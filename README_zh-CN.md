<div align="center">

# LLM-Plug

**LLM API 格式转换代理**

[English](./README.md) | **中文**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136+-009688.svg)](https://fastapi.tiangolo.com/)

</div>

---

LLM-Plug 是一个 LLM API 格式转换代理服务。客户端用一种 API 格式发请求，代理自动转换后转发给不同格式的上游 LLM 提供商，再把响应转换回来——对客户端完全透明。

## 核心特性

- **三种 API 格式互转** — OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 任意组合转换
- **负载均衡与故障转移** — 平滑加权轮询（SWRR）+ 优先级分组 + 自动健康检查与冷却
- **模型组 Fallback** — 两层降级：先按模型组顺序切换模型，再在模型内部切换渠道
- **SOCKS5 代理** — 每个渠道可独立配置出站代理
- **能力管理** — 按渠道/模型自动推断并过滤不支持的多模态内容（图片、音频、文件）
- **Web 管理界面** — 可视化配置渠道、API Key、模型组、IP 白名单和业务设置
- **请求记录与统计** — SQLite 持久化，按月分库，支持原始请求/响应回放
- **安全** — 管理员会话鉴权（PBKDF2-SHA256）、CSRF 防护、IP 白名单、SSRF 防护
- **零配置启动** — 无需 `.env`，默认监听 `0.0.0.0:55555`

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.12+ / FastAPI / Uvicorn |
| 前端 | 原生 HTML + TailwindCSS + htmx（本地资源，零 CDN 依赖） |
| 存储 | JSON 文件 / SQLite3 |
| HTTP 客户端 | httpx[socks] + brotli + zstandard |
| 日志 | Loguru |

## 快速开始

### 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) 包管理器

### 本地运行

```bash
# 安装依赖
uv sync

# 启动服务（默认热重载）
uv run python main.py

# Windows 推荐（避免热重载退出后端口占用）
uv run python main.py --no-reload
```

启动后访问 `http://localhost:55555/admin` 进入管理页面，首次访问需设置管理员密码。

### Docker 部署（推荐）

```bash
mkdir llm-plug && cd llm-plug
```

创建 `docker-compose.yml`：

```yaml
services:
  llm-plug:
    image: ghcr.io/sdial/llm-plug:latest
    container_name: llm-plug
    user: root
    mem_limit: 256m
    restart: unless-stopped
    ports:
      - "55555:55555"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - TZ=${TZ:-Asia/Shanghai}
```

```bash
docker-compose up -d
```

或使用 `docker run`：

```bash
docker run -d \
  --name llm-plug \
  --restart unless-stopped \
  -p 55555:55555 \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  ghcr.io/sdial/llm-plug:latest
```

## 支持的 API 端点

| 格式 | 端点 |
|------|------|
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| 模型列表 | `GET /v1/models` |

客户端只需使用上述任一端点，代理会自动完成格式转换。`GET /v1/responses/{id}` 和 `DELETE /v1/responses/{id}` 操作代理本地保存的 Responses 会话状态。

## 转换矩阵

| 入口 \ 上游 | Chat Completions | Responses | Anthropic |
|---|---|---|---|
| **Chat Completions** | 直通 | ✅ | ✅ |
| **Responses** | ✅ | 直通 | ✅ |
| **Anthropic** | ✅ | ✅ | 直通 |

流式与非流式均支持。

## 数据持久化

所有运行时数据存放在 `data/` 目录：

| 文件 | 用途 |
|------|------|
| `channels.json` | 渠道与模型组配置 |
| `api_keys.json` | 客户端访问密钥 |
| `settings.json` | 业务设置（超时、负载均衡等） |
| `admin_auth.json` | 管理员密码哈希 |
| `whitelist.csv` | IP 白名单规则 |
| `stats.db` | 统计聚合（按渠道/模型/天） |
| `request_logs.db` | 请求记录（按月分库） |

Docker 部署时挂载 `./data` 和 `./logs` 即可实现持久化，建议定期备份。

## 文档

| 文档 | 说明 |
|------|------|
| [快速上手](docs/getting-started.md) | 安装、配置、使用指南 |
| [架构设计](docs/architecture.md) | 核心概念、请求流程、模块划分 |
| [模块详解](docs/modules.md) | 各模块详细实现文档 |
| [部署指南](docs/deployment.md) | 零配置启动、Docker、生产部署 |
| [故障排查](docs/troubleshooting.md) | 常见问题与解决方案 |

## 开发

```bash
# 运行测试
uv run pytest

# Lint
uv run ruff check .

# 格式化
uv run ruff format .
```

## License

[MIT](./LICENSE)
