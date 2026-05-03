# spec-converters — API 格式转换器

> 对应目录：`converters/`（4 个文件，约 1700 行）

## 模块定位

转换器负责三种 LLM API 格式之间的请求/响应转换。这是本项目最核心、最复杂的模块——理解了转换器，就理解了这个项目为什么存在。

## 三种 API 格式对比

在理解转换器之前，你需要先了解三种 API 格式的核心差异：

### OpenAI Chat Completions

```json
// 请求
{
  "model": "gpt-4",
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi!", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"}
  ],
  "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
  "stream": false
}

// 响应
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi!"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
}
```

### OpenAI Response

```json
// 请求
{
  "model": "gpt-4",
  "instructions": "You are helpful.",
  "input": [
    {"role": "user", "content": "Hello"},
    {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "call_1", "output": "Sunny"}
  ],
  "tools": [{"type": "function", "name": "get_weather", "parameters": {}}],
  "stream": false
}

// 响应
{
  "id": "resp_xxx",
  "object": "response",
  "status": "completed",
  "output": [
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi!"}]},
    {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}"}
  ],
  "usage": {"input_tokens": 10, "output_tokens": 5}
}
```

### Anthropic Messages

```json
// 请求
{
  "model": "claude-3",
  "system": "You are helpful.",
  "messages": [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}, {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "Sunny"}]}
  ],
  "tools": [{"name": "get_weather", "input_schema": {}}],
  "stream": false,
  "max_tokens": 4096
}

// 响应
{
  "id": "msg_xxx",
  "type": "message",
  "role": "assistant",
  "content": [{"type": "text", "text": "Hi!"}],
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 10, "output_tokens": 5}
}
```

## BaseConverter 抽象基类

```python
class BaseConverter(ABC):
    @abstractmethod
    def convert_request(self, source_data: dict, source_type: str = "") -> dict:
        """将入口请求体转为上游 API 所需 JSON。"""

    @abstractmethod
    def convert_response(self, target_response: dict, source_type: str = "") -> dict:
        """将上游非流式 JSON 转为入口 API 对应格式。"""

    @abstractmethod
    def convert_stream_chunk(self, chunk: dict, source_type: str = "") -> dict | None:
        """将上游 SSE 解析出的单条 JSON 转为入口格式；返回 None 表示跳过该块。"""

    def get_stream_event_type(self, chunk: dict, source_type: str = "") -> str | None:
        """获取流式事件的 event type（仅 Anthropic 输出格式需要）。"""

    def get_extra_events(self, chunk: dict) -> list:
        """获取流式转换产生的额外事件。"""
```

**关键设计**：

- `source_type` 参数：因为每个 Converter 要处理两种不同来源的格式（如 `ToChatCompletionsConverter` 要处理来自 `anthropic` 和 `openai-response` 的数据），所以需要 `source_type` 来区分。
- `_stream_state`：流式转换时维护的状态机（消息 ID、tool call index 等）。每个实例只服务一次流式请求，**不可复用**。
- `_extra_events`：一个上游 chunk 可能产生多个下游事件，通过此字段传递。

## ToChatCompletionsConverter

**目标格式**：OpenAI Chat Completions

### 支持的转换路径

| 方法 | 源格式 | 说明 |
|------|--------|------|
| `_anthropic_request_to_chat` | anthropic | system → system message；tool_use → tool_calls；tool_result → tool message |
| `_anthropic_response_to_chat` | anthropic | content blocks → message + tool_calls；thinking → reasoning_content |
| `_anthropic_stream_chunk_to_chat` | anthropic | message_start/content_block_start/delta/stop → chat.completion.chunk |
| `_response_request_to_chat` | openai-response | instructions → system message；input items → messages；function_call → tool_calls |
| `_response_response_to_chat` | openai-response | output items → choices message + tool_calls |
| `_response_stream_chunk_to_chat` | openai-response | response.created/output_text.delta/function_call → chat.completion.chunk |

### 关键字段映射

**Anthropic → Chat**：
| Anthropic | Chat Completions |
|-----------|------------------|
| `system` | `messages[role=system]` |
| `content[].type=text` | `message.content` |
| `content[].type=thinking` | `message.reasoning_content` |
| `content[].type=tool_use` | `message.tool_calls[]` |
| `stop_reason: end_turn` | `finish_reason: stop` |
| `stop_reason: max_tokens` | `finish_reason: length` |
| `stop_reason: tool_use` | `finish_reason: tool_calls` |
| `usage.input_tokens` | `usage.prompt_tokens` |
| `usage.output_tokens` | `usage.completion_tokens` |

**Response → Chat**：
| OpenAI Response | Chat Completions |
|-----------------|------------------|
| `instructions` | `messages[role=system]` |
| `input[type=function_call]` | `messages[role=assistant, tool_calls]` |
| `input[type=function_call_output]` | `messages[role=tool]` |
| `output[type=message]` | `choices[0].message.content` |
| `output[type=function_call]` | `choices[0].message.tool_calls` |
| `status: incomplete` | `finish_reason: length` |

### 流式状态机

```
_stream_state = {
    "msg_id": "chatcmpl-xxx",        # 消息 ID
    "model": "",                     # 模型名
    "tool_call_index": 0,            # 下一个 tool_call 的 index
    "content_block_to_tc_index": {}, # Anthropic content block index → tool_call index
    "output_index_to_tc_index": {},  # Response output_index → tool_call index
}
```

## ToResponseConverter

**目标格式**：OpenAI Response

### 支持的转换路径

| 方法 | 源格式 | 说明 |
|------|--------|------|
| `_chat_request_to_response` | openai-chat-completions | messages → input items；system → instructions |
| `_chat_response_to_response` | openai-chat-completions | choices → output items；tool_calls → function_call items |
| `_chat_stream_chunk_to_response` | openai-chat-completions | delta → response.created/output_text.delta/function_call |
| `_anthropic_request_to_response` | anthropic | system → instructions；tool_use → function_call；tool_result → function_call_output |
| `_anthropic_response_to_response` | anthropic | content blocks → output items |
| `_anthropic_stream_chunk_to_response` | anthropic | Anthropic events → Response events |

### 特殊处理

- **reasoning_content / thinking_delta → reasoning item**：流式中首次遇到 reasoning 内容时，先发 `response.output_item.added`（type=reasoning），然后发 `response.reasoning_summary_text.delta`。通过 `_stream_state["reasoning_started"]` 标记是否已创建 reasoning item。
- **多事件合并**：一个 OpenAI Chat chunk 中的 tool_calls 可能包含多个，会通过 `_extra_events` 传递。

### 流式状态机

```
_stream_state = {
    "reasoning_started": False, # 是否已创建 reasoning item
    "reasoning_id": "", # reasoning item 的 ID
    "message_id": "", # 消息 ID
    "output_index": 0, # 当前 output item 的索引
}
```

## ToAnthropicConverter

**目标格式**：Anthropic Messages

### 支持的转换路径

| 方法 | 源格式 | 说明 |
|------|--------|------|
| `_chat_request_to_anthropic` | openai-chat-completions | system → system 字段；tool_calls → tool_use content；tool → tool_result content |
| `_chat_response_to_anthropic` | openai-chat-completions | choices → content blocks；stop → end_turn |
| `_chat_stream_chunk_to_anthropic` | openai-chat-completions | OpenAI delta → Anthropic events（注意：返回 list） |
| `_response_request_to_anthropic` | openai-response | instructions → system；function_call → tool_use；function_call_output → tool_result |
| `_response_response_to_anthropic` | openai-response | output items → content blocks |
| `_response_stream_chunk_to_anthropic` | openai-response | Response events → Anthropic events（返回 list） |

### 关键字段映射

**Chat → Anthropic**：
| Chat Completions | Anthropic |
|------------------|-----------|
| `messages[role=system]` | `system` 字段 |
| `message.tool_calls[]` | `content[type=tool_use]` |
| `messages[role=tool]` | `content[type=tool_result]`（放在 user 消息中） |
| `tools[].function` | `tools[]`（name + input_schema） |
| `finish_reason: stop` | `stop_reason: end_turn` |
| `finish_reason: length` | `stop_reason: max_tokens` |
| `finish_reason: tool_calls` | `stop_reason: tool_use` |
| `reasoning_effort` / `enable_thinking` | `thinking: {type: "enabled", budget_tokens: N}` |

### 特殊处理

1. **Tool 消息合并**：Anthropic 要求 `tool_result` 放在 `user` 角色消息中，且连续的 tool 消息需合并到同一个 user 消息内。`_chat_request_to_anthropic()` 会自动合并。
2. **流式转换返回列表**：`_chat_stream_chunk_to_anthropic()` 和 `_response_stream_chunk_to_anthropic()` 返回 `list[tuple[str, dict]]`（event_type, data），因为一个上游 chunk 可能产生多个 Anthropic 事件。`convert_stream_chunk()` 会将第一个事件作为主返回值，后续事件放入 `_extra_events`。
3. **Thinking/Reasoning**：支持将 OpenAI 的 `reasoning_content` / `reasoning_effort` 和 `enable_thinking` 字段转换为 Anthropic 的 `thinking` 字段。

### 流式状态机

```
_stream_state = {
    "started": False, # 是否已发 message_start
    "content_block_started": False, # 是否有正在进行的 content block
    "content_block_index": 0, # 当前 content block index
    "current_content_type": None, # 当前内容类型（text/tool_use/thinking）
    "tool_id": None, # 当前 tool call 的 ID
    "tool_name": None, # 当前 tool call 的名称
    "tool_call_indices": {}, # OpenAI tool_call index → content_block_index 映射
    "_prev_completion_tokens": 0, # 上一次累计的 output tokens，用于计算增量
}
```

**`message_start` 生成时机**：当前实现在 `_ensure_message_started()` 中，当收到第一个包含 `role: "assistant"` 的 chunk 时即发射 `message_start`，确保上游首个 chunk 携带的 `usage.prompt_tokens` 被正确捕获到 `message_start` 事件的 `usage.input_tokens` 字段中。此前的实现在首个实际内容到达时才发射 `message_start`，导致 prompt tokens 信息丢失。
```

## 流式 SSE 格式差异

| 特性 | OpenAI 格式 | Anthropic 格式 |
|------|-------------|----------------|
| 行结构 | 仅 `data: {...}\n\n` | `event: xxx\ndata: {...}\n\n` |
| 结束标记 | `data: [DONE]\n\n` | `event: message_stop` |
| 心跳 | 无 | `event: ping` |
| 内容增量 | `delta.content` / `delta.tool_calls` | `content_block_delta` (text_delta / input_json_delta / thinking_delta) |

## 工具（Tool）格式对比

| 特性 | OpenAI Chat | OpenAI Response | Anthropic |
|------|-------------|-----------------|-----------|
| 定义方式 | `tools[].function.{name, parameters}` | `tools[].{name, parameters}` | `tools[].{name, input_schema}` |
| 调用方式 | `message.tool_calls[].{id, function.name, function.arguments}` | `output[type=function_call].{call_id, name, arguments}` | `content[type=tool_use].{id, name, input}` |
| 结果方式 | `messages[role=tool].{tool_call_id, content}` | `input[type=function_call_output].{call_id, output}` | `content[type=tool_result].{tool_use_id, content}` |

## 图片格式对比

| 特性 | OpenAI Chat | Anthropic |
|------|-------------|-----------|
| 传入方式 | `content[type=image_url, image_url.url=data:image/...;base64,...]` | `content[type=image, source={type:base64, media_type, data}]` |

转换时双向支持 base64 图片的互转。
