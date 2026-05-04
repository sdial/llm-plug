# 部署指南

本文档介绍 LLM-Plug 的部署配置和生产环境最佳实践。

## 环境变量

### 服务器配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |

### 数据存储

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATA_DIR` | 项目根目录/data | 数据存储目录 |
| `CHANNELS_FILE` | `DATA_DIR/channels.json` | 渠道配置文件 |
| `API_KEYS_FILE` | `DATA_DIR/api_keys.json` | API Key 配置文件 |

### 负载均衡

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MAX_FAIL_COUNT` | `5` | 连续失败 N 次后标记渠道不健康 |
| `COOLDOWN_SECONDS` | `60` | 不健康渠道冷却恢复时间（秒） |

### 请求配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REQUEST_TIMEOUT` | `300` | 上游请求超时时间（秒） |
| `MAX_BODY_SIZE` | `10485760` | 请求体最大字节数（默认 10MB） |

### 鉴权

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_API_KEY` | (空) | 代理 API 密钥，空则不鉴权 |

### 日志与调试

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_LEVEL` | `info` | 日志级别 |
| `DEBUG` | `false` | 调试模式开关 |
| `DEBUG_LOG_DIR` | 项目根目录/logs | 调试日志目录 |

### PostgreSQL 统计

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql://localhost:5432/llmplug` | PostgreSQL 连接 URL |
| `STATS_TRACKED_HEADERS` | (空) | 统计追踪的请求头 |

## Docker 部署

### 构建镜像

```bash
docker build -t llm-plug:latest .
```

### 运行容器

```bash
docker run -d \
  --name llm-plug \
  -p 8000:8000 \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  -e PROXY_API_KEY=your-secret-key \
  -e DATABASE_URL=postgresql://user:pass@host:5432/llmplug \
  llm-plug:latest
```

### Docker Compose

```yaml
version: '3.8'
services:
  llm-plug:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - PROXY_API_KEY=${PROXY_API_KEY}
      - DATABASE_URL=postgresql://llmplug:llmplug@postgres:5432/llmplug
      - MAX_FAIL_COUNT=3
      - COOLDOWN_SECONDS=120
    depends_on:
      - postgres

  postgres:
    image: postgres:15
    environment:
      - POSTGRES_USER=llmplug
      - POSTGRES_PASSWORD=llmplug
      - POSTGRES_DB=llmplug
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
```

启动：

```bash
docker-compose up -d
```

## PostgreSQL 配置

### 创建数据库

```sql
CREATE DATABASE llmplug;
CREATE USER llmplug WITH PASSWORD 'your-password';
GRANT ALL PRIVILEGES ON DATABASE llmplug TO llmplug;
```

### 表结构

应用启动时会自动创建所需表：

- `requests` — 请求明细记录
- `stats_hourly` — 小时聚合统计
- `stats_daily` — 日聚合统计

### 统计功能

如果未配置 `DATABASE_URL`，统计功能将被禁用，不影响代理核心功能。

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
```

### 进程管理

使用 systemd 管理服务：

```ini
# /etc/systemd/system/llm-plug.service
[Unit]
Description=LLM-Plug API Proxy
After=network.target

[Service]
Type=simple
User=llmplug
WorkingDirectory=/opt/llm-plug
Environment="PROXY_API_KEY=your-secret-key"
Environment="DATABASE_URL=postgresql://..."
ExecStart=/usr/local/bin/uv run uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl enable llm-plug
sudo systemctl start llm-plug
```

### 安全建议

1. **启用鉴权**：设置 `PROXY_API_KEY` 或使用 API Keys 管理
2. **HTTPS**：通过 Nginx 配置 SSL 证书
3. **限制访问**：Nginx 配置 IP 白名单或限流
4. **日志轮转**：配置 logrotate 管理 DEBUG 日志
5. **定期备份**：备份 `data/channels.json` 和 PostgreSQL 数据库

### 性能调优

| 参数 | 建议值 | 说明 |
|------|--------|------|
| `MAX_FAIL_COUNT` | 3 | 生产环境更快剔除故障渠道 |
| `COOLDOWN_SECONDS` | 120 | 更长冷却期避免频繁重试 |
| `REQUEST_TIMEOUT` | 600 | 处理长耗时请求 |
| `LOG_LEVEL` | `warning` | 减少日志输出 |

### 监控

1. **健康检查**：`GET /v1/models` 可作为健康检查端点
2. **统计查询**：`GET /admin/stats` 获取请求统计
3. **日志分析**：DEBUG 日志为 JSONL 格式，可用 jq 或 ELK 分析

## Windows 部署

Windows 下推荐使用 `--no-reload` 模式启动，避免端口释放问题：

```bash
uv run python main.py --no-reload
```

或使用 `kill_port.sh` 脚本强制释放端口：

```bash
./kill_port.sh 8000
uv run python main.py
```

## 故障恢复

### 数据恢复

所有配置存储在 `data/channels.json`，恢复时：

```bash
cp channels.json.backup data/channels.json
```

### PostgreSQL 重连

数据库连接断开时会自动重连，无需重启服务。

### 渠道健康恢复

不健康渠道在冷却期后自动恢复探测，无需手动干预。
