# Anthropic 到 Anthropic 直通准确性修复计划

> **面向 AI 代理的工作者：** 推荐使用 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框语法跟进进度，先写失败测试，再做最小修复，最后运行验证命令。

**目标：** 修复客户端以 Anthropic Messages 格式请求、上游渠道也是 Anthropic Messages 格式时，当前实现不能完全按 Anthropic API 语义直通的问题。

**结论：** 当前实现不是完全准确的直通。非流式请求体基本保持原样，但 URL 拼接、Anthropic 专用请求头、流式 SSE 解析/错误处理、流式响应归档重建仍有偏差。

**架构：** 保持 `source == target` 时不经过 converter 的设计，只收紧同类型 Anthropic 直通周边逻辑：URL 构造、上游 header 构造、SSE 事件解析与统计归档。不要把 Anthropic->Anthropic 引入 `ToAnthropicConverter`，避免破坏真正的请求体直通。

**技术栈：** Python、FastAPI、httpx、pytest、ruff。

---

## 审查发现

1. **[必须修复] Anthropic `base_url` 配置为 `/v1` 时会拼出重复路径**
   - 位置：`proxy_core.py` 的 `_get_upstream_url()`。
   - 当前逻辑只判断 `base.endswith("/messages")`，如果渠道配置为 `https://api.anthropic.com/v1`，最终 URL 会变成 `https://api.anthropic.com/v1/v1/messages`。
   - 影响：这是 Anthropic 官方常见 base URL 形态，会导致同类型直通请求直接打错上游地址。

2. **[必须修复] 同类型 Anthropic 直通会丢弃客户端发送的 Anthropic 版本/ beta 头**
   - 位置：`client.py` 的 `get_upstream_headers()` 与 `proxy_core.py` 的 `_SKIP_HEADERS`。
   - 当前固定发送 `anthropic-version: 2023-06-01`，并且把客户端 `anthropic-version`、`anthropic-beta` 都过滤掉；只有渠道配置了 `channel.anthropic_beta` 才会发送 beta。
   - 影响：客户端请求体虽然原样透传，但需要 beta 头配合的新 Anthropic 参数会失败；客户端指定 API version 也无法生效。`x-api-key` 仍必须由渠道密钥覆盖，不能透传客户端认证头。

3. **[必须修复] Anthropic SSE 解析不是完整 SSE 语义**
   - 位置：`proxy_core.py` 的 `_do_stream_request()` 行级解析。
   - 当前按单行 `event:` / `data:` 处理，遇到多行 `data:`、SSE 注释、`id:`、`retry:` 时不能按事件块处理。第二个 `data:` 行还可能丢失 `event:` 类型。
   - 影响：官方当前示例多为单行 JSON，但 SSE 协议允许多行数据；作为直通代理，应至少保证 Anthropic 事件块语义不被破坏。

4. **[必须修复] 上游 `event: error` 流式错误会被统计为成功，也不会触发首包前故障转移**
   - 位置：`proxy_core.py` 的 `_do_stream_request()`。
   - 当前 `event: error` 会被当作普通事件转发，循环结束后 `stream_success = True`，负载均衡记录成功。
   - 影响：真实失败渠道会被认为健康；如果错误事件是上游第一包，也不会像 HTTP 5xx/连接失败一样尝试备用 Anthropic 渠道。

5. **[建议修复] 流式响应归档重建会丢失 Anthropic 内容块结构**
   - 位置：`proxy_core.py` 的 `_build_anthropic_stream_response()`。
   - 当前把所有文本 delta 合并成一个 text block，把 thinking 合并成一个 block，并忽略 `signature_delta`、`citations_delta`、`stop_sequence`、扩展 usage 字段，以及多内容块顺序。
   - 影响：客户端实际收到的 SSE 基本可用，但统计/请求记录里的 `response_body` 不是精确 Anthropic 响应。

## 文件结构

- 修改：`proxy_core.py`
  - 修复 Anthropic URL 拼接。
  - 抽出 Anthropic 同类型直通的上游 header 合并逻辑。
  - 把流式解析从“逐行即时处理”改为“按 SSE event block 处理”。
  - 识别上游 Anthropic `event: error`，修正故障转移和统计。
  - 精确重建 Anthropic 流式响应归档。
- 修改：`client.py`
  - 保持渠道认证头生成职责；必要时支持传入已决策的 Anthropic version/beta。
- 修改：`models/channel.py`
  - 可选新增 `anthropic_version: Optional[str] = None`，用于渠道级版本覆盖；默认仍为 `2023-06-01`。
- 修改：`tests/test_proxy_core.py`
  - 增加同类型 Anthropic URL、header、SSE、错误事件、归档重建回归测试。
- 修改：`tests/test_client.py`
  - 增加 Anthropic version/beta header 生成策略测试。
- 修改：`static/index.html`（可选）
  - 如果新增 `anthropic_version` 字段，则在渠道管理表单中暴露；若只支持请求头透传则不改 UI。

---

### 任务 1：补齐 Anthropic `/v1` base URL 回归测试并修复

**文件：**
- 修改：`tests/test_proxy_core.py`
- 修改：`proxy_core.py`

- [ ] **步骤 1：添加失败测试**

在 `TestGetUpstreamUrl` 中添加：

```python
def test_anthropic_base_url_ending_v1(self):
    ch = Channel(
        name="Anthropic",
        api_type=APIType.ANTHROPIC,
        base_url="https://api.anthropic.com/v1",
        api_key="ak-test",
    )

    assert _get_upstream_url(ch) == "https://api.anthropic.com/v1/messages"
```

运行：

```bash
uv run pytest tests/test_proxy_core.py::TestGetUpstreamUrl::test_anthropic_base_url_ending_v1 -q
```

预期：当前失败，实际值包含 `/v1/v1/messages`。

- [ ] **步骤 2：实现 URL 拼接修复**

在 `proxy_core.py` 增加小 helper，避免每种 API 手写重复判断：

```python
def _append_api_path(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.endswith(path):
        return base
    if base.endswith("/v1"):
        return f"{base}{path}"
    return f"{base}/v1{path}"
```

然后把 Anthropic 分支改为：

```python
elif actual_type == "anthropic":
    return _append_api_path(base, "/messages")
```

OpenAI 两个分支可先保持现状；如果顺手迁移，也要补对应测试，避免扩大无测试改动。

- [ ] **步骤 3：运行 URL 相关测试**

```bash
uv run pytest tests/test_proxy_core.py::TestGetUpstreamUrl -q
```

预期：全部通过。

---

### 任务 2：修正同类型 Anthropic 直通的 header 策略

**文件：**
- 修改：`models/channel.py`
- 修改：`client.py`
- 修改：`proxy_core.py`
- 修改：`tests/test_client.py`
- 修改：`tests/test_proxy_core.py`

- [ ] **步骤 1：写明目标策略**

实现以下优先级：

```text
x-api-key:
  始终使用 channel.api_key，永不透传客户端 x-api-key。

authorization:
  永不透传到 Anthropic 上游。

anthropic-version:
  channel.anthropic_version 如果配置则优先；
  否则同类型 Anthropic 直通时使用客户端 anthropic-version；
  否则使用默认 2023-06-01。

anthropic-beta:
  channel.anthropic_beta 如果配置则优先；
  否则同类型 Anthropic 直通时使用客户端 anthropic-beta；
  否则不发送。
```

- [ ] **步骤 2：为 `anthropic_version` 添加模型字段**

在 `models/channel.py` 的 `Channel`、`ChannelCreate`、`ChannelUpdate` 中添加：

```python
anthropic_version: Optional[str] = None
```

- [ ] **步骤 3：增加 header 生成单元测试**

在 `tests/test_client.py::TestGetUpstreamHeaders` 添加：

```python
def test_anthropic_version_can_be_channel_configured(self, anthropic_channel):
    anthropic_channel.anthropic_version = "2023-06-01"
    headers = client.get_upstream_headers(anthropic_channel)

    assert headers["anthropic-version"] == "2023-06-01"
```

- [ ] **步骤 4：增加同类型请求头透传失败测试**

在 `tests/test_proxy_core.py::TestAnthropicHeaderPriority` 添加：

```python
@pytest.mark.anyio
async def test_same_type_anthropic_passes_client_version_and_beta_when_channel_not_configured(self):
    captured_headers = {}

    class FakeClient:
        async def post(self, url, json, headers):
            captured_headers.update(headers)
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "model": "claude-3",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
                request=request,
            )

    channel = Channel(
        id="ch_1",
        name="Anthropic",
        api_type=APIType.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="ak-channel",
        models=["claude-3"],
    )

    await _do_request(
        channel,
        {"model": "claude-3", "messages": []},
        APIType.ANTHROPIC,
        is_stream=False,
        client_headers={
            "x-api-key": "client-proxy-key",
            "authorization": "Bearer client-proxy-key",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "client-beta",
        },
    )

    assert captured_headers["x-api-key"] == "ak-channel"
    assert "authorization" not in {k.lower(): v for k, v in captured_headers.items()}
    assert captured_headers["anthropic-version"] == "2023-06-01"
    assert captured_headers["anthropic-beta"] == "client-beta"
```

用 `patch("proxy_core.create_client", ...)`、`patch("proxy_core.stats.record_request")` 包住上面的调用，保持现有测试风格。

- [ ] **步骤 5：实现 `_build_upstream_headers()`**

在 `proxy_core.py` 中把 `_do_request()` 内 header 拼装抽出：

```python
def _build_upstream_headers(
    channel: Channel,
    target_api_type: APIType,
    same_type_passthrough: bool,
    client_headers: dict[str, str] | None,
) -> dict:
    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    incoming = {k.lower(): v for k, v in (client_headers or {}).items()}

    if channel.api_type == APIType.ANTHROPIC:
        configured_version = getattr(channel, "anthropic_version", None)
        if configured_version:
            headers["anthropic-version"] = configured_version
        elif same_type_passthrough and incoming.get("anthropic-version"):
            headers["anthropic-version"] = incoming["anthropic-version"]

        if not channel.anthropic_beta and same_type_passthrough and incoming.get("anthropic-beta"):
            headers["anthropic-beta"] = incoming["anthropic-beta"]

    skip_headers = {
        "host",
        "authorization",
        "x-api-key",
        "content-type",
        "content-length",
        "anthropic-version",
        "anthropic-beta",
    }
    for key, val in (client_headers or {}).items():
        if key.lower() not in skip_headers:
            headers[key] = val

    return headers
```

然后 `_do_request()` 使用：

```python
headers = _build_upstream_headers(channel, target_api_type, same_type_passthrough, client_headers)
```

- [ ] **步骤 6：保留渠道级 beta 优先测试**

现有 `test_client_headers_do_not_override_anthropic_channel_config` 应继续通过；如果新增 `anthropic_version`，再补一个渠道级 version 覆盖客户端 version 的断言。

- [ ] **步骤 7：运行 header 相关测试**

```bash
uv run pytest tests/test_client.py::TestGetUpstreamHeaders tests/test_proxy_core.py::TestAnthropicHeaderPriority -q
```

预期：全部通过。

---

### 任务 3：按 SSE 事件块解析 Anthropic 流式响应

**文件：**
- 修改：`proxy_core.py`
- 修改：`tests/test_proxy_core.py`

- [ ] **步骤 1：增加多行 data 的失败测试**

在 `TestDoStreamRequest` 中添加：

```python
@pytest.mark.anyio
async def test_same_type_anthropic_stream_preserves_event_type_for_multiline_data(self):
    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield "event: ping"
            yield 'data: {"type":"ping",'
            yield 'data: "extra":true}'
            yield ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def stream(self, *args, **kwargs):
            return FakeStreamResponse()

        async def aclose(self):
            return None

    channel = Channel(
        id="ch_1",
        name="Anthropic",
        api_type=APIType.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="ak-test",
        models=["claude-3"],
    )

    with patch("proxy_core.create_stream_client", return_value=FakeClient()), \
            patch("proxy_core.stats.record_request"):
        stream = _do_stream_request(
            channel=channel,
            url="https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            upstream_data={"model": "claude-3", "stream": True},
            response_converter=None,
            source_type="anthropic",
            target_api_type=APIType.ANTHROPIC,
        )
        outputs = [chunk async for chunk in stream]

    joined = "".join(outputs)
    assert "event: ping" in joined
    assert '"extra": true' in joined
    for block in joined.strip().split("\n\n"):
        assert block.startswith("event: ")
```

运行：

```bash
uv run pytest tests/test_proxy_core.py::TestDoStreamRequest::test_same_type_anthropic_stream_preserves_event_type_for_multiline_data -q
```

预期：当前失败或输出缺少正确事件块。

- [ ] **步骤 2：实现 SSE block parser**

在 `proxy_core.py` 增加内部 helper：

```python
async def _iter_sse_blocks(lines):
    event_type = None
    data_lines = []
    passthrough_lines = []

    async for line in lines:
        if not line.strip():
            if event_type or data_lines or passthrough_lines:
                yield event_type, data_lines, passthrough_lines
            event_type = None
            data_lines = []
            passthrough_lines = []
            continue

        if line.startswith(":"):
            passthrough_lines.append(line)
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        else:
            passthrough_lines.append(line)

    if event_type or data_lines or passthrough_lines:
        yield event_type, data_lines, passthrough_lines
```

在 `_do_stream_request()` 中用该 helper 替换逐行 `event:`/`data:` 状态机。对 Anthropic/OpenAI Responses 这类 event SSE，`event_type` 必须随 data 一起处理，避免 data 行分裂后丢类型。

- [ ] **步骤 3：处理非 JSON data 与非 SSE JSON fallback**

实现规则：

```text
如果第一个 block 既没有 event/data，又像普通 JSON 文本：
  走现有 nonlocal_stream_body fallback。

如果 data_lines 合并后不是 JSON：
  同类型直通时按原 event_type 重新输出原 data 文本；
  转换路径仍抛 ConverterError，避免 converter 接收未知结构。
```

同类型 Anthropic raw 输出 helper：

```python
def _format_raw_sse(event_type: str | None, data: str) -> str:
    lines = []
    if event_type:
        lines.append(f"event: {event_type}")
    for data_line in data.splitlines() or [""]:
        lines.append(f"data: {data_line}")
    return "\n".join(lines) + "\n\n"
```

- [ ] **步骤 4：保留现有直通测试**

以下测试必须继续通过：

```bash
uv run pytest tests/test_proxy_core.py::TestDoStreamRequest::test_same_type_anthropic_stream_does_not_leak_event_type tests/test_proxy_core.py::TestAnthropicNonSseJsonFallback::test_anthropic_non_sse_json_produces_event_lines -q
```

---

### 任务 4：识别 Anthropic 流式 error 事件并修正故障转移/统计

**文件：**
- 修改：`proxy_core.py`
- 修改：`tests/test_proxy_core.py`

- [ ] **步骤 1：添加首事件 error 时故障转移测试**

在 `TestAnthropicSameTypeFailover` 中添加：

```python
@pytest.mark.anyio
async def test_anthropic_stream_error_event_before_output_fails_over(self):
    class ErrorStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield "event: error"
            yield 'data: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}'
            yield ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class WorkingStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield "event: message_start"
            yield 'data: {"type":"message_start","message":{"id":"msg_fb","type":"message","role":"assistant","content":[],"model":"claude-3","usage":{"input_tokens":1,"output_tokens":0}}}'
            yield ""
            yield "event: content_block_start"
            yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
            yield ""
            yield "event: content_block_delta"
            yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}'
            yield ""
            yield "event: content_block_stop"
            yield 'data: {"type":"content_block_stop","index":0}'
            yield ""
            yield "event: message_delta"
            yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}'
            yield ""
            yield "event: message_stop"
            yield 'data: {"type":"message_stop"}'
            yield ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class ErrorClient:
        def stream(self, *args, **kwargs):
            return ErrorStreamResponse()

        async def aclose(self):
            return None

    class WorkingClient:
        def stream(self, *args, **kwargs):
            return WorkingStreamResponse()

        async def aclose(self):
            return None

    primary = Channel(
        id="ch_primary",
        name="Primary",
        api_type=APIType.ANTHROPIC,
        base_url="https://primary.example",
        api_key="ak-primary",
        models=["claude-3"],
        priority=1,
    )
    fallback = Channel(
        id="ch_fallback",
        name="Fallback",
        api_type=APIType.ANTHROPIC,
        base_url="https://fallback.example",
        api_key="ak-fallback",
        models=["claude-3"],
        priority=2,
    )

    def fake_stream_client(ch):
        return ErrorClient() if ch.id == "ch_primary" else WorkingClient()

    with patch("proxy_core._get_channels_for_model", new_callable=AsyncMock, return_value=[primary, fallback]), \
            patch("proxy_core.create_stream_client", side_effect=fake_stream_client), \
            patch("proxy_core.stats.record_request"):
        stream, selected = await _proxy_single_model_request(
            model="claude-3",
            request_data={"model": "claude-3", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            target_api_type=APIType.ANTHROPIC,
            is_stream=True,
            query_string=None,
            client_headers=None,
            api_key_id=None,
        )
        outputs = [chunk async for chunk in stream]

    assert selected.id == "ch_fallback"
    assert "ok" in "".join(outputs)
```

- [ ] **步骤 2：实现 Anthropic stream error 类型**

在 `proxy_core.py` 增加：

```python
class _UpstreamStreamErrorEvent(Exception):
    def __init__(self, event: dict[str, Any]):
        self.event = event
        error = event.get("error", {})
        super().__init__(error.get("message") or "upstream stream error")
```

在 `_do_stream_request()` 解析出 chunk 后：

```python
if is_upstream_anthropic and (upstream_event_type == "error" or chunk.get("type") == "error"):
    if not emitted_output:
        raise _StreamPreflightError(_UpstreamStreamErrorEvent(chunk))
    stream_error = chunk.get("error", {}).get("message") or "upstream stream error"
    # 继续把 error event 透传给客户端，但最终不要标记 success。
```

注意：不要在已经向客户端输出正文后切换渠道；那会破坏一个 HTTP stream 的一致性。

- [ ] **步骤 3：调整 finally 里的 success 记录**

不要在循环结束时无条件 `stream_success = True`。改为：

```python
if stream_error is None:
    stream_success = True
```

并确保 `load_balancer.record_success(channel.id)` 只在 `stream_success` 为真时执行；错误事件后应记录 failure。

- [ ] **步骤 4：运行故障转移相关测试**

```bash
uv run pytest tests/test_proxy_core.py::TestAnthropicSameTypeFailover -q
```

预期：全部通过。

---

### 任务 5：提高 Anthropic 流式响应归档重建准确性

**文件：**
- 修改：`proxy_core.py`
- 修改：`tests/test_proxy_core.py`

- [ ] **步骤 1：为多内容块顺序添加测试**

新增测试直接调用 `_build_anthropic_stream_response()`，覆盖 text、tool_use、thinking signature 和 stop_sequence：

```python
def test_build_anthropic_stream_response_preserves_block_order_and_signature():
    chunks = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "claude-3",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 3,
                },
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_1"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hello"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "calc", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"x":'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "1}"}},
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 7},
        },
    ]

    response = _build_anthropic_stream_response(chunks, "claude-3")

    assert response["content"] == [
        {"type": "thinking", "thinking": "plan", "signature": "sig_1"},
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "toolu_1", "name": "calc", "input": {"x": 1}},
    ]
    assert response["stop_reason"] == "tool_use"
    assert response["stop_sequence"] is None
    assert response["usage"]["input_tokens"] == 10
    assert response["usage"]["output_tokens"] == 7
    assert response["usage"]["cache_read_input_tokens"] == 3
```

- [ ] **步骤 2：改造 `_build_anthropic_stream_response()`**

实现方式：

```text
用 blocks: dict[int, dict] 按 content_block_start 初始化原始 block。
text_delta 追加到对应 text block。
thinking_delta 追加到对应 thinking block。
signature_delta 写入同一个 thinking block 的 signature。
input_json_delta 追加到 tool_use 的 private buffer，content_block_stop 时尝试 json.loads。
message_delta 同时保留 stop_reason 和 stop_sequence。
usage 字段用 dict merge，保留 cache_creation_input_tokens/cache_read_input_tokens 等扩展字段。
最终按 index 排序输出 blocks。
```

避免再用全局 `content_text`、`thinking_text` 合并所有块。

- [ ] **步骤 3：无效 tool JSON 的处理**

当前无效 JSON 会静默变成 `{}`。改为保留原始字符串到 `_partial_json` 或 `input` 原值二选一。推荐：

```python
try:
    block["input"] = json.loads(buffer) if buffer else {}
except json.JSONDecodeError:
    block["input"] = {}
    block["_partial_json"] = buffer
```

这样记录仍可追溯，不影响客户端已收到的真实 SSE。

- [ ] **步骤 4：运行归档相关测试**

```bash
uv run pytest tests/test_proxy_core.py -q
```

预期：全部通过。

---

### 任务 6：端到端验证与代码检查

**文件：**
- 验证：`proxy_core.py`
- 验证：`client.py`
- 验证：`models/channel.py`
- 验证：`tests/test_proxy_core.py`
- 验证：`tests/test_client.py`

- [ ] **步骤 1：运行同类型 Anthropic 相关测试**

```bash
uv run pytest tests/test_proxy_core.py::TestGetUpstreamUrl tests/test_proxy_core.py::TestDoStreamRequest tests/test_proxy_core.py::TestAnthropicNonSseJsonFallback tests/test_proxy_core.py::TestAnthropicSameTypeFailover tests/test_proxy_core.py::TestAnthropicHeaderPriority tests/test_client.py::TestGetUpstreamHeaders -q
```

预期：全部通过。

- [ ] **步骤 2：运行代理核心和客户端测试**

```bash
uv run pytest tests/test_proxy_core.py tests/test_client.py tests/routers/test_proxy_base.py -q
```

预期：全部通过。

- [ ] **步骤 3：运行 lint**

```bash
uv run ruff check proxy_core.py client.py models/channel.py tests/test_proxy_core.py tests/test_client.py
```

预期：退出码 0。

- [ ] **步骤 4：可选运行完整测试**

```bash
uv run pytest -q
```

预期：全部通过。若环境缺少外部服务或数据库导致非相关测试失败，记录失败测试名和原因，不把它们误判为本次改动回归。

---

## 验收标准

- [ ] Anthropic 渠道 `base_url=https://api.anthropic.com` 和 `base_url=https://api.anthropic.com/v1` 都会请求 `/v1/messages`，不会重复 `/v1`。
- [ ] 同类型 Anthropic 直通时，请求体不经过 converter，也不被 capability filter 改写。
- [ ] 上游 `x-api-key` 始终来自渠道配置；客户端认证头不会泄漏到上游。
- [ ] 客户端 `anthropic-version`、`anthropic-beta` 在没有渠道级覆盖时能传到上游。
- [ ] 渠道级 `anthropic_version`、`anthropic_beta` 优先于客户端请求头。
- [ ] Anthropic SSE 输出始终保留 `event:` 行；多行 `data:` 不会拆坏事件类型。
- [ ] 上游首包 `event: error` 可触发故障转移；输出后发生的 error 事件会透传但记录为失败。
- [ ] 流式请求记录里的 Anthropic `response_body.content` 保留内容块顺序、thinking signature、tool input、stop_sequence 和扩展 usage 字段。

## 参考

- Anthropic Messages API 官方文档：`https://docs.anthropic.com/en/api/messages`
- 本地 Anthropic API 规格整理：`docs/api-spec-anthropic-and-openai/anthropic-api-spec.md`
- 当前直通分派：`proxy_core.py::_get_converter_and_upstream_type`
- 当前上游 header 构造：`client.py::get_upstream_headers`
- 当前流式处理：`proxy_core.py::_do_stream_request`
