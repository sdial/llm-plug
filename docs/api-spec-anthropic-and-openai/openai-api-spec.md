# OpenAI API 使用规范

> **来源**：本规范主要整理自 OpenAI 官方 OpenAPI 规范（[openai/openai-openapi](https://github.com/openai/openai-openapi) 仓库 `manual_spec` 分支，版本 2.3.0）以及 [platform.openai.com/docs/api-reference](https://platform.openai.com/docs/api-reference)。
> **更新日期**：2026-04-28

---

## 目录

1. [认证方式](#1-认证方式)
2. [Chat Completions API](#2-chat-completions-api)
3. [Responses API](#3-responses-api)
4. [Models API](#4-models-api)
5. [通用请求头](#5-通用请求头)
6. [错误处理](#6-错误处理)
7. [流式传输（SSE）](#7-流式传输sse)

---

## 1. 认证方式

所有请求均通过 HTTP `Authorization` 头进行 Bearer Token 认证：

```http
Authorization: Bearer <OPENAI_API_KEY>
```

可选组织头：
```http
OpenAI-Organization: org-xxx
```

---

## 2. Chat Completions API

### 2.1 基本信息

| 属性 | 值 |
|------|-----|
| **Endpoint** | `POST https://api.openai.com/v1/chat/completions` |
| **Content-Type** | `application/json` |
| **支持模式** | 非流式（`stream: false`）、流式（`stream: true`） |

### 2.2 请求体（Request Body）

```json
{
  "model": "gpt-4.1",
  "messages": [
    {"role": "developer", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": false,
  "max_tokens": 4096,
  "temperature": 1.0,
  "top_p": 1.0,
  "n": 1,
  "stop": null,
  "presence_penalty": 0,
  "frequency_penalty": 0,
  "logprobs": false,
  "top_logprobs": null,
  "tools": null,
  "tool_choice": "auto",
  "response_format": null,
  "seed": null,
  "user": null,
  "store": false,
  "metadata": {}
}
```

#### 关键字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | **是** | 模型 ID，如 `gpt-4.1`、`gpt-4o-mini` |
| `messages` | array | **是** | 消息列表，按时间顺序排列 |
| `messages[].role` | string | **是** | 角色：`system`/`developer`、`user`、`assistant`、`tool` |
| `messages[].content` | string / array | 是 | 消息内容。可为纯文本或内容部件数组（支持多模态） |
| `stream` | boolean | 否 | 是否启用流式响应，默认 `false` |
| `max_tokens` / `max_completion_tokens` | integer | 否 | 生成 token 上限 |
| `temperature` | number | 否 | 采样温度，0–2，默认 1 |
| `top_p` | number | 否 | 核采样参数，0–1，默认 1 |
| `tools` | array | 否 | 可用工具（函数）列表 |
| `tool_choice` | string / object | 否 | 工具选择策略：`auto`、`none`、`required` 或指定工具 |
| `response_format` | object | 否 | 指定输出格式，如 `{"type": "json_object"}` 或 `{"type": "json_schema", "json_schema": {...}}` |
| `seed` | integer | 否 | 随机种子，用于可复现输出 |
| `logprobs` | boolean | 否 | 是否返回各 token 的对数概率 |
| `store` | boolean | 否 | 是否存储该 completion 供后续检索 |

#### Messages 内容多模态格式

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "What is in this image?"},
    {
      "type": "image_url",
      "image_url": {
        "url": "https://example.com/image.jpg",
        "detail": "auto"
      }
    }
  ]
}
```

#### Tools / Function Calling 格式

```json
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_current_weather",
        "description": "Get the current weather in a given location",
        "parameters": {
          "type": "object",
          "properties": {
            "location": {"type": "string", "description": "The city and state, e.g. San Francisco, CA"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
          },
          "required": ["location"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

当模型决定调用工具时，`assistant` 消息的响应中会包含 `tool_calls`：

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "get_current_weather",
        "arguments": "{\"location\": \"Boston, MA\"}"
      }
    }
  ]
}
```

工具结果需以 `role: "tool"` 的消息回传：

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "{\"temperature\": \"72\", \"unit\": \"fahrenheit\"}"
}
```

### 2.3 响应体（非流式）

```json
{
  "id": "chatcmpl-B9MBs8CjcvOU2jLn4n570S5qMJKcT",
  "object": "chat.completion",
  "created": 1741569952,
  "model": "gpt-4.1-2025-04-14",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I assist you today?",
        "refusal": null,
        "annotations": []
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 19,
    "completion_tokens": 10,
    "total_tokens": 29,
    "prompt_tokens_details": {
      "cached_tokens": 0,
      "audio_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 0,
      "audio_tokens": 0,
      "accepted_prediction_tokens": 0,
      "rejected_prediction_tokens": 0
    }
  },
  "service_tier": "default",
  "system_fingerprint": "fp_44709d6fcb"
}
```

#### 关键字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 唯一标识符，前缀 `chatcmpl-` |
| `object` | string | 固定为 `chat.completion` |
| `choices` | array | 生成结果列表（默认 1 条） |
| `choices[].message.role` | string | 固定为 `assistant` |
| `choices[].message.content` | string / null | 生成的文本内容 |
| `choices[].message.tool_calls` | array / null | 工具调用列表 |
| `choices[].finish_reason` | string | 停止原因：`stop`、`length`、`tool_calls`、`content_filter` 等 |
| `usage.prompt_tokens` | integer | 输入 token 数 |
| `usage.completion_tokens` | integer | 输出 token 数 |
| `usage.total_tokens` | integer | 总 token 数 |
| `system_fingerprint` | string | 系统指纹，标识模型运行配置 |

### 2.4 流式响应（SSE）

当 `stream: true` 时，服务端返回 `text/event-stream`，每条数据以 `data:` 开头：

```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-4o-mini","system_fingerprint":"fp_44709d6fcb","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-4o-mini","system_fingerprint":"fp_44709d6fcb","choices":[{"index":0,"delta":{"content":"Hello"},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-4o-mini","system_fingerprint":"fp_44709d6fcb","choices":[{"index":0,"delta":{},"logprobs":null,"finish_reason":"stop"}]}

data: [DONE]
```

#### 流式 Chunk 结构

| 字段 | 说明 |
|------|------|
| `id` | 与整个流相同的 completion ID |
| `object` | 固定为 `chat.completion.chunk` |
| `choices[].delta` | 增量内容。`role` 仅在首条出现，`content` 逐字/逐段出现 |
| `choices[].finish_reason` | 最后一条 chunk 中标记停止原因，此前为 `null` |
| `[DONE]` | 流结束标记 |

---

## 3. Responses API

> **注意**：Responses API 是 OpenAI 推出的新一代对话接口，旨在统一文本、工具调用、多模态和状态管理。

### 3.1 基本信息

| 属性 | 值 |
|------|-----|
| **Endpoint** | `POST https://api.openai.com/v1/responses` |
| **Content-Type** | `application/json` |
| **支持模式** | 非流式、流式 |

### 3.2 请求体

```json
{
  "model": "gpt-4.1",
  "input": [
    {"role": "user", "content": "What is the capital of France?"}
  ],
  "instructions": "You are a helpful assistant.",
  "tools": [
    {
      "type": "function",
      "name": "get_weather",
      "description": "Get weather for a location",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {"type": "string"}
        },
        "required": ["location"]
      }
    }
  ],
  "tool_choice": "auto",
  "stream": false,
  "temperature": 1.0,
  "top_p": 1.0,
  "max_output_tokens": 4096,
  "previous_response_id": null,
  "store": true,
  "user": null,
  "metadata": {}
}
```

#### 关键字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | **是** | 模型 ID |
| `input` | array / string | **是** | 输入内容。可为字符串（单条用户消息）或消息对象数组 |
| `input[].role` | string | 是 | `user`、`assistant`、`system`/`developer`、`tool` |
| `input[].content` | string / array | 是 | 消息内容，支持多模态部件 |
| `input[].type` | string | 条件 | 用于特殊输入项：`function_call`、`function_call_output`、`file` 等 |
| `instructions` | string | 否 | 系统级指令（等效于 `system` 消息） |
| `tools` | array | 否 | 工具定义列表。格式与 Chat Completions 略有不同：顶层直接包含 `name`、`description`、`parameters` |
| `tool_choice` | string / object | 否 | `auto`、`none`、`required` 或指定 `{type: "function", name: "..."}` |
| `stream` | boolean | 否 | 是否流式输出，默认 `false` |
| `max_output_tokens` | integer | 否 | 最大输出 token 数 |
| `previous_response_id` | string | 否 | 关联的上一次 response ID，用于多轮对话状态保持 |
| `store` | boolean | 否 | 是否存储该 response |

#### 函数调用输出（Function Call Output）

当需要回传工具结果时，`input` 中应包含：

```json
{
  "type": "function_call_output",
  "call_id": "call_abc123",
  "output": "{\"temperature\": 72, \"unit\": \"fahrenheit\"}"
}
```

### 3.3 响应体（非流式）

```json
{
  "id": "resp_abc123",
  "object": "response",
  "created_at": 1741569952,
  "model": "gpt-4.1-2025-04-14",
  "status": "completed",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {"type": "output_text", "text": "The capital of France is Paris."}
      ]
    }
  ],
  "usage": {
    "input_tokens": 15,
    "output_tokens": 10,
    "total_tokens": 25,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 0}
  },
  "error": null,
  "incomplete_details": null,
  "instructions": "You are a helpful assistant.",
  "max_output_tokens": null,
  "metadata": {},
  "parallel_tool_calls": true,
  "previous_response_id": null,
  "tool_choice": "auto",
  "tools": [],
  "user": null
}
```

#### 关键字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 唯一标识符，前缀 `resp_` |
| `object` | string | 固定为 `response` |
| `status` | string | 请求状态：`in_progress`、`completed`、`incomplete`、`cancelled`、`failed` |
| `output` | array | 输出项数组，每个项有 `type`（`message`、`function_call`、`web_search_call` 等） |
| `output[].content` | array | 内容部件数组，文本类型为 `{"type": "output_text", "text": "..."}` |
| `usage` | object | Token 用量统计 |
| `error` | object / null | 若处理出错，返回错误详情 |

### 3.4 流式响应（SSE）

流式返回与 Chat Completions 类似，但事件格式更丰富，可能包含以下事件类型：

```
event: response.created
data: {"type":"response.created","response":{"id":"resp_123",...}}

event: response.in_progress
data: {"type":"response.in_progress","response":{"id":"resp_123",...}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","item_id":"msg_123","output_index":0,"content_index":0,"delta":"Paris"}

event: response.output_item.done
data: {"type":"response.output_item.done","item_id":"msg_123","output_index":0,"item":{...}}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_123","status":"completed",...}}
```

#### 常见事件类型

| 事件 | 说明 |
|------|------|
| `response.created` | Response 创建完成 |
| `response.in_progress` | 开始生成 |
| `response.output_text.delta` | 文本增量 |
| `response.output_item.added` | 新增输出项（如函数调用） |
| `response.output_item.done` | 某输出项完成 |
| `response.completed` | 整个 Response 完成 |
| `response.incomplete` | 因长度或其他原因未完成 |
| `response.failed` | 处理失败 |

---

## 4. Models API

### 4.1 获取模型列表

```http
GET https://api.openai.com/v1/models
Authorization: Bearer <token>
```

响应：

```json
{
  "object": "list",
  "data": [
    {
      "id": "gpt-4.1",
      "object": "model",
      "created": 1698894913,
      "owned_by": "openai"
    }
  ]
}
```

---

## 5. 通用请求头

| 头字段 | 说明 |
|--------|------|
| `Authorization` | `Bearer <api_key>`，必填 |
| `Content-Type` | `application/json`，POST 请求必填 |
| `OpenAI-Beta` | 用于 Beta 功能，如 `assistants=v2` |
| `OpenAI-Organization` | 指定组织 |

---

## 6. 错误处理

错误响应统一格式：

```json
{
  "error": {
    "message": "...",
    "type": "...",
    "param": "...",
    "code": "..."
  }
}
```

#### 常见 HTTP 状态码

| 状态码 | 含义 |
|--------|------|
| 400 | Bad Request — 请求参数错误 |
| 401 | Unauthorized — API Key 无效或缺失 |
| 403 | Forbidden — 权限不足 |
| 404 | Not Found — 模型或资源不存在 |
| 429 | Too Many Requests — 速率限制 |
| 500 | Internal Server Error — 服务端错误 |
| 503 | Service Unavailable — 服务暂时不可用 |

#### 常见错误码

| 错误码 | 说明 |
|--------|------|
| `invalid_request_error` | 请求格式或参数有误 |
| `authentication_error` | 认证失败 |
| `rate_limit_error` | 超出速率限制 |
| `insufficient_quota` | 额度不足 |
| `context_length_exceeded` | 上下文长度超限 |
| `server_error` | 服务端内部错误 |

---

## 7. 流式传输（SSE）

### 7.1 Chat Completions SSE 格式

- Content-Type: `text/event-stream`
- 每行以 `data: ` 开头
- 流结束标志：`data: [DONE]`
- **无 `event:` 字段**，仅 `data:` 行

### 7.2 Responses SSE 格式

- Content-Type: `text/event-stream`
- 包含 `event:` 和 `data:` 行
- 事件类型见 3.4 节

### 7.3 解析示例（Python）

```python
import httpx

with httpx.stream("POST", "https://api.openai.com/v1/chat/completions",
    headers={"Authorization": "Bearer <token>", "Content-Type": "application/json"},
    json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}], "stream": True}
) as response:
    for line in response.iter_lines():
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk["choices"][0]["delta"]
            if delta.get("content"):
                print(delta["content"], end="")
```

---

## 参考链接

- [OpenAI API Reference](https://platform.openai.com/docs/api-reference)
- [OpenAI OpenAPI Spec (GitHub)](https://github.com/openai/openai-openapi)
- [Chat Completions Guide](https://platform.openai.com/docs/guides/text-generation)
- [Responses API Guide](https://platform.openai.com/docs/guides/responses)
- [Vision Guide](https://platform.openai.com/docs/guides/vision)
- [Function Calling Guide](https://platform.openai.com/docs/guides/function-calling)
