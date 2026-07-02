# Anthropic Messages API 使用规范

> **来源**：本规范主要整理自 Anthropic 官方 Python SDK 类型定义（[anthropics/anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python) 仓库主分支 `api.md`）以及 Anthropic TypeScript SDK（[anthropics/anthropic-sdk-typescript](https://github.com/anthropics/anthropic-sdk-typescript)）。官方完整文档见 [docs.anthropic.com/en/api/messages](https://docs.anthropic.com/en/api/messages)。
> **更新日期**：2026-04-28

---

## 目录

1. [认证方式](#1-认证方式)
2. [Messages API](#2-messages-api)
3. [请求体详解](#3-请求体详解)
4. [响应体详解](#4-响应体详解)
5. [流式传输（SSE）](#5-流式传输sse)
6. [工具使用（Tool Use）](#6-工具使用tool-use)
7. [多模态输入](#7-多模态输入)
8. [Models API](#8-models-api)
9. [错误处理](#9-错误处理)
10. [Token 用量与计费](#10-token-用量与计费)

---

## 1. 认证方式

Anthropic API 使用 `x-api-key` 请求头进行认证：

```http
x-api-key: <ANTHROPIC_API_KEY>
```

同时必须携带 API 版本头：

```http
anthropic-version: 2023-06-01
```

可选 Beta 功能头：

```http
anthropic-beta: prompt-caching-2024-07-31
```

---

## 2. Messages API

### 2.1 基本信息

| 属性 | 值 |
|------|-----|
| **Endpoint** | `POST https://api.anthropic.com/v1/messages` |
| **Content-Type** | `application/json` |
| **支持模式** | 非流式、流式（`stream: true`） |

### 2.2 基本请求示例

```bash
curl -X POST https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-7",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello, Claude"}
    ]
  }'
```

### 2.3 基本响应示例

```json
{
  "id": "msg_01XgYHxk3j9g1Y8uJ8v7g8qQ",
  "type": "message",
  "role": "assistant",
  "model": "claude-opus-4-7",
  "content": [
    {"type": "text", "text": "Hello! How can I help you today?"}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 12,
    "output_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

---

## 3. 请求体详解

### 3.1 完整请求体结构

```json
{
  "model": "claude-opus-4-7",
  "max_tokens": 4096,
  "messages": [
    {"role": "user", "content": "Hello!"}
  ],
  "system": "You are a helpful assistant.",
  "stream": false,
  "temperature": 1.0,
  "top_p": 0.999,
  "top_k": 0,
  "stop_sequences": [],
  "tools": [],
  "tool_choice": {"type": "auto"},
  "thinking": null,
  "metadata": {},
  "user_id": null
}
```

#### 关键字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | **是** | 模型 ID，如 `claude-opus-4-7`、`claude-sonnet-4-6`、`claude-haiku-4-5` |
| `max_tokens` | integer | **是** | 最大生成 token 数（硬上限） |
| `messages` | array | **是** | 消息列表，每条含 `role` 和 `content` |
| `messages[].role` | string | **是** | `user` 或 `assistant`。Anthropic **没有 `system` 角色**，系统提示通过顶层 `system` 字段传入 |
| `messages[].content` | string / array | **是** | 消息内容，支持字符串或内容块数组 |
| `system` | string / array | 否 | 系统提示。可为纯字符串或 `{"type": "text", "text": "...", "cache_control": {...}}` 数组 |
| `stream` | boolean | 否 | 是否启用流式响应，默认 `false` |
| `temperature` | number | 否 | 采样温度，默认 1.0，范围 0.0–1.0 |
| `top_p` | number | 否 | 核采样参数，默认 0.999 |
| `top_k` | integer | 否 | Top-k 采样，默认 0（禁用） |
| `stop_sequences` | array of string | 否 | 自定义停止序列，最多 4 个 |
| `tools` | array | 否 | 可用工具定义列表 |
| `tool_choice` | object | 否 | 工具选择策略：`{"type": "auto"}`、`{"type": "any"}`、`{"type": "none"}` 或强制指定 `{"type": "tool", "name": "..."}` |
| `thinking` | object | 否 | 思考模式配置（部分模型支持），如 `{"type": "enabled", "budget_tokens": 16000}` |
| `metadata` | object | 否 | 元数据键值对 |
| `user_id` | string | 否 | 终端用户标识，用于滥用检测 |

### 3.2 内容块（Content Block）格式

Anthropic `content` 字段支持灵活的内容块数组：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "What do you see?"},
    {
      "type": "image",
      "source": {
        "type": "base64",
        "media_type": "image/png",
        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
      }
    }
  ]
}
```

#### 内容块类型

| 类型 | 说明 |
|------|------|
| `text` | 纯文本内容块 |
| `image` | 图片内容块，需通过 `source` 提供 base64 或 URL（部分部署支持） |
| `tool_use` | 模型发出的工具调用请求（出现在 assistant 消息中） |
| `tool_result` | 工具执行结果（回传给模型的 user 类消息） |
| `document` | 文档内容块（PDF 等），支持 `source` 为 base64 或 URL |

#### Image Source 格式

```json
{
  "type": "image",
  "source": {
    "type": "base64",
    "media_type": "image/png",
    "data": "<base64-encoded-image-data>"
  }
}
```

支持的 `media_type`：`image/jpeg`、`image/png`、`image/gif`、`image/webp`。

---

## 4. 响应体详解

### 4.1 非流式响应结构

```json
{
  "id": "msg_01XgYHxk3j9g1Y8uJ8v7g8qQ",
  "type": "message",
  "role": "assistant",
  "model": "claude-opus-4-7",
  "content": [
    {"type": "text", "text": "Hello! How can I help you today?"}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 12,
    "output_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

#### 关键字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 唯一标识符，前缀 `msg_` |
| `type` | string | 固定为 `message` |
| `role` | string | 固定为 `assistant` |
| `model` | string | 实际使用的模型 |
| `content` | array | 内容块数组，可能包含 `text`、`tool_use` 等类型 |
| `stop_reason` | string / null | 停止原因：`end_turn`、`max_tokens`、`stop_sequence`、`tool_use` |
| `stop_sequence` | string / null | 若因 `stop_sequence` 停止，显示匹配的序列 |
| `usage.input_tokens` | integer | 输入 token 数（含 system 和 messages） |
| `usage.output_tokens` | integer | 输出 token 数 |
| `usage.cache_creation_input_tokens` | integer | Prompt Caching 创建的缓存 token 数 |
| `usage.cache_read_input_tokens` | integer | 从缓存读取的 token 数 |

### 4.2 内容块响应类型

当模型返回文本时：

```json
{"type": "text", "text": "The capital of France is Paris."}
```

当模型决定使用工具时，`content` 中会包含 `tool_use` 块：

```json
{
  "type": "tool_use",
  "id": "toolu_01T1xCFM9qA4yR7x8X2vJk8L",
  "name": "get_current_weather",
  "input": {"location": "Paris, France", "unit": "celsius"}
}
```

| 字段 | 说明 |
|------|------|
| `id` | 工具调用的唯一 ID，回传结果时需要使用 |
| `name` | 调用的工具名称 |
| `input` | 模型生成的工具参数（JSON 对象） |

---

## 5. 流式传输（SSE）

当 `stream: true` 时，Anthropic 返回 `text/event-stream`，**每行包含 `event:` 和 `data:`**：

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_123","type":"message","role":"assistant","content":[],"model":"claude-opus-4-7","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: ping
data: {"type": "ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"!"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":10}}

event: message_stop
data: {"type":"message_stop"}
```

### 5.1 事件类型总览

| 事件 | 说明 |
|------|------|
| `message_start` | 消息开始，包含完整的消息元数据 |
| `content_block_start` | 新内容块开始 |
| `content_block_delta` | 内容块增量更新（文本、JSON、thinking 等） |
| `content_block_stop` | 内容块结束 |
| `message_delta` | 消息级增量（如 `stop_reason` 更新、用量增量） |
| `message_stop` | 整个消息流结束 |
| `ping` | 心跳包 |

### 5.2 Delta 类型

| Delta 类型 | 说明 |
|------------|------|
| `text_delta` | 文本增量，`{"type": "text_delta", "text": "..."}` |
| `input_json_delta` | 工具参数 JSON 增量，`{"type": "input_json_delta", "partial_json": "..."}` |
| `thinking_delta` | 思考过程增量（扩展思考模式） |
| `signature_delta` | 思考签名增量 |
| `citations_delta` | 引用增量 |

---

## 6. 工具使用（Tool Use）

### 6.1 工具定义格式

```json
{
  "tools": [
    {
      "name": "get_current_weather",
      "description": "Get the current weather in a given location",
      "input_schema": {
        "type": "object",
        "properties": {
          "location": {
            "type": "string",
            "description": "The city and state, e.g. San Francisco, CA"
          },
          "unit": {
            "type": "string",
            "enum": ["celsius", "fahrenheit"]
          }
        },
        "required": ["location"]
      }
    }
  ],
  "tool_choice": {"type": "auto"}
}
```

> **注意**：Anthropic 的工具定义使用 `input_schema`（而非 OpenAI 的 `parameters`），且顶层没有 `type: "function"` 包装。

### 6.2 工具结果回传

将工具执行结果以 `tool_result` 内容块形式回传：

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_01T1xCFM9qA4yR7x8X2vJk8L",
      "content": "The weather in Paris is 18°C and sunny."
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `tool_use_id` | 对应 `tool_use` 块中的 `id` |
| `content` | 工具执行结果，可为字符串或内容块数组 |
| `is_error` | boolean，可选，标记该结果是否为错误 |

---

## 7. 多模态输入

Anthropic Messages API 支持在同一轮对话中混合文本和图片：

```json
{
  "model": "claude-opus-4-7",
  "max_tokens": 1024,
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "What is in this image?"},
        {
          "type": "image",
          "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "<base64-data>"
          }
        }
      ]
    }
  ]
}
```

同时也支持 PDF 文档：

```json
{
  "type": "document",
  "source": {
    "type": "base64",
    "media_type": "application/pdf",
    "data": "<base64-pdf-data>"
  }
}
```

---

## 8. Models API

### 8.1 获取模型列表

```http
GET https://api.anthropic.com/v1/models
x-api-key: <token>
anthropic-version: 2023-06-01
```

响应示例：

```json
{
  "data": [
    {
      "type": "model",
      "id": "claude-opus-4-7",
      "display_name": "Claude Opus 4.7",
      "created_at": "2026-03-01T00:00:00Z"
    },
    {
      "type": "model",
      "id": "claude-sonnet-4-6",
      "display_name": "Claude Sonnet 4.6",
      "created_at": "2026-02-01T00:00:00Z"
    }
  ],
  "has_more": false,
  "first_id": "claude-opus-4-7",
  "last_id": "claude-sonnet-4-6"
}
```

---

## 9. 错误处理

错误响应统一格式：

```json
{
  "type": "error",
  "error": {
    "type": "invalid_request_error",
    "message": "..."
  }
}
```

#### 常见错误类型

| 错误类型 | 说明 |
|----------|------|
| `invalid_request_error` | 请求格式或参数有误 |
| `authentication_error` | API Key 无效或缺失 |
| `permission_error` | 权限不足 |
| `not_found_error` | 请求的资源不存在 |
| `rate_limit_error` | 超出速率限制 |
| `overloaded_error` | 服务过载 |
| `api_error` | 内部 API 错误 |
| `gateway_timeout_error` | 网关超时 |

#### HTTP 状态码映射

| 状态码 | 含义 |
|--------|------|
| 400 | 请求参数错误 |
| 401 | 认证失败 |
| 403 | 禁止访问 |
| 404 | 资源不存在 |
| 429 | 速率限制 |
| 500 | 内部服务器错误 |
| 529 | 服务过载（overloaded） |

---

## 10. Token 用量与计费

### 10.1 用量字段

| 字段 | 说明 |
|------|------|
| `input_tokens` | 输入文本（含 system prompt、messages、tools 定义）的 token 数 |
| `output_tokens` | 模型生成内容的 token 数 |
| `cache_creation_input_tokens` | Prompt Caching 写入缓存的 token 数（计费不同） |
| `cache_read_input_tokens` | 命中缓存的 token 数（大幅降价） |

### 10.2 Prompt Caching

通过为内容块添加 `cache_control` 启用：

```json
{
  "type": "text",
  "text": "<very long system prompt...>",
  "cache_control": {"type": "ephemeral"}
}
```

- 首次请求：产生 `cache_creation_input_tokens`
- 后续请求（在 TTL 内）：产生 `cache_read_input_tokens`，费用大幅降低

---

## 参考链接

- [Anthropic Messages API Reference](https://docs.anthropic.com/en/api/messages)
- [Anthropic Python SDK (GitHub)](https://github.com/anthropics/anthropic-sdk-python)
- [Anthropic TypeScript SDK (GitHub)](https://github.com/anthropics/anthropic-sdk-typescript)
- [Prompt Caching Guide](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
- [Tool Use Guide](https://docs.anthropic.com/en/docs/build-with-claude/tool-use)
- [Vision Guide](https://docs.anthropic.com/en/docs/build-with-claude/vision)
