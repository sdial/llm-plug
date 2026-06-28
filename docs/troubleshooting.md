# 故障排查

本文档介绍 LLM-Plug 常见问题的诊断和解决方法。

---

## 启动与访问问题

### 端口被占用

**现象**：启动时报 `Address already in use` 或 `端口 55555 被占用`

**原因**：
- 上次退出的进程未完全释放端口（尤其是热重载模式下）
- 其他程序已占用该端口

**解决方法**：

```bash
# 使用 kill_port.sh 脚本强制释放端口
./kill_port.sh 55555

# 或手动查找并终止进程
# Linux / macOS:
lsof -ti:55555 | xargs kill -9

# Windows (PowerShell):
netstat -ano | findstr :55555
taskkill /F /PID <找到的PID>
```

**预防**：Windows 开发时建议使用 `--no-reload` 模式启动：

```bash
uv run python main.py --no-reload
```

### 管理员密码忘记

**现象**：无法登录管理页面 `/admin`

**解决方法**：

管理员密码哈希存储在 `data/admin_auth.json`。删除该文件后重启服务，下次访问管理页面时会重新引导设置密码。

```bash
# 删除密码文件
rm data/admin_auth.json

# 重启服务
uv run python main.py
```

> 该文件结构：`{"password_hash": "...", "revoked_sessions": [...]}`。删除后所有已有会话也会失效，需重新登录。

### 访问管理页面显示空白或 404

**现象**：访问 `http://localhost:55555/` 返回 404

**原因**：管理页面路径为 `/admin`，不是根路径 `/`

**解决方法**：

```
# 正确的管理页面地址
http://localhost:55555/admin
```

---

## 鉴权问题

### 401 Unauthorized

**现象**：请求返回 401 错误

**原因**：
- 未设置 `Authorization` 请求头
- API Key 不正确或已被禁用
- API Key 列表为空（初始安装后未创建 Key）

**解决方法**：
1. 确认请求头格式正确：
   ```
   Authorization: Bearer <your-api-key>
   ```
   或使用 `x-api-key` 头：
   ```
   x-api-key: <your-api-key>
   ```
2. 在管理页面 `http://localhost:55555/admin` → 「API Keys」检查 Key 状态
3. 如果没有 Key，点击「添加 API Key」创建一个新的

### 403 Forbidden（IP 白名单拦截）

**现象**：请求返回 403，日志显示 `whitelist` 相关拦截信息

**原因**：
- `data/whitelist.csv` 配置了 IP 白名单规则，当前请求 IP 不在允许范围内

**解决方法**：
1. 检查当前规则：在管理页面「白名单」区域查看，或直接查看 `data/whitelist.csv`
2. 添加你的 IP 到白名单，例如：
   ```csv
   path_pattern,methods,cidr,desc
   /v1/*,,0.0.0.0/0,允许所有IP访问代理接口
   ```
3. 白名单基于文件 `mtime` 自动重载，修改保存后立即生效，无需重启
4. 如果不需要 IP 限制，清空 `whitelist.csv` 内容或删除该文件（无规则时默认放行）

---

## 代理请求问题

### 模型不存在 / model not found

**现象**：请求返回 `model not found in any enabled channel` 错误

**原因**：
- 所有渠道都未配置该模型名称
- 模型名称与请求中的不完全一致（区分大小写）
- 包含该模型的渠道全部被禁用
- `allowed_models` 设置限制了可用模型范围

**解决方法**：
1. 在管理页面检查渠道的模型列表，确认模型名称完全匹配
2. 检查渠道是否启用（开关状态）
3. 在「设置」页检查 `allowed_models` 配置：
   - 如果不为空，只有列出的模型名才能被代理
   - 留空表示不限制

### 所有渠道不可用 / No available channels

**现象**：请求返回 `No available channels for model: xxx` 错误

**原因**：
- 所有匹配渠道被标记为不健康（连续失败超过阈值）
- 所有匹配渠道被禁用
- 渠道的 `allow_format_conversion` 设置阻止了格式转换

**解决方法**：
1. 在管理页面检查渠道健康状态（红色 = 不健康）
2. 使用渠道「测试」按钮验证连通性
3. 检查渠道 API Key 是否有效
4. 检查渠道的 SOCKS5 代理配置是否正确
5. 重启服务可立即重置所有渠道的健康状态（内存存储，进程退出即清零）

> **提示**：不健康渠道在冷却期（默认 60 秒）后会自动恢复探测，也可以等待自动恢复。

### 请求体过大 / 413 Request Entity Too Large

**现象**：请求返回 413 状态码

**原因**：
- 请求体大小超过 `max_body_size` 限制（默认 10MB）
- `CombinedMiddleware` 在读取请求体前就进行校验

**解决方法**：
1. 在前端「设置」页调大请求体上限（前端显示为 MB 单位）
2. 该设置属于**热更新**，保存后立即生效，无需重启
3. 如果同时使用了 Nginx 反向代理，确保 `client_max_body_size` 也对应调大：
   ```nginx
   client_max_body_size 50m;
   ```

### 上游超时 / 504 Gateway Timeout

**现象**：请求返回 504 错误

**原因**：
- 上游响应时间超过「设置」页中的 `request_timeout`（默认 300 秒）
- 网络不稳定

**解决方法**：
1. 在「设置」页增加请求超时时间
2. 修改 `request_timeout` 后会自动重建所有 HTTP 客户端连接池
3. 检查上游服务状态和网络连接

### 流式响应中断

**现象**：流式响应中途停止或报错

**原因**：
- 上游中断流式输出
- 网络不稳定
- Nginx 缓冲了 SSE 事件

**解决方法**：
1. 检查上游 API 是否支持流式
2. 如使用 Nginx，确保以下配置：
   ```nginx
   proxy_buffering off;
   proxy_cache off;
   proxy_set_header X-Accel-Buffering no;
   ```
3. 查看请求日志（管理页面「请求记录」），检查上游返回的错误信息
4. 开启 debug 日志级别可以看到流式 chunk 详细内容：
   ```bash
   uv run python main.py --log-level debug
   ```

### 上游返回空流 / Empty Stream

**现象**：流式请求返回空内容，代理触发故障转移

**原因**：
- 上游连接成功但没有任何 SSE 事件输出（`_EmptyStreamError`）
- 可能是上游服务问题或请求参数不正确

**解决方法**：
1. 使用上游 API 地址直接测试，确认上游是否正常
2. 检查请求参数（model、messages 等）是否正确
3. 代理会自动触发故障转移，尝试下一个可用渠道

---

## 配置问题

### 配置保存后不生效

**现象**：在「设置」页修改配置并保存后，行为未改变

**原因**：部分配置需要重启服务才能生效

**详细说明**：

| 配置项 | 热更新 | 说明 |
|--------|--------|------|
| `request_timeout` | 是 | 自动重建客户端连接池 |
| `max_body_size` | 是 | 立即生效 |
| `log_level` | **否** | 需重启服务或下次 `--log-level` 启动时生效 |
| `host` | **否** | 只读，需修改启动参数 |
| `port` | **否** | 只读，需修改启动参数 |
| 负载均衡参数 | 是 | 立即生效 |
| 请求记录数据库 | 是 | 自动切换 |
| 其他业务配置 | 是 | 立即生效 |

保存响应中如果包含 `needs_restart: true`，说明该项需要重启才能生效。

### 渠道配置修改不生效

**现象**：通过 API 或管理页面修改渠道后，请求仍使用旧配置

**原因**：`storage.py` 有 5 秒 TTL 缓存

**解决方法**：
- 正常等待最多 5 秒缓存自动刷新
- 如需立即生效，重启服务
- **重要**：修改渠道数据必须通过管理 API（内部调用 `atomic_update_data()`），不要直接编辑 `channels.json` 文件

---

## 数据库问题

<<<<<<< HEAD
### 请求记录 PostgreSQL 连接失败

**现象**：日志显示数据库连接错误

**原因**：
- PostgreSQL 服务未启动
- 连接字符串格式不正确
- 防火墙阻止连接

**解决方法**：
1. 检查 PostgreSQL 服务状态
2. 验证「设置」页中的连接串格式：`postgresql://user:pass@host:5432/dbname`
3. 测试连接：`psql "postgresql://user:pass@host:5432/dbname"`

> **注意**：请求记录数据库连接失败时，代理功能**不受影响**。请求会正常处理，只是不记录到数据库。也可以切回默认 SQLite。

请求记录采用异步队列写入（`asyncio.Queue`，容量 1000，2 个后台 worker）。队列满时溢出记录写入 `logs/` 目录文件，不会阻塞代理请求。
=======
### SOCKS5 代理连接失败
>>>>>>> 2de5263 (docs: 移除 PostgreSQL 相关描述，明确仅支持 SQLite3)

---

## SOCKS5 代理问题

### 代理连接失败

**现象**：配置 SOCKS5 代理后请求失败

**原因**：
- 代理地址格式不正确
- 代理服务不可用
- 认证信息错误

**解决方法**：
1. 确认渠道的代理地址格式：`socks5://[user:pass@]host:port`
2. 测试代理连通性
3. 检查代理服务日志
4. 需要安装依赖 `httpx[socks]` 和 `python-socks[asyncio]`（项目已包含）

---

## 日志分析

### 日志文件说明

日志输出到 `logs/` 目录，使用 loguru 管理，10MB 自动轮转：

| 文件 | 级别 | 说明 |
|------|------|------|
| `logs/warning.log` | WARNING | 仅警告 |
| `logs/error.log` | ERROR | 仅错误 |
| `logs/critical.log` | CRITICAL | 仅严重错误 |

主服务和 `serve_viewer.py` 共用同一套 loguru 分级文件输出配置。`serve_viewer.py` 是独立的本地日志查看服务，只绑定 `127.0.0.1:8080`，不接入主服务的管理员会话和 CSRF 校验；它用于读取 `logs/*.jsonl` 会话记录，不替代管理端 `/admin/logs`。

### 常用日志查询

```bash
# 查看最新错误
tail -50 logs/error.log

# 实时查看警告日志
tail -f logs/warning.log

# 搜索特定模型名的请求
grep "gpt-4" logs/warning.log

# 搜索错误请求
grep "\[RES\].*status=[45]" logs/warning.log
```

### 请求记录查看

管理页面「请求记录」提供结构化查询界面，可查看每个请求的：
- 请求/响应头
- 请求/响应体（RAW 字段，受 `max_log_body_size` 截断限制）
- 延迟、状态码、使用的渠道等信息

---

## 性能问题排查

### 响应慢

**诊断步骤**：
1. 查看请求记录中的 `latency_ms`，区分是上游慢还是代理慢
2. `latency_ms` 起点是 `create_client()` 返回之后（不含连接建立时间）
3. 检查网络延迟

**优化建议**：
- 增加渠道数量分散负载
- 使用 SOCKS5 代理减少网络延迟
- 调整负载均衡权重（高权重渠道被选中概率更大）
- 使用同格式渠道减少格式转换开销

### 内存占用高

**原因**：
- HTTP 客户端连接池过多（按 `base_url|socks5_proxy` 缓存）

**解决方法**：
1. 合并相同 `base_url` 的渠道
2. 系统会定期调用 `cleanup_stale_clients()` 清理过期客户端
3. 修改 `request_timeout` 会自动调用 `invalidate_all_clients()` 重建所有连接池

### CPU 占用高

**原因**：
- 请求量大
- 格式转换计算密集（尤其是流式场景下逐 chunk 转换）

**优化建议**：
- 使用同格式渠道减少转换开销
- 关闭 debug 日志减少 CPU 开销
- 增加服务实例数量（通过 Nginx 负载均衡分发）

---

## 健康检查

### 检查服务状态

```bash
# 健康检查端点（Docker HEALTHCHECK 也使用此端点）
curl http://localhost:55555/v1/models
```

返回 200 表示服务正常。

### 检查渠道连通性

通过管理页面「测试」按钮，或 API：

```bash
curl -X POST "http://localhost:55555/admin/channels/{id}/test?model=gpt-4" \
  -H "Cookie: admin_session=<your-session>" \
  -H "X-CSRF-Token: <your-csrf-token>"
```

### 检查统计

```bash
# 需要管理员登录态
curl http://localhost:55555/admin/stats/overall
curl http://localhost:55555/admin/stats/today
```

---

## 恢复操作速查

| 场景 | 操作 |
|------|------|
| 重置所有渠道健康状态 | 重启服务 |
| 清除 storage 缓存 | 等待 5 秒或重启 |
| 重建 HTTP 连接池 | 修改 `request_timeout` 或重启 |
| 重置管理员密码 | 删除 `data/admin_auth.json` 并重启 |
| 恢复渠道配置 | 从备份覆盖 `data/channels.json`，等待 5 秒 |
| 释放被占用端口 | `./kill_port.sh 55555` |

---

## 获取帮助

1. 查看 `docs/` 目录下的架构文档和模块文档
2. 在管理页面查看请求记录定位问题
3. 检查 `logs/error.log` 和 `logs/critical.log` 分析错误
4. 提交 Issue 时附带错误日志和相关配置信息
