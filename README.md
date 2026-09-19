<div align="center">

# LLM-Plug

**LLM API Format Conversion Proxy**

**English** | [中文](./README_zh-CN.md)

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136+-009688.svg)](https://fastapi.tiangolo.com/)

</div>

---

LLM-Plug is an LLM API format conversion proxy. Clients send requests in one API format, and the proxy transparently converts and forwards them to upstream LLM providers using a different format, then converts the response back — completely invisible to the client.

**Search keywords:** self-hosted LLM API proxy, OpenAI-compatible API gateway, OpenAI Chat Completions proxy, OpenAI Responses API proxy, Anthropic Messages API proxy, LLM load balancing, failover, and API format conversion.

## Key Features

- **Tri-format Conversion** — Convert between OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages in any combination
- **Load Balancing & Failover** — Smooth Weighted Round-Robin (SWRR) + priority groups + automatic health checks with cooldown
- **Model Group Fallback** — Two-tier degradation: switch models within a group first, then switch channels within a model
- **Channel Catalog** — Typed, atomic Channel and Model Group changes with precise runtime-state synchronization
- **SOCKS5 Proxy** — Per-channel outbound proxy configuration
- **Capability Management** — Auto-infer and filter unsupported multimodal content (images, audio, files) per channel/model
- **Web Admin UI** — Visual management for channels, API keys, model groups, IP whitelist, and settings
- **Request Logging & Stats** — SQLite persistence with monthly database rotation and raw request/response replay
- **Security** — Admin session auth (PBKDF2-SHA256), CSRF protection, IP whitelist
- **Zero-config Startup** — No `.env` required, listens on `0.0.0.0:55555` by default

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12+ / FastAPI / Uvicorn |
| Frontend | Native HTML + TailwindCSS + htmx (local assets, zero CDN dependency) |
| Storage | JSON files / SQLite3 |
| HTTP Client | httpx[socks] + brotli + zstandard |
| Logging | Loguru |

## Quick Start

### Prerequisites

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) package manager

### Run Locally

```bash
# Install dependencies
uv sync

# Start the server (hot-reload by default)
uv run python main.py

# Recommended on Windows (avoids port occupation after reload exit)
uv run python main.py --no-reload
```

Visit `http://localhost:55555/admin` to access the admin panel. You'll be prompted to set an admin password on first visit.

### Docker Deployment (Recommended)

```bash
mkdir llm-plug && cd llm-plug
```

Create a `docker-compose.yml`:

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

Or use `docker run`:

```bash
docker run -d \
  --name llm-plug \
  --restart unless-stopped \
  -p 55555:55555 \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  ghcr.io/sdial/llm-plug:latest
```

## Supported API Endpoints

| Format | Endpoint |
|--------|----------|
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| Model List | `GET /v1/models` |

Clients only need to use any of the endpoints above — the proxy handles format conversion automatically. `GET /v1/responses/{id}` and `DELETE /v1/responses/{id}` operate on locally stored Responses session state.

## Conversion Matrix

| Inbound \ Upstream | Chat Completions | Responses | Anthropic |
|---|---|---|---|
| **Chat Completions** | Passthrough | ✅ | ✅ |
| **Responses** | ✅ | Passthrough | ✅ |
| **Anthropic** | ✅ | ✅ | Passthrough |

Both streaming and non-streaming modes are supported.

## Data Persistence

All runtime data is stored in the `data/` directory:

| File | Purpose |
|------|---------|
| `channels.json` | Channel and model group configuration |
| `api_keys.json` | Client access keys |
| `settings.json` | Business settings (timeouts, load balancing, etc.) |
| `admin_auth.json` | Admin password hash |
| `whitelist.csv` | IP whitelist rules |
| `stats.db` | Statistics aggregation (by channel/model/day) |
| `request_logs.db` | Request logs (monthly rotation) |

For Docker deployments, mount `./data` and `./logs` for persistence. Regular backups are recommended.

## Documentation

| Document | Description |
|----------|-------------|
| [Getting Started](docs/getting-started.en.md) | English installation, configuration, and usage guide |
| [中文快速上手](docs/getting-started.md) | Chinese installation, configuration, and usage guide |
| [Documentation Map](docs/README.en.md) | English documentation map |
| [文档导航](docs/README.md) | Chinese documentation map |
| [Architecture](docs/architecture.md) | Core concepts, request flow, module layout |
| [Module Reference](docs/modules.md) | Detailed implementation docs per module |
| [Deployment Guide](docs/deployment.md) | Zero-config startup, Docker, production deployment |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and solutions |
| [Quota Limits](docs/quota-limits.md) | 429 window limits and recovery behavior |
| [ADR Index](docs/adr/README.md) | Architectural decisions and their implementation status |

## Development

```bash
# Run tests
uv run pytest

# Lint
uv run ruff check .

# Format
uv run ruff format .
```
