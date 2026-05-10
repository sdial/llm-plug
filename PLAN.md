# OpenAI Responses → OpenAI Chat Completions 完整转换补齐计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 让客户端以 OpenAI Responses 格式请求 `/v1/responses`、上游渠道为 OpenAI Chat Completions 时，所有可由 Chat Completions 表达的能力都正确转换；无法表达的 Responses 能力必须显式拒绝或显式降级，不能静默丢失。

**架构：** 以 `converters/to_chat.py` 负责 Responses 请求 → Chat 请求，以 `converters/to_response.py` 负责 Chat 响应/流 → Responses 响应/流。`routers/proxy_response.py` 继续负责 `previous_response_id` 状态管理，`proxy_core.py` 负责选择 `openai-chat-completions` 上游并套用转换器。新增测试先覆盖失败场景，再逐项补齐转换逻辑。

**技术栈：** Python、FastAPI、httpx、pytest、Pydantic、现有 converter/state store/debug log 架构。

**参考资料：**
- OpenAI Responses API：`https://platform.openai.com/docs/api-reference/responses`
- OpenAI Responses streaming events：`https://platform.openai.com/docs/api-reference/responses-streaming/response/output_text/delta`
- OpenAI Chat Completions API：`https://platform.openai.com/docs/api-reference/chat/create`
- 本地评估：`docs/openai-responses-to-chat-comletions-ref/README.md`
- 现有状态管理设计：`docs/superpowers/specs/2026-05-05-responses-state-management-design.md`

---

## 范围边界

### 必须做到

- 可转换字段必须转换到 Chat Completions 等价字段。
- 不可转换字段必须返回清晰错误，或在显式配置允许时降级并记录 debug log。
- 请求转换、非流式响应转换、流式事件转换、状态存储和测试都要覆盖。
- `previous_response_id` 必须通过本地状态存储展开为完整 Chat `messages`。
- 不允许静默丢弃会改变语义的字段。

### 不承诺无损支持

以下 Responses 能力不能只靠 Chat Completions 上游无损实现：

- 托管工具：`web_search`、`file_search`、`code_interpreter`、`computer_use`、`image_generation`、`mcp` 等。
- 后台响应：`background`。
- OpenAI 服务端会话：`conversation`。
- OpenAI 服务端上下文管理：`context_management`。
- 需要 Responses 服务端返回的 `include` 扩展，例如加密 reasoning 内容、托管工具中间结果。

这些能力的完成标准是：**检测出来，并给出明确错误或明确的本地适配入口**。

---

## 预计修改文件

| 文件 | 动作 | 职责 |
|------|------|------|
| `converters/to_chat.py` | 修改 | 补齐 Responses 请求到 Chat 请求的字段、内容、工具和错误处理 |
| `converters/to_response.py` | 修改 | 补齐 Chat 响应和 Chat 流到 Responses 响应/事件的结构 |
| `routers/proxy_response.py` | 修改 | 补齐历史展开、状态保存、流式完成响应提取 |
| `proxy_core.py` | 小改 | 必要时补齐流式 usage、错误传播和转换错误 HTTP 映射 |
| `tests/converters/test_response_to_chat.py` | 修改 | 请求转换单元测试 |
| `tests/converters/test_stream_sequences.py` | 修改 | Chat 流 → Responses 流事件序列测试 |
| `tests/test_responses_full_flow.py` | 修改 | `/v1/responses` 端到端转换测试 |
| `tests/test_proxy_core_responses.py` | 修改 | proxy core + Responses 状态测试 |
| `docs/api-spec-anthropic-and-openai/openai-api-spec.md` | 修改 | 更新转换能力矩阵 |

---

## 任务 1：建立转换契约和字段矩阵

**文件：**
- 修改：`docs/api-spec-anthropic-and-openai/openai-api-spec.md`
- 修改：`tests/converters/test_response_to_chat.py`

- [ ] **步骤 1：列出 Responses 请求字段处理策略**

在文档中增加「Responses → Chat Completions 转换策略」表，至少包含：

| Responses 字段 | Chat 字段 | 策略 |
|----------------|-----------|------|
| `model` | `model` | 透传 |
| `input` | `messages` | 转换 |
| `instructions` | `messages` 中的 `system` | 转换 |
| `tools` 中的 `function` | `tools[].function` | 转换 |
| 托管工具 | 无 | 拒绝或适配器 |
| `tool_choice` | `tool_choice` | 转换 |
| `parallel_tool_calls` | `parallel_tool_calls` | 按渠道能力透传或拒绝 |
| `max_output_tokens` | `max_tokens` 或 `max_completion_tokens` | 转换 |
| `reasoning.effort` | `reasoning_effort` | 按渠道能力转换 |
| `text.format` | `response_format` | 转换可表达格式 |
| `previous_response_id` | 本地历史展开 | 路由层处理 |
| `conversation` | 无 | 拒绝 |
| `background` | 无 | 拒绝 |
| `include` | 无等价能力 | 限定白名单，否则拒绝 |

- [ ] **步骤 2：新增字段矩阵测试骨架**

在 `tests/converters/test_response_to_chat.py` 增加测试类：

```python
class TestResponseRequestFieldContract:
    """Responses 请求字段转换契约。"""
```

- [ ] **步骤 3：为当前会静默丢失的字段写失败测试**

至少新增这些测试名：

```python
def test_rejects_background_mode(self): ...
def test_rejects_conversation_field(self): ...
def test_rejects_hosted_tools_without_adapter(self): ...
def test_maps_reasoning_effort_to_chat_reasoning_effort(self): ...
def test_maps_text_json_schema_to_response_format(self): ...
def test_maps_safety_identifier_to_user(self): ...
```

- [ ] **步骤 4：运行测试确认失败**

运行：

```bash
uv run pytest tests/converters/test_response_to_chat.py -q
```

预期：新增测试失败，失败原因指向未实现字段映射或未抛出错误。

---

## 任务 2：补齐 function tools 和 `tool_choice` 转换

**文件：**
- 修改：`converters/to_chat.py`
- 修改：`tests/converters/test_response_to_chat.py`

- [ ] **步骤 1：为 `strict` 透传写失败测试**

输入：

```json
{
  "type": "function",
  "name": "get_weather",
  "description": "Get weather",
  "parameters": {"type": "object"},
  "strict": true
}
```

预期输出：

```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "Get weather",
    "parameters": {"type": "object"},
    "strict": true
  }
}
```

- [ ] **步骤 2：为 function `tool_choice` 写失败测试**

输入：

```json
{"tool_choice": {"type": "function", "name": "get_weather"}}
```

预期输出：

```json
{"tool_choice": {"type": "function", "function": {"name": "get_weather"}}}
```

- [ ] **步骤 3：实现 `_response_tools_to_chat()` 的 `strict` 透传**

在 `converters/to_chat.py` 中让 function tool 的 `strict` 字段进入 `function.strict`。

- [ ] **步骤 4：实现 `_response_tool_choice_to_chat()`**

新增私有方法，处理：

| Responses `tool_choice` | Chat `tool_choice` |
|-------------------------|--------------------|
| `"auto"` | `"auto"` |
| `"none"` | `"none"` |
| `"required"` | `"required"` |
| `{"type": "function", "name": "x"}` | `{"type": "function", "function": {"name": "x"}}` |
| 其他对象 | 抛出 `ValueError` |

- [ ] **步骤 5：运行工具转换测试**

运行：

```bash
uv run pytest tests/converters/test_response_to_chat.py::TestResponseRequestToChat -q
```

预期：工具定义和 `tool_choice` 测试通过。

---

## 任务 3：处理托管工具和不可转换工具

**文件：**
- 修改：`converters/to_chat.py`
- 修改：`tests/converters/test_response_to_chat.py`
- 修改：`routers/proxy_response.py`

- [ ] **步骤 1：定义不可直接转换工具集合**

在 `converters/to_chat.py` 中定义：

```python
HOSTED_RESPONSE_TOOL_TYPES = {
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "computer_use",
    "image_generation",
    "mcp",
}
```

- [ ] **步骤 2：为每类托管工具写参数化失败测试**

测试 `tools` 中出现这些类型时，转换器抛出 `ValueError`，错误信息包含工具类型和「Chat Completions upstream does not support hosted Responses tools」。

- [ ] **步骤 3：为 `input` 中的托管工具调用项写失败测试**

覆盖输入项类型：

```python
web_search_call
file_search_call
code_interpreter_call
computer_call
image_generation_call
```

预期：转换器抛出 `ValueError`，不生成残缺 `messages`。

- [ ] **步骤 4：实现工具拒绝逻辑**

在 `_response_tools_to_chat()` 和 `_response_request_to_chat()` 中检测不可转换工具，抛出 `ValueError`。

- [ ] **步骤 5：确认路由返回 400**

在 `tests/test_responses_full_flow.py` 增加集成测试：客户端发送托管工具到 `/v1/responses`，预期 HTTP 400，响应体说明该工具不支持 Chat Completions 上游。

---

## 任务 4：补齐 Responses `input` 内容块转换

**文件：**
- 修改：`converters/to_chat.py`
- 修改：`tests/converters/test_response_to_chat.py`

- [ ] **步骤 1：覆盖 `input_text` + `input_image` 混合内容**

测试输入：

```json
[
  {"type": "input_text", "text": "Describe this image"},
  {"type": "input_image", "image_url": "https://example.com/a.png", "detail": "high"}
]
```

预期 Chat content：

```json
[
  {"type": "text", "text": "Describe this image"},
  {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "high"}}
]
```

- [ ] **步骤 2：覆盖 `input_file` 转换或拒绝策略**

如果当前 Chat Completions 上游 schema 支持 `{"type": "file", "file": ...}`，映射：

| Responses | Chat |
|-----------|------|
| `file_id` | `file.file_id` |
| `filename` | `file.filename` |
| `file_data` | `file.file_data` |

如果渠道能力未声明支持 file content，则抛出 `ValueError`。

- [ ] **步骤 3：覆盖 `input_audio` 转换或拒绝策略**

如果当前 Chat Completions 上游 schema 支持 `input_audio`，保留 `input_audio` 内容块；否则抛出 `ValueError`。

- [ ] **步骤 4：覆盖 `refusal` 内容块转换**

Responses 或 Chat 中出现拒绝内容时，转换为 Chat 可表达的 `{"type": "refusal", "refusal": "..."}`，或者在上游不支持时转为文本并记录 debug log。

- [ ] **步骤 5：实现 `_response_content_to_chat_content()` 完整分派**

将当前只处理文本和图片的逻辑扩展为显式分派：

```python
if part_type == "input_text": ...
elif part_type == "input_image": ...
elif part_type == "input_file": ...
elif part_type == "input_audio": ...
elif part_type == "refusal": ...
else: raise ValueError(...)
```

- [ ] **步骤 6：运行内容块测试**

运行：

```bash
uv run pytest tests/converters/test_response_to_chat.py::TestResponseRequestToChat -q
```

预期：内容块转换测试通过，未知内容块不再静默丢失。

---

## 任务 5：补齐请求级参数映射

**文件：**
- 修改：`converters/to_chat.py`
- 修改：`tests/converters/test_response_to_chat.py`

- [ ] **步骤 1：为采样和输出控制参数写测试**

覆盖：

```python
max_output_tokens -> max_tokens
temperature -> temperature
top_p -> top_p
stop -> stop
```

如果项目决定用 `max_completion_tokens` 替代 `max_tokens`，测试必须固定这一策略。

- [ ] **步骤 2：为 `reasoning.effort` 写测试**

输入：

```json
{"reasoning": {"effort": "medium"}}
```

预期：

```json
{"reasoning_effort": "medium"}
```

- [ ] **步骤 3：为 `text.format` 写测试**

覆盖：

| Responses `text.format.type` | Chat `response_format` |
|------------------------------|------------------------|
| `text` | 省略 |
| `json_object` | `{"type": "json_object"}` |
| `json_schema` | `{"type": "json_schema", "json_schema": ...}` |

- [ ] **步骤 4：为 `metadata`、`safety_identifier`、`prompt_cache_key` 写测试**

策略：

| Responses 字段 | Chat 策略 |
|----------------|-----------|
| `safety_identifier` | 映射到 `user` |
| `user` | 兼容映射到 `user` |
| `prompt_cache_key` | 若 Chat 上游不支持则不透传，保留在 debug log |
| `metadata` | 不透传到 Chat 请求，保留在状态响应或 debug log |

- [ ] **步骤 5：为不支持的请求级字段写拒绝测试**

字段：

```python
background
conversation
context_management
```

预期：抛出 `ValueError`。

- [ ] **步骤 6：实现 `_validate_response_request_for_chat()`**

在转换入口先执行校验，拒绝不可转换字段，并返回明确错误信息。

- [ ] **步骤 7：运行请求参数测试**

运行：

```bash
uv run pytest tests/converters/test_response_to_chat.py -q
```

预期：所有请求转换测试通过。

---

## 任务 6：修正 `previous_response_id` 历史展开和状态保存

**文件：**
- 修改：`routers/proxy_response.py`
- 修改：`tests/test_responses_full_flow.py`
- 修改：`tests/test_proxy_core_responses.py`

- [ ] **步骤 1：为历史中的 function call 写集成测试**

流程：

1. 第一次 `/v1/responses` 返回 `output[].function_call`。
2. 第二次请求带 `previous_response_id` 和 `function_call_output`。
3. 断言上游 Chat 请求包含 assistant `tool_calls` 和 tool message。

- [ ] **步骤 2：为历史中的 reasoning item 写测试**

如果 Chat 上游可表达 `reasoning_content`，保存并回放；否则保存时保留但回放时跳过并记录 debug log。

- [ ] **步骤 3：修正 `_response_output_to_items()`**

确保保存的历史至少覆盖：

```python
message
function_call
reasoning
```

不可回放的输出项必须保存在 response 原文中，不能破坏 `GET /v1/responses/{id}`。

- [ ] **步骤 4：确保 `instructions` 继承规则正确**

规则：

- 新请求显式带 `instructions` 时使用新值。
- 新请求不带 `instructions` 时继承历史中的 instructions。
- 不把历史 instructions 重复插入为多条 system message。

- [ ] **步骤 5：运行状态流测试**

运行：

```bash
uv run pytest tests/test_responses_full_flow.py tests/test_proxy_core_responses.py -q
```

预期：多轮文本、多轮工具、历史缺失、`store: false` 全部通过。

---

## 任务 7：补齐非流式 Chat 响应 → Responses 响应

**文件：**
- 修改：`converters/to_response.py`
- 修改：`tests/converters/test_stream_sequences.py`
- 修改：`tests/test_responses_full_flow.py`

- [ ] **步骤 1：为标准 Responses 响应字段写测试**

非流式转换结果必须包含：

```python
id
object == "response"
created_at
model
status
output
output_text
usage
```

- [ ] **步骤 2：修正响应 ID 策略**

如果上游返回 `chatcmpl-*`，转换后生成或映射为 `resp_*`。保留原始 ID 到内部字段时只能用扩展字段，例如 `_upstream_id`，不能破坏标准 `id`。

- [ ] **步骤 3：补齐 `output_text` 聚合字段**

从所有 `output[].message.content[].output_text.text` 聚合出 `output_text`。

- [ ] **步骤 4：补齐 finish reason 到 status 映射**

| Chat `finish_reason` | Responses `status` | `incomplete_details` |
|----------------------|--------------------|----------------------|
| `stop` | `completed` | 无 |
| `tool_calls` | `completed` | 无 |
| `length` | `incomplete` | `{"reason": "max_output_tokens"}` |
| `content_filter` | `incomplete` 或 `failed` | 写明策略并测试 |

- [ ] **步骤 5：补齐 tool call 输出项字段**

`choices[].message.tool_calls[]` 转换为：

```json
{
  "type": "function_call",
  "id": "fc_...",
  "call_id": "call_...",
  "name": "...",
  "arguments": "...",
  "status": "completed"
}
```

- [ ] **步骤 6：补齐 usage details 映射**

映射：

```python
prompt_tokens -> input_tokens
completion_tokens -> output_tokens
total_tokens -> total_tokens
prompt_tokens_details.cached_tokens -> input_tokens_details.cached_tokens
completion_tokens_details.reasoning_tokens -> output_tokens_details.reasoning_tokens
```

- [ ] **步骤 7：运行非流式响应测试**

运行：

```bash
uv run pytest tests/converters/test_stream_sequences.py tests/test_responses_full_flow.py -q
```

预期：非流式 Responses 结构测试通过。

---

## 任务 8：重构流式转换状态机

**文件：**
- 修改：`converters/to_response.py`
- 修改：`tests/converters/test_stream_sequences.py`

- [ ] **步骤 1：为事件顺序写 golden test**

文本流预期顺序：

```text
response.created
response.in_progress
response.output_item.added
response.content_part.added
response.output_text.delta
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
```

- [ ] **步骤 2：为每个事件补齐索引字段测试**

`response.output_text.delta` 必须包含：

```python
item_id
output_index
content_index
delta
sequence_number
```

- [ ] **步骤 3：替换单一 `output_item_added_sent` 布尔状态**

改为按输出项维护状态：

```python
output_items: list[dict]
active_text_item_id: str | None
tool_call_index_to_output_index: dict[int, int]
sequence_number: int
```

- [ ] **步骤 4：实现统一事件生成方法**

新增私有方法：

```python
def _next_sequence_number(self) -> int: ...
def _make_response_event(self, event_type: str, **payload) -> dict[str, Any]: ...
def _queue_events(self, events: list[dict[str, Any]]) -> None: ...
```

- [ ] **步骤 5：补齐文本流事件**

当 Chat chunk 出现 `delta.content`：

1. 首次文本时生成 `response.output_item.added`。
2. 首次文本内容时生成 `response.content_part.added`。
3. 每段文本生成 `response.output_text.delta`。
4. finish 时生成 `response.output_text.done`、`response.content_part.done`、`response.output_item.done`。

- [ ] **步骤 6：补齐工具调用流事件**

当 Chat chunk 出现 `delta.tool_calls`：

1. function name 首次出现时生成 `response.output_item.added`。
2. arguments 增量生成 `response.function_call_arguments.delta`。
3. finish 时生成 `response.function_call_arguments.done` 和 `response.output_item.done`。

- [ ] **步骤 7：补齐 usage-only chunk 处理**

当 Chat chunk `choices == []` 且包含 `usage`，只更新 usage，不输出空事件。

- [ ] **步骤 8：运行流式序列测试**

运行：

```bash
uv run pytest tests/converters/test_stream_sequences.py -q
```

预期：文本流、工具流、多工具流、usage-only chunk 全部通过。

---

## 任务 9：修正 `proxy_core.py` 的 Responses SSE 输出

**文件：**
- 修改：`proxy_core.py`
- 修改：`tests/test_proxy_core.py`

- [ ] **步骤 1：为 Responses SSE event 行写测试**

断言输出块包含：

```text
event: response.output_text.delta
data: {...}
```

不能只输出 `data:`。

- [ ] **步骤 2：为 `[DONE]` 策略写测试**

Responses SSE 不应依赖 Chat 风格 `data: [DONE]` 作为语义完成；必须以 `response.completed`、`response.failed` 或 `response.incomplete` 结束。

- [ ] **步骤 3：确保转换器 `finalize_stream()` 只执行一次**

当上游已通过 finish chunk 生成 `response.completed` 后，`[DONE]` 分支不能重复生成 completed 事件。

- [ ] **步骤 4：修正流式错误事件**

上游流已开始后失败时，输出：

```text
event: error
data: {"type": "error", ...}

event: response.failed
data: {"type": "response.failed", "response": {...}}
```

- [ ] **步骤 5：运行 proxy core 流式测试**

运行：

```bash
uv run pytest tests/test_proxy_core.py -q
```

预期：Responses SSE event 行、完成事件、失败事件测试通过。

---

## 任务 10：补齐渠道能力过滤和错误策略

**文件：**
- 修改：`capability_manager.py`
- 修改：`proxy_core.py`
- 修改：`tests/test_capability_manager.py`
- 修改：`tests/test_responses_full_flow.py`

- [ ] **步骤 1：扩展 Chat 上游能力字段**

能力模型至少包含：

```python
supports_parallel_tool_calls
supports_response_format
supports_reasoning_effort
supports_file_content
supports_audio_content
supports_tool_choice_required
supports_strict_tools
```

- [ ] **步骤 2：为能力降级写测试**

覆盖：

| 请求字段 | 渠道不支持时 |
|----------|--------------|
| `parallel_tool_calls: true` | 删除或 400，按配置固定 |
| `text.format.json_schema` | 400 |
| `reasoning.effort` | 400 或删除，按配置固定 |
| `input_file` | 400 |
| `input_audio` | 400 |

- [ ] **步骤 3：实现 Responses 请求转换前能力过滤**

在 `_do_request()` 中，Responses → Chat 路径必须先转换为 Chat 请求，再按 Chat 能力过滤 Chat 字段，避免在 Responses 原始结构上误判。

- [ ] **步骤 4：禁止语义字段静默丢弃**

任何会改变模型行为的字段，不能被无日志删除。必须：

1. 明确映射；
2. 明确拒绝；
3. 或在配置允许时降级并记录 debug log。

- [ ] **步骤 5：运行能力测试**

运行：

```bash
uv run pytest tests/test_capability_manager.py tests/test_responses_full_flow.py -q
```

预期：能力降级和错误策略稳定。

---

## 任务 11：补齐端到端兼容测试

**文件：**
- 修改：`tests/test_responses_full_flow.py`
- 修改：`tests/mock_server.py`

- [ ] **步骤 1：新增 Chat 上游请求捕获能力**

让 `tests/mock_server.py` 能记录最近一次 `/chat/completions` 请求体，供测试断言转换后的 Chat 请求。

- [ ] **步骤 2：增加单轮文本端到端测试**

客户端请求 Responses：

```json
{"model": "gpt-4o", "input": "Hello"}
```

断言上游收到 Chat：

```json
{"messages": [{"role": "user", "content": "Hello"}]}
```

断言客户端收到 Responses：

```json
{"object": "response", "output_text": "..."}
```

- [ ] **步骤 3：增加多轮 `previous_response_id` 端到端测试**

断言第二轮上游 Chat 请求包含第一轮 user、assistant 和第二轮 user。

- [ ] **步骤 4：增加 function tool 端到端测试**

断言 Responses function tool 被转换为 Chat `tools[].function`，Chat `tool_calls` 被转换回 Responses `output[].function_call`。

- [ ] **步骤 5：增加流式文本端到端测试**

断言客户端收到完整 Responses SSE 生命周期事件，并且最终 `response.completed.response.output` 可用于状态保存。

- [ ] **步骤 6：增加不可转换字段端到端测试**

覆盖托管工具、`background`、`conversation`，预期 HTTP 400。

- [ ] **步骤 7：运行完整 Responses flow 测试**

运行：

```bash
uv run pytest tests/test_responses_full_flow.py -q
```

预期：端到端转换行为和错误行为全部通过。

---

## 任务 12：更新管理文档和用户可见说明

**文件：**
- 修改：`README.md`
- 修改：`docs/architecture.md`
- 修改：`docs/api-spec-anthropic-and-openai/openai-api-spec.md`

- [ ] **步骤 1：更新 README 能力描述**

把「三种 API 格式互转」补充为：

```text
对 Chat Completions 无法表达的 Responses 托管能力，代理会显式拒绝或按配置降级，不做静默丢弃。
```

- [ ] **步骤 2：更新架构文档转换矩阵**

在 `docs/architecture.md` 的 converter 部分增加 Responses → Chat 的支持等级：

| 能力 | 支持等级 |
|------|----------|
| 文本 | 完整 |
| function tools | 完整 |
| 图片输入 | 按上游能力 |
| 文件/音频输入 | 按上游能力 |
| 托管工具 | 不支持，显式错误 |
| 状态历史 | 本地存储展开 |

- [ ] **步骤 3：更新 API 规范中的错误示例**

新增示例：请求 `web_search` 到 Chat 上游时返回 HTTP 400。

- [ ] **步骤 4：运行文档检查**

运行：

```bash
rg -n "Response API|Responses API|Chat Completions|静默|托管工具" README.md docs
```

预期：描述一致，无「完全无损」之类误导性表达。

---

## 任务 13：全量验证

**文件：**
- 无代码文件修改

- [ ] **步骤 1：运行 targeted tests**

运行：

```bash
uv run pytest \
  tests/converters/test_response_to_chat.py \
  tests/converters/test_stream_sequences.py \
  tests/test_proxy_core.py \
  tests/test_proxy_core_responses.py \
  tests/test_responses_full_flow.py \
  -q
```

预期：全部通过。

- [ ] **步骤 2：运行完整测试**

运行：

```bash
uv run pytest
```

预期：全部通过。

- [ ] **步骤 3：运行代码检查**

运行：

```bash
uv run ruff check .
```

预期：无 lint 错误。

- [ ] **步骤 4：人工检查 debug log**

用一个包含降级字段的请求开启 `DEBUG=true`，确认 debug log 中包含：

```text
field dropped
reason
original field name
```

如果策略是拒绝而不是降级，则确认 HTTP 400 错误信息足够明确。

---

## 完成定义

- 所有可转换 Responses 字段都有测试覆盖。
- 所有不可转换 Responses 字段都有 HTTP 400 或显式降级测试覆盖。
- 非流式响应符合 Responses 基本结构，包含 `output`、`output_text`、`usage`。
- 流式响应使用 Responses semantic events，包含正确 event 行、索引字段和完成事件。
- `previous_response_id` 在 Chat 上游路径下可稳定展开历史。
- `uv run pytest` 和 `uv run ruff check .` 通过。
