# 故障排查

本文档介绍 LLM-Plug 常见问题的诊断和解决方法。

## 常见错误

### 401 Unauthorized

**现象**：请求返回 401 错误

**原因**：
- 未设置 `Authorization` 请求头
- API Key 不正确
- API Key 未启用

**解决方法**：
1. 检查请求头：`Authorization: Bearer <your-api-key>`
2. 在管理页面检查 API Key 状态
3. 如未配置鉴权，检查 `PROXY_API_KEY` 环境变量

### 模型不存在

**现象**：请求返回 `model not found` 错误

**原因**：
- 渠道中未配置该模型
- 模型名称与请求中的不完全一致

**解决方法**：
1. 访问管理页面检查渠道的模型列表
2. 确保模型名称完全匹配（区分大小写）
3. 检查渠道是否启用

### 所有渠道不可用

**现象**：请求返回 `No available channels` 错误

**原因**：
- 所有匹配的渠道都被标记为不健康
- 所有匹配的渠道都被禁用

**解决方法**：
1. 访问管理页面检查渠道状态
2. 使用渠道测试功能验证连通性
3. 检查渠道 API Key 是否有效
4. 检查代理配置是否正确

### 上游超时

**现象**：请求返回 504 Gateway Timeout

**原因**：
- 上游响应时间超过 `REQUEST_TIMEOUT`
- 网络不稳定

**解决方法**：
1. 增加 `REQUEST_TIMEOUT` 环境变量值
2. 检查上游服务状态
3. 检查网络连接

### 流式响应中断

**现象**：流式响应中途停止或报错

**原因**：
- 上游中断流式输出
- 网络不稳定
- Nginx 缓冲 SSE

**解决方法**：
1. 检查上游 API 是否支持流式
2. 如使用 Nginx，确保配置 `proxy_buffering off`
3. 开启 DEBUG 日志定位问题

### PostgreSQL 连接失败

**现象**：日志显示数据库连接错误

**原因**：
- PostgreSQL 服务未启动
- 连接字符串不正确
- 防火墙阻止连接

**解决方法**：
1. 检查 PostgreSQL 服务状态
2. 验证 `DATABASE_URL` 格式
3. 测试网络连接：`psql $DATABASE_URL`

**注意**：数据库连接失败时，统计功能自动禁用，代理功能不受影响。

### SOCKS5 代理连接失败

**现象**：配置代理后请求失败

**原因**：
- 代理地址格式不正确
- 代理服务不可用
- 认证信息错误

**解决方法**：
1. 检查代理格式：`socks5://[user:pass@]host:port`
2. 测试代理连通性
3. 检查代理服务日志

## 日志分析

### 开启调试日志

```bash
export DEBUG=true
uv run python main.py
```

日志文件位于 `logs/debug_YYYY-MM-DD.jsonl`。

### 日志格式

每行一个 JSON 对象：

```json
{
  "timestamp": "2024-01-01T12:00:00Z",
  "model": "gpt-4",
  "channel": "OpenAI 官方",
  "request": {...},
  "response": {...},
  "latency_ms": 1234
}
```

### 常用日志查询

```bash
# 查看错误日志
cat logs/debug_*.jsonl | jq 'select(.error)'

# 统计各渠道请求量
cat logs/debug_*.jsonl | jq -r '.channel' | sort | uniq -c

# 查看慢请求（>5s）
cat logs/debug_*.jsonl | jq 'select(.latency_ms > 5000)'

# 查看特定模型
cat logs/debug_*.jsonl | jq 'select(.model == "gpt-4")'
```

## 性能问题排查

### 响应慢

**诊断步骤**：
1. 开启 DEBUG 日志，检查 `latency_ms` 字段
2. 区分是上游慢还是代理慢
3. 检查网络延迟

**优化建议**：
- 增加渠道数量分散负载
- 使用代理减少网络延迟
- 调整负载均衡权重

### 内存占用高

**原因**：
- DEBUG 日志文件过大
- 客户端连接池过多

**解决方法**：
1. 定期清理日志文件
2. 减少渠道数量或合并相同 base_url 的渠道
3. 调整 `cleanup_stale_clients` 参数

### CPU 占用高

**原因**：
- 请求量大
- 格式转换计算密集

**优化建议**：
- 使用同格式渠道减少转换开销
- 增加服务实例数量

## 健康检查

### 检查服务状态

```bash
curl http://localhost:8000/v1/models
```

### 检查渠道连通性

通过管理页面或 API：

```bash
curl -X POST "http://localhost:8000/admin/channels/{id}/test?model=gpt-4"
```

### 检查数据库连接

```bash
curl http://localhost:8000/admin/stats
```

## 恢复操作

### 重置渠道健康状态

重启服务可重置所有渠道的健康状态。

### 清除缓存

修改配置后如有缓存问题，重启服务。

### 数据恢复

从备份恢复 `channels.json`：

```bash
cp data/channels.json.backup data/channels.json
```

## 获取帮助

1. 查看本文档和相关模块文档
2. 开启 DEBUG 日志分析问题
3. 提交 Issue 附带日志和配置信息
