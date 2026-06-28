# 部署指南

本文档介绍 LLM-Plug 在各种环境下的部署方式，从本地开发到 Docker 生产部署。

## 零配置启动

LLM-Plug **不需要 `.env`**。服务使用内置默认值启动，业务设置通过前端「设置」页写入 `data/settings.json`。

固定启动约定：

| 项 | 值 |
|------|------|
| 监听地址 | `0.0.0.0` |
| 监听端口 | `55555` |
| 数据目录 | `data/` |
| 渠道配置 | `data/channels.json` |
| API Key | `data/api_keys.json` |
| 系统设置 | `data/settings.json` |
| 管理员鉴权 | `data/admin_auth.json` |
| IP 白名单 | `data/whitelist.csv` |
| 统计库 | `data/stats.db` |
| 默认请求记录库 | `data/request_logs.db` |
| Responses 会话状态 | `data/responses_session/` |

对外端口请使用 Docker 端口映射或反向代理处理。请求超时、请求体大小、负载均衡、日志级别、请求记录数据库等业务配置都在前端「设置」页维护。

---

## 本地开发部署

### 环境要求

- Python >= 3.10（推荐 3.12）
- [uv](https://docs.astral.sh/uv/) 包管理器

### 安装依赖

```bash
uv sync
```

> `uv sync` 会根据 `pyproject.toml` 和 `uv.lock` 安装所有依赖到 `.venv/` 目录。

### 启动方式

#### 方式一：直接运行 Python

```bash
# 带热重载（代码修改后自动重启，适合开发）
uv run python main.py

# 不带热重载（推荐 Windows 使用，避免退出后端口占用问题）
uv run python main.py --no-reload
```

也可以通过命令行参数指定日志级别：

```bash
uv run python main.py --log-level debug
```

#### 方式二：使用 start.sh 脚本（Linux / macOS / Git Bash）

```bash
# 正常运行（默认）
./start.sh run

# 调试模式（热重载 + trace 级别日志）
./start.sh debug
```

**`start.sh` 的工作流程：**

1. 检查 `.venv/` 目录是否存在，首次运行自动执行 `uv sync` 安装依赖
2. 注册 `SIGINT` / `SIGTERM` 信号处理器，Ctrl+C 时自动清理所有子进程
3. 根据模式启动 `uvicorn`：
   - **run 模式**：`httptools` HTTP 解析 + `auto` 事件循环 + `--access-log` + `backlog=2048`
   - **debug 模式**：额外启用 `--reload` + `trace` 日志级别 + 彩色输出

### 端口占用问题

开发时如果热重载退出后端口仍被占用，使用 `kill_port.sh` 脚本强制释放：

```bash
# 释放默认端口 55555
./kill_port.sh 55555

# 释放指定端口
./kill_port.sh 8080
```

该脚本自动识别操作系统：
- **Windows（Git Bash）**：通过 `netstat` + `taskkill` 终止进程
- **Linux / macOS**：通过 `lsof` + `kill -9` 终止进程

### 独立日志查看器

`serve_viewer.py` 是与主代理分离运行的本地日志查看服务，用于查看 `logs/*.jsonl` 会话记录：

```bash
uv run python serve_viewer.py
```

默认监听 `127.0.0.1:8080`，也可以通过第一个参数指定端口：

```bash
uv run python serve_viewer.py 18080
```

viewer 无管理员鉴权，只绑定 loopback；不要直接暴露到公网。它与主服务共用 `logs/` 目录和 loguru 分级文件输出配置，但不会承载代理 API、管理 API 或 `/admin/logs`。

---

## Docker 部署

### 镜像信息

项目提供预构建镜像，托管在 CNB（Cloud Native Build）：

```
docker.cnb.cool/lfo.cc/llm-plug:latest
```

支持 `linux/amd64` 和 `linux/arm64` 两种架构。

### Dockerfile 结构

Dockerfile 位于 `docker-deploy/Dockerfile`，采用**两阶段构建**以减小最终镜像体积：

**Stage 1 — 构建依赖（builder）：**

```dockerfile
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock .
RUN uv sync --no-dev --frozen
```

- 使用 `uv` 0.6.14 版本，仅安装生产依赖（`--no-dev --frozen`）

**Stage 2 — 运行时：**

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY . .
RUN mkdir -p data logs
EXPOSE 55555
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:55555/v1/models')" || exit 1
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "55555", \
     "--http", "httptools", "--workers", "1", \
     "--timeout-keep-alive", "360", "--log-level", "info", \
     "--no-use-colors", "--no-server-header", "--ws", "none", \
     "--loop", "uvloop", "--backlog", "2048"]
```

关键配置说明：

| 参数 | 值 | 说明 |
|------|-----|------|
| `--http` | `httptools` | 高性能 HTTP 解析器（比默认 `h11` 更快） |
| `--loop` | `uvloop` | 高性能事件循环（Linux 专用，Windows 不支持） |
| `--workers` | `1` | 单 worker（代理本身是异步 IO，无需多进程） |
| `--timeout-keep-alive` | `360` | Keep-Alive 超时 6 分钟（适应长连接 SSE 流式请求） |
| `--backlog` | `2048` | 连接排队长度（应对突发流量） |
| `--ws` | `none` | 禁用 WebSocket（本服务不需要） |

健康检查端点：`GET /v1/models`（每 30 秒检测一次，连续 3 次失败标记为不健康）

### 使用 Docker Compose 部署（推荐）

`docker-deploy/docker-compose.yml` 内容如下：

```yaml
services:
  llm-plug:
    image: docker.cnb.cool/lfo.cc/llm-plug:latest
    container_name: llm-plug
    restart: unless-stopped
    ports:
      - "55555:55555"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
```

部署步骤：

```bash
# 1. 进入部署目录
cd docker-deploy

# 2. 拉取最新镜像
docker compose pull

# 3. 启动容器
docker compose up -d

# 4. 查看日志
docker compose logs -f

# 5. 停止容器
docker compose down
```

<<<<<<< HEAD
**端口映射**：默认映射为 `55555:55555`。如需改为其他对外端口（如 8000），修改 `ports` 为 `"8000:55555"`。

**数据持久化**：`data/` 和 `logs/` 通过 volume 挂载到宿主机，容器重建不会丢失数据。

### 手动构建镜像

如果需要自行构建（如修改了代码）：

```bash
# 在项目根目录执行
docker build -f docker-deploy/Dockerfile -t llm-plug:latest .
```

或使用 `build.sh` 构建并推送多架构镜像到 CNB：

```bash
# 使用日期时间作为 tag
./docker-deploy/build.sh

# 指定自定义 tag
./docker-deploy/build.sh v1.0.0
=======
## 生产环境建议

### 反向代理配置

推荐使用 Nginx 作为反向代理：

```nginx
server {
    listen 80;
    server_name api.your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 支持
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header X-Accel-Buffering no;
    }
}
>>>>>>> 2de5263 (docs: 移除 PostgreSQL 相关描述，明确仅支持 SQLite3)
```

`build.sh` 使用 `docker buildx` 同时构建 `amd64` 和 `arm64` 架构，并自动推送到 CNB 仓库。

### 手动运行容器

不使用 Compose 时，可直接 `docker run`：

```bash
docker run -d \
  --name llm-plug \
  -p 55555:55555 \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  --restart unless-stopped \
  docker.cnb.cool/lfo.cc/llm-plug:latest
```

---

## systemd 部署（Linux 服务器）

不使用 Docker 时，可用 systemd 管理进程：

```ini
# /etc/systemd/system/llm-plug.service
[Unit]
Description=LLM-Plug API Proxy
After=network.target

[Service]
Type=simple
User=llmplug
WorkingDirectory=/opt/llm-plug
ExecStart=/opt/llm-plug/.venv/bin/uvicorn main:app \
    --host 0.0.0.0 \
    --port 55555 \
    --http httptools \
    --loop uvloop \
    --workers 1 \
    --timeout-keep-alive 360 \
    --log-level info \
    --no-use-colors \
    --no-server-header \
    --ws none \
    --backlog 2048
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable llm-plug
sudo systemctl start llm-plug

# 查看状态
sudo systemctl status llm-plug

# 查看日志
journalctl -u llm-plug -f
```

---

<<<<<<< HEAD
## 反向代理配置
=======
1. **启用鉴权**：在前端 API Key 页面创建访问 Key
2. **HTTPS**：通过 Nginx 配置 SSL 证书
3. **限制访问**：Nginx 配置 IP 白名单或限流
4. **定期备份**：备份 `data/channels.json` 和 `data/` 目录
>>>>>>> 2de5263 (docs: 移除 PostgreSQL 相关描述，明确仅支持 SQLite3)

生产环境推荐使用 Nginx 作为反向代理，提供 SSL 终止、静态缓存和访问控制。

### Nginx 配置

```nginx
server {
    listen 80;
    server_name api.your-domain.com;

    # 如需 HTTPS，配置 SSL 后取消以下注释
    # listen 443 ssl;
    # ssl_certificate     /path/to/cert.pem;
    # ssl_certificate_key /path/to/key.pem;

    # 请求体大小限制（与 LLM-Plug 的 max_body_size 设置对齐，默认 10MB）
    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:55555;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE（Server-Sent Events）流式响应支持 —— 必须关闭缓冲
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header X-Accel-Buffering no;

        # 长连接超时（建议 >= 上游 request_timeout 设置）
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

> **重要**：`proxy_buffering off` 和 `X-Accel-Buffering: no` 是流式响应正常工作的必要条件。缺少这些配置会导致 SSE 事件被 Nginx 缓冲，客户端无法实时收到流式数据。

---

## 请求记录数据库

请求记录默认使用 SQLite（`data/request_logs.db`）。如需切换到 PostgreSQL，在前端「设置」页修改数据库类型和连接串。

### 创建 PostgreSQL 数据库

```sql
CREATE DATABASE llmplug;
CREATE USER llmplug WITH PASSWORD 'your-password';
GRANT ALL PRIVILEGES ON DATABASE llmplug TO llmplug;
```

连接字符串格式：

```
postgresql://llmplug:your-password@localhost:5432/llmplug
```

应用切换到 PostgreSQL 后会自动创建所需表。统计聚合始终使用本地 `data/stats.db`。

请求记录写入采用异步队列（`asyncio.Queue`，容量 1000）+ 2 个后台 worker，不会阻塞代理请求。队列满时溢出记录写入 `logs/` 目录下的文件。

---

## 安全建议

### 1. 设置管理员密码

首次访问 `http://your-host:55555/admin` 时，系统会引导你设置管理员密码。密码使用 PBKDF2-SHA256（260,000 轮迭代）哈希存储。

### 2. 创建 API Key

在管理页面「API Keys」区域创建客户端访问 Key。客户端请求时必须携带：

```
Authorization: Bearer <your-api-key>
```

或使用 `x-api-key` 头：

```
x-api-key: <your-api-key>
```

### 3. 配置 IP 白名单

在管理页面「白名单」区域或手动编辑 `data/whitelist.csv` 限制访问 IP。格式为 CSV：

```csv
path_pattern,methods,cidr,desc
/v1/*,,10.0.0.0/8,内网访问代理接口
/admin/*,GET;POST,192.168.1.0/24,管理页面只读
```

- `path_pattern`：URL 路径模式（支持 `*` 通配符）
- `methods`：允许的 HTTP 方法（空 = 所有方法）
- `cidr`：IP 地址段（CIDR 格式）
- `desc`：备注说明

无任何规则时默认放行所有请求。

### 4. HTTPS

通过 Nginx 配置 SSL 证书，确保所有流量经过 HTTPS。

### 5. 定期备份

核心数据都在 `data/` 目录下，建议定期备份：

```bash
# 备份所有配置和数据库
tar czf llm-plug-backup-$(date +%Y%m%d).tar.gz data/
```

---

## 生产环境性能调优

以下参数在前端「设置」页调整：

| 设置 | 建议值 | 说明 |
|------|--------|------|
| 失败次数阈值 | 3 | 更快剔除故障渠道 |
| 冷却时间 | 120 | 更长冷却期避免频繁重试故障渠道 |
| 请求超时 | 600 | 适应长耗时请求（如大量 token 生成） |
| 日志级别 | `warning` | 减少日志输出开销 |
| 请求体上限 | `10485760` | 默认 10MB，按需调整 |

---

## 监控与健康检查

| 端点 | 用途 |
|------|------|
| `GET /v1/models` | 服务健康检查（Docker HEALTHCHECK 也使用此端点） |
| `GET /admin/stats/overall` | 全局统计数据 |
| `GET /admin/stats/today` | 今日统计 |
| `GET /admin/stats/daily` | 按天统计 |

---

## 故障恢复

### 数据恢复

核心数据都在 `data/` 下，从备份恢复：

```bash
# 恢复渠道配置
cp channels.json.backup data/channels.json

# 重启服务使缓存刷新（storage 有 5 秒 TTL 缓存）
```

<<<<<<< HEAD
> **注意**：直接覆盖 `channels.json` 文件后，内存缓存最多 5 秒才会更新。如需立即生效，重启服务。

### PostgreSQL 重连

数据库连接断开时会自动重连（异步队列 worker 每次写入时检测连接状态），无需重启服务。

=======
>>>>>>> 2de5263 (docs: 移除 PostgreSQL 相关描述，明确仅支持 SQLite3)
### 渠道健康恢复

不健康渠道在冷却期（默认 60 秒，可在设置页调整）后自动恢复探测，无需手动干预。重启服务可立即重置所有渠道的健康状态（内存存储，进程退出即清零）。
