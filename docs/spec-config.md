# spec-config — 配置管理

> 对应文件：`config.py`（约 50 行）

## 模块定位

`config.py` 是项目配置的**单一来源**（Single Source of Truth），所有配置项通过环境变量读取，模块加载时即确定值。其他模块通过 `import config` 或 `from config import XXX` 获取配置。

## 配置项详解

### 服务器配置

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `HOST` | `HOST` | `"0.0.0.0"` | str | 监听地址，`0.0.0.0` 表示所有网卡 |
| `PORT` | `PORT` | `8000` | int | 监听端口 |

### 数据存储

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `DATA_DIR` | `DATA_DIR` | `项目根目录/data` | str | 数据存储目录 |
| `CHANNELS_FILE` | `CHANNELS_FILE` | `DATA_DIR/channels.json` | str | 渠道配置文件路径 |
| `API_KEYS_FILE` | `API_KEYS_FILE` | `DATA_DIR/api_keys.json` | str | API Key 配置文件路径 |

**路径解析**：默认值基于 `os.path.dirname(__file__)`（即 `config.py` 所在目录，也就是项目根目录），确保无论从哪个目录启动服务都能正确找到数据文件。

### 负载均衡

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `MAX_FAIL_COUNT` | `MAX_FAIL_COUNT` | `5` | int | 连续失败 N 次后标记渠道不健康 |
| `COOLDOWN_SECONDS` | `COOLDOWN_SECONDS` | `60` | int | 不健康渠道冷却恢复时间（秒） |

### 请求超时

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `REQUEST_TIMEOUT` | `REQUEST_TIMEOUT` | `300` | int | 上游请求超时时间（秒） |

**影响范围**：
- `create_client()` 的总超时
- `create_stream_client()` 的总超时和读取超时
- 渠道连通性测试使用独立的 30 秒超时，不使用此配置

### 鉴权

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `PROXY_API_KEY` | `PROXY_API_KEY` | `""` (空) | str | 代理 API 密钥 |

**空值 = 不鉴权**：如果环境变量未设置或为空，则对应接口不需要鉴权。

> 注意：管理接口（`/admin/*`）无需认证，直接访问。

### 调试

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `DEBUG` | `DEBUG` | `false` | bool | 调试模式开关 |
| `DEBUG_LOG_DIR` | `DEBUG_LOG_DIR` | `项目根目录/logs` | str | 调试日志目录 |

**DEBUG 的判断逻辑**：

```python
DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
```

以下值都会启用调试模式：`true`、`True`、`1`、`yes`、`YES`。

**DEBUG 启用后的效果**：

1. `proxy_core.py`：每次请求记录完整的请求/响应到 JSONL 日志文件
2. `main.py`：注册 HTTP 中间件，打印每个请求的 method/path/model/stream 信息和响应状态码

### PostgreSQL 统计

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `DATABASE_URL` | `DATABASE_URL` | `postgresql://localhost:5432/llmplug` | str | PostgreSQL 连接 URL |

**统计功能说明**：

- 如果 `DATABASE_URL` 未配置或连接失败，统计功能将被禁用，不影响代理核心功能
- 统计数据存储在 PostgreSQL 中，包括请求明细和聚合统计

### 统计追踪请求头

| 变量 | 环境变量 | 默认值 | 类型 | 说明 |
|------|----------|--------|------|------|
| `STATS_TRACKED_HEADERS` | `STATS_TRACKED_HEADERS` | (空) | str | 统计追踪的请求头 |

**追踪逻辑**：

- 空值或 `ALL`：追踪所有请求头
- 其他值：逗号分隔的请求头名称列表（如 `user-agent,x-request-id`）

## 配置值的读取时机

所有配置在 `config.py` 模块**加载时**读取，即 Python 进程启动时确定。**运行期间修改环境变量不会生效**，需要重启服务。

## 各模块如何使用配置

| 模块 | 导入的配置 | 用途 |
|------|-----------|------|
| `main.py` | `HOST`, `PORT`, `DEBUG`, `STATS_TRACKED_HEADERS`, `TRACK_ALL_HEADERS` | 服务器启动参数、调试中间件、请求头追踪 |
| `storage.py` | `config.DATA_DIR`, `config.CHANNELS_FILE`, `config.API_KEYS_FILE` | 数据文件路径 |
| `client.py` | `REQUEST_TIMEOUT` | httpx 客户端超时 |
| `balancer/load_balancer.py` | `MAX_FAIL_COUNT`, `COOLDOWN_SECONDS` | 健康检查参数 |
| `routers/auth.py` | `PROXY_API_KEY` | 代理鉴权 |
| `proxy_core.py` | `DEBUG`, `DEBUG_LOG_DIR` | 调试日志 |
| `stats.py` | `DATABASE_URL`, `STATS_TRACKED_HEADERS`, `TRACK_ALL_HEADERS` | PostgreSQL 连接、请求头追踪 |

## 常见配置场景

### 开发环境

```bash
# 最简配置，无鉴权，调试模式
export DEBUG=true
uv run python main.py
```

### 生产环境

```bash
export PROXY_API_KEY=sk-your-secret-key
export MAX_FAIL_COUNT=3
export COOLDOWN_SECONDS=120
export REQUEST_TIMEOUT=600
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

### 使用 SOCKS5 代理访问上游

在渠道配置中设置 `socks5_proxy` 字段，而非在 config 中配置全局代理。

## 注意事项

1. **不支持热加载**：修改环境变量后需重启服务。如果需要动态配置，需要改造为读取存储或实现配置热加载。
2. **PORT 是 int 转换**：`int(os.getenv("PORT", "8000"))`，如果环境变量不是数字会抛出 `ValueError`。
3. **DATA_DIR 路径**：默认基于 `config.py` 的位置解析，不是当前工作目录。如果你通过符号链接或打包部署，需要显式设置 `DATA_DIR`。
4. **DEBUG 日志可能很大**：开启 DEBUG 后每个请求都记录完整数据，高流量下磁盘空间消耗很快。建议仅调试时开启。
