# spec-proxy-core — 代理核心模块

> 对应文件：`proxy_core.py`（约 950 行）

## 模块定位

`proxy_core.py` 是整个代理服务的**调度中心**，协调路由层、转换器、负载均衡器、HTTP 客户端、存储层和统计模块。它不直接处理 HTTP 协议细节，而是被路由层调用，专注于"选渠道 → 转换 → 发请求 → 转回 → 故障转移 → 统计记录"这个核心循环。

## 核心函数

### `proxy_request(model, request_data, target_api_type, is_stream, query_string, client_headers, api_key_id, tracked_headers)`

**主入口**，由路由层调用。

```python
async def proxy_request(
    model: str,               # 请求的模型名，用于筛选渠道
    request_data: dict,       # 客户端请求体（原始格式）
    target_api_type: APIType, # 客户端使用的 API 格式
    is_stream: bool = False,  # 是否流式请求
    query_string: str | None = None,  # 原始 URL query string（透传给上游）
    client_headers: dict[str, str] | None = None,  # 需转发的客户端请求头
    api_key_id: str | None = None,    # API Key ID（用于统计）
    tracked_headers: dict[str, str] | None = None,  # 追踪的请求头（用于统计）
) -> tuple[Any, Channel]     # 返回 (响应数据或流式生成器, 选中的渠道)
```

**流程**：

1. 检查 model 是否匹配模型组（model group），若匹配则调用 `_proxy_model_group_request()`，按组内 fallback 顺序依次尝试每个模型
2. 若非模型组，调用 `_get_channels_for_model(model)` 获取匹配的已启用渠道
3. 进入 while 循环（故障转移循环）：
- `load_balancer.select_channel(channels, exclude_ids=all_tried)` 选择渠道
- 调用 `_do_request()` 执行请求
- 成功则返回 `(响应, 渠道)`
- 失败则 `load_balancer.record_failure()`，加入已试集合，继续循环
4. 所有渠道耗尽则抛出最后一次异常

### `_do_request(channel, request_data, target_api_type, is_stream, query_string, client_headers, api_key_id, tracked_headers)`

**单次请求执行**，包含完整的格式转换 + 发送流程。

```python
async def _do_request(
    channel: Channel,
    request_data: dict,
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None = None,
    client_headers: dict[str, str] | None = None,
    api_key_id: str | None = None,
    tracked_headers: dict[str, str] | None = None,
)
```

**流程**：

1. `_get_converter_and_upstream_type(channel, target_api_type)` → 获取转换器和上游类型
2. 如有转换器：`converter.convert_request(request_data, source_type)` → 转换请求体
3. 构建上游 URL 和请求头
4. **分支**：
   - `is_stream=True` → 调用 `_do_stream_request()`，返回异步生成器
   - `is_stream=False` → 使用缓存的 `create_client(channel)` 发送 POST，转换响应，返回 JSON
5. 成功后 `load_balancer.record_success(channel.id)` + `stats.record_request()`
6. 失败时记录调试日志、统计记录并 re-raise

### `_do_stream_request(channel, url, headers, upstream_data, converter, source_type, target_api_type)`

**流式请求处理**，这是一个**异步生成器**，逐行解析上游 SSE 并逐 chunk 转换后 yield 给客户端。

```python
async def _do_stream_request(
    channel: Channel, url: str, headers: dict,
    upstream_data: dict, converter, source_type: str,
    target_api_type: APIType = APIType.OPENAI_CHAT,
)  # AsyncGenerator
```

**关键细节**：

- 使用 `get_or_create_stream_client(channel)` 获取**缓存的**流式 httpx 客户端
- 通过 `client.stream("POST", ...)` 进入流式上下文
- 逐行 `aiter_lines()` 解析 SSE
- Anthropic 上游有 `event:` 行 + `data:` 行；OpenAI 上游仅有 `data:` 行
- 每个 chunk 通过 `converter.convert_stream_chunk()` 转换
- 转换结果可能附带 `_extra_events`，通过 `converter.get_extra_events()` 取出并一起输出
- Anthropic 输出格式需要 `event:` 行（通过 `_yield_anthropic_event()` 生成）
- 流式客户端已缓存，`finally` 块不再手动关闭
- 流式传输中途出错时，向客户端发送错误事件后结束流

## 辅助函数

### `_get_channels_for_model(model)`

从 storage 加载所有渠道，筛选出 `model in channel.models` 且 `channel.enabled` 的渠道。

**缓存机制**：

- 使用 `_model_channels_cache` 按 model 索引渠道列表
- 通过 `register_save_callback()` 注册缓存失效回调
- 当 `save_data()` 被调用时，缓存自动失效，下次请求重新加载

### `_invalidate_model_channels_cache()`

**失效模型渠道缓存**，通过 `storage.register_save_callback()` 注册，在渠道数据变更时自动调用。

### `_get_converter_and_upstream_type(channel, target_api_type)`

根据渠道 API 类型与客户端 API 类型的差异，返回 `(converter, source_type)`：
- 类型相同 → `(None, source_type)`，表示直通
- 类型不同 → 返回对应的 Converter 实例

### `_get_upstream_url(channel)`

根据渠道 `api_type` 拼接上游 URL：
- `openai-chat-completions` → `{base_url}/v1/chat/completions`
- `openai-response` → `{base_url}/v1/responses`
- `anthropic` → `{base_url}/v1/messages`

### `_log_debug(...)`

调试日志记录函数，仅在 `DEBUG=true` 时生效。记录完整的请求/响应信息到 `logs/debug_YYYY-MM-DD.jsonl`。流式响应限制记录前 100 + 后 10 个 chunk 摘要。日志记录失败仅打印警告，不影响主流程。

### `_yield_anthropic_event(event_type, data)` / `_yield_anthropic_events(events)`

生成 Anthropic SSE 格式的文本行（`event: xxx\ndata: {...}\n\n`）。

## 常量

| 常量 | 值 | 说明 |
|------|----|------|
| `MAX_STREAM_CHUNKS` | 2000 | 流式响应最大记录 chunk 数量，防止内存溢出 |

## 与其他模块的交互

```
proxy_core.py
  ├── 导入 balancer.load_balancer     → 选择渠道、记录成功/失败
├── 导入 client.create_client → 非流式请求（缓存客户端）
├── 导入 client.get_or_create_stream_client → 流式请求（缓存流式客户端）
├── 导入 client.create_stream_client → 流式请求（独立客户端，不缓存）
├── 导入 client.get_upstream_headers → 构建上游认证头
  ├── 导入 converters.to_chat         → 格式转换
  ├── 导入 converters.to_response     → 格式转换
  ├── 导入 converters.to_anthropic    → 格式转换
  ├── 导入 storage.load_data          → 加载渠道数据
  ├── 导入 storage.register_save_callback → 注册缓存失效回调
  ├── 导入 stats.record_request       → 记录请求统计
  ├── 导入 config.DEBUG/DEBUG_LOG_DIR → 调试日志配置
  └── 导入 models.channel.Channel     → 渠道数据模型
```

## 统计记录

每次请求（成功或失败）都会调用 `stats.record_request()` 记录以下信息：

| 字段 | 说明 |
|------|------|
| `channel_id` | 渠道 ID |
| `channel_name` | 渠道名称 |
| `model` | 模型名 |
| `is_stream` | 是否流式请求 |
| `input_tokens` | 输入 token 数 |
| `output_tokens` | 输出 token 数 |
| `latency_ms` | 总延迟（毫秒） |
| `lag_ms` | 首 token 延迟（毫秒，仅流式） |
| `success` | 是否成功 |
| `error_msg` | 错误信息（失败时） |
| `finish_reason` | 完成原因 |
| `api_key_id` | API Key ID |
| `headers` | 追踪的请求头 |

## 注意事项

1. **非流式请求使用缓存客户端**：`create_client()` 按 base_url+proxy 缓存，不能 `async with` 关闭。
2. **流式请求使用缓存流式客户端**：`get_or_create_stream_client()` 按 base_url+proxy 缓存流式客户端，`finally` 块不再手动关闭。`create_stream_client()` 用于需要独立客户端的场景（不缓存）。
3. **故障转移循环**：`all_tried` 集合保证同一渠道不会重试。循环直到有渠道成功或全部耗尽。
4. **query_string 透传**：某些 API（如 Anthropic）使用 query 参数（如 `?beta=...`），需要从原始请求透传给上游。
5. **流式中途错误**：流式传输已经开始向客户端写数据后，无法改变 HTTP 状态码，只能通过 SSE 错误事件通知客户端。
6. **统计记录**：无论请求成功或失败，都会调用 `stats.record_request()` 记录统计信息。如果 PostgreSQL 连接失败，统计功能自动禁用。
7. **模型渠道缓存**：`_model_channels_cache` 在 `save_data()` 时自动失效，无需手动清理。
8. **Anthropic 请求头转发**：客户端的 `anthropic-beta`、`anthropic-version` 请求头会被合并转发给上游（逗号分隔）。
