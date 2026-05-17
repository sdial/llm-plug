# 部署指南

本文档介绍 LLM-Plug 的部署配置和生产环境最佳实践。

## 零配置启动

LLM-Plug 不需要 `.env`。服务使用内置默认值启动，业务设置通过前端「设置」页写入 `data/settings.json`。

固定启动约定：

| 项 | 值 |
|------|------|
| 容器监听地址 | `0.0.0.0` |
| 容器内部端口 | `55555` |
| 数据目录 | `data/` |
| 渠道配置 | `data/channels.json` |
| API Key | `data/api_keys.json` |
| 系统设置 | `data/settings.json` |
| 统计库 | `data/stats.db` |
| 默认请求记录库 | `data/request_logs.db` |

对外端口请使用 Docker 端口映射处理，例如 `8000:55555`。请求超时、请求体大小、负载均衡、日志级别、请求记录数据库等业务配置都在前端设置页维护。

## Docker 部署

### 构建镜像

```bash
docker build -t llm-plug:latest .
```

### 运行容器

```bash
docker run -d \
  --name llm-plug \
  -p 8000:55555 \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  llm-plug:latest
```

### Docker Compose

```yaml
version: '3.8'
services:
  llm-plug:
    build: .
    ports:
      - "8000:55555"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
```

启动：

```bash
docker-compose up -d
```

## 请求记录数据库

请求记录默认使用 `data/request_logs.db`。如需 PostgreSQL，在前端「设置」页切换请求记录数据库并填写连接串。

### 创建 PostgreSQL 数据库

```sql
CREATE DATABASE llmplug;
CREATE USER llmplug WITH PASSWORD 'your-password';
GRANT ALL PRIVILEGES ON DATABASE llmplug TO llmplug;
```

### 表结构

应用切换到 PostgreSQL 请求记录后会自动创建所需表：

- `requests` — 请求明细记录

统计聚合仍使用本地 `data/stats.db`。

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
ExecStart=/usr/local/bin/uv run uvicorn main:app --host 0.0.0.0 --port 55555
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

1. **启用鉴权**：在前端 API Key 页面创建访问 Key
2. **HTTPS**：通过 Nginx 配置 SSL 证书
3. **限制访问**：Nginx 配置 IP 白名单或限流
4. **定期备份**：备份 `data/channels.json` 和 PostgreSQL 数据库

### 性能调优

这些参数在前端「设置」页调整：

| 设置 | 建议值 | 说明 |
|------|--------|------|
| 失败次数阈值 | 3 | 生产环境更快剔除故障渠道 |
| 冷却时间 | 120 | 更长冷却期避免频繁重试 |
| 请求超时 | 600 | 处理长耗时请求 |
| 日志级别 | `warning` | 减少日志输出 |

### 监控

1. **健康检查**：`GET /v1/models` 可作为健康检查端点
2. **统计查询**：`GET /admin/stats` 获取请求统计

## Windows 部署

Windows 下推荐使用 `--no-reload` 模式启动，避免端口释放问题：

```bash
uv run python main.py --no-reload
```

或使用 `kill_port.sh` 脚本强制释放端口：

```bash
./kill_port.sh 55555
uv run python main.py
```

## 故障恢复

### 数据恢复

核心数据都在 `data/` 下。恢复渠道配置时：

```bash
cp channels.json.backup data/channels.json
```

### PostgreSQL 重连

数据库连接断开时会自动重连，无需重启服务。

### 渠道健康恢复

不健康渠道在冷却期后自动恢复探测，无需手动干预。
