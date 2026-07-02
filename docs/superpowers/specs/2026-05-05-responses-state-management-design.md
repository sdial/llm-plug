# Responses API 完整兼容设计

> 日期：2026-05-05
> 目标：100% 兼容 Codex CLI 的 Responses API 请求
> 参考：[codex-responses-adapter](https://github.com/SSebo/codex-responses-adapter/blob/main/docs/specs/2026-03-11-responses-adapter-design.md)

---

## 1. 背景

Codex CLI 使用 OpenAI Responses API 格式（`wire_api = "responses"`），核心特性：

1. **有状态对话**：通过 `previous_response_id` 引用历史，服务端自动加载上下文
2. **服务器端工具执行**：内置工具（file_search、web_search 等）在服务器端执行
3. **简化客户端**：客户端只需发送增量消息，无需维护完整历史
4. **特殊角色**：使用 `developer` 角色替代 `system`

当前 llm-plug 需要完整支持 Responses API 协议，确保与 Codex CLI 100% 兼容。

---

## 2. 核心类型定义

### 2.1 上游类型（Codex → Adapter）

```python
# Responses API 请求
class ResponsesRequest:
    model: str                          # 模型名称
    instructions: str                   # 系统指令
    input: list[ResponseItem]           # 输入消息/推理项
    tools: list[dict]                   # 工具定义
    tool_choice: str | dict             # 工具选择策略
    parallel_tool_calls: bool           # 是否允许并行调用
    reasoning: dict | None              # 推理配置
    stream: bool                        # 是否流式
    previous_response_id: str | None    # 前一个响应 ID
    store: bool                         # 是否存储

# 输入项类型
class ResponseItem:
    # 消息项
    Message = {"role": "user"|"assistant"|"developer"|"system", "content": "..."}
    # 推理项
    Reasoning = {"type": "reasoning", "id": "...", "summary": [...]}
    # 函数调用请求
    FunctionCall = {"type": "function_call", "call_id": "...", "name": "...", "arguments": "..."}
    # 函数调用结果
    FunctionCallOutput = {"type": "function_call_output", "call_id": "...", "output": "..."}
    # 托管工具（不支持）
    WebSearchCall = {"type": "web_search_call", ...}
    FileSearchCall = {"type": "file_search_call", ...}
    ComputerCall = {"type": "computer_call", ...}
```

### 2.2 下游类型（Adapter → 上游 Chat API）

```python
# Chat API 请求
class ChatRequest:
    model: str
    messages: list[ChatMessage]
    tools: list[dict] | None
    tool_choice: str | dict | None
    stream: bool
    # 上游特定参数

# Chat 消息
class ChatMessage:
    role: str                           # system / user / assistant / tool
    content: str
    name: str | None                    # 用于 tool 消息
    tool_calls: list[dict] | None       # assistant 的工具调用
    tool_call_id: str | None            # tool 消息的调用 ID
```

### 2.3 返回类型（Adapter → Codex）

```python
# Responses API 响应
class ResponsesResponse:
    id: str                             # "resp_" + 24字符hex
    object: str = "response"
    created: int                        # Unix 时间戳
    model: str
    output: list[ResponseItemOutput]
    usage: dict | None
    status: str = "completed"

# 输出项类型
class ResponseItemOutput:
    Message = {
        "type": "message",
        "id": "msg_xxx",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "..."}]
    }
    FunctionCall = {
        "type": "function_call",
        "id": "fc_xxx",
        "call_id": "call_xxx",
        "name": "function_name",
        "arguments": "{...}"
    }
```

---

## 3. 转换规则矩阵

### 3.1 请求转换

| Responses 字段 | Chat 字段 | 转换规则 |
|----------------|-----------|----------|
| `instructions` | `messages[0]` (system) | 转换为第一条 system 消息 |
| `input[].Message` | `messages[]` | 转换内容并规范化角色（见 3.2） |
| `input[].Reasoning` | ❌ dropped | 记录日志警告并忽略 |
| `input[].FunctionCall` | `assistant.tool_calls` | 转换为 Chat 工具调用格式 |
| `input[].FunctionCallOutput` | `tool` message | 转换为工具响应消息 |
| `input[].WebSearchCall` | ❌ error | 托管工具不支持，返回 400 |
| `input[].FileSearchCall` | ❌ error | 托管工具不支持，返回 400 |
| `input[].ComputerCall` | ❌ error | 托管工具不支持，返回 400 |
| `tools` (function) | `tools` | 转换格式（见 3.3） |
| `tool_choice` | `tool_choice` | 可能需要降级（见 Provider Capability） |
| `parallel_tool_calls` | `parallel_tool_calls` | 仅在提供者支持时透传 |
| `previous_response_id` | 从存储加载历史 | 加载历史消息合并到 messages |
| `reasoning` | ❌ dropped | Chat API 无对应语义 |

### 3.2 Role 规范化

Responses API 使用 `developer` 角色，Chat API 不支持，需要规范化：

| Responses Role | Chat Role | 说明 |
|----------------|-----------|------|
| `developer` | `system` | 开发者指令转系统指令 |
| `system` | `system` | 保持不变 |
| `user` | `user` | 保持不变 |
| `assistant` | `assistant` | 保持不变 |
| `tool` | `tool` | 保持不变 |

### 3.3 工具格式转换

**Responses 格式（输入）**：
```json
{
  "type": "function",
  "name": "get_weather",
  "description": "获取天气信息",
  "parameters": { "type": "object", "properties": {...} },
  "strict": true
}
```

**Chat 格式（输出）**：
```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "获取天气信息",
    "parameters": { "type": "object", "properties": {...} },
    "strict": true
  }
}
```

**转换逻辑**：
```python
def convert_tool(tool: dict) -> dict:
    if tool.get("type") == "function":
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
                "strict": tool.get("strict", False)
            }
        }
    return tool  # 其他类型透传
```

### 3.4 响应转换

| Chat 字段 | Responses 字段 | 说明 |
|-----------|----------------|------|
| `choices[].message.content` | `output[].message.content[].text` | 提取文本内容 |
| `choices[].message.tool_calls` | `output[].function_call` | 转换为 function_call 输出项 |
| `id` | `id` | 前缀 `resp_` 区分 |
| `usage` | `usage` | 透传 |

### 3.5 流式事件映射

| Chat Chunk | Responses Event |
|------------|-----------------|
| `role: assistant` | `response.created` + `response.in_progress` |
| `delta.content` | `response.output_text.delta` |
| `delta.tool_calls[].function.name` | `response.output_item.added` (type=function_call) |
| `delta.tool_calls[].function.arguments` | `response.function_call_arguments.delta` |
| `delta.tool_calls[].function.arguments` 完成 | `response.function_call_arguments.done` |
| `finish_reason: stop` | `response.output_item.done` + `response.completed` |
| `finish_reason: tool_calls` | `response.output_item.done` + `response.completed` |

---

## 4. Provider Capability 管理

### 4.1 能力定义

```python
class ProviderCapabilities:
    supports_tools: bool = True
    supports_tool_choice_auto: bool = True
    supports_parallel_tool_calls: bool = False
    supports_streaming: bool = True
    supports_system_role: bool = True
    requires_single_leading_system_message: bool = False
    max_context_tokens: int | None = None
```

### 4.2 内置提供者能力表

| 提供者 | tools | tool_choice=auto | parallel | streaming | system | 单前置 system | max_tokens |
|--------|-------|------------------|----------|-----------|--------|---------------|------------|
| openai | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 128K |
| minimax | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 256K |
| glm | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 128K |
| deepseek | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | 64K |

### 4.3 能力降级策略

```python
def handle_capability_mismatch(request, provider_caps, allow_downgrade=True):
    if request.tool_choice == "auto" and not provider_caps.supports_tool_choice_auto:
        if allow_downgrade:
            log.warning("Downgrading tool_choice from 'auto' to 'none'")
            return "none"
        else:
            raise AdapterError(400, "Provider does not support tool_choice=auto")
    return request.tool_choice
```

### 4.4 MiniMax 特殊处理

MiniMax 要求单条前置 system 消息，需要合并：

```python
def merge_system_messages_for_minimax(messages: list[dict]) -> list[dict]:
    """合并 instructions 和所有 developer/system 消息为单条 system 消息"""
    system_parts = []
    other_messages = []
    
    for msg in messages:
        if msg["role"] in ("system", "developer"):
            system_parts.append(msg["content"])
        else:
            other_messages.append(msg)
    
    if system_parts:
        merged_system = {"role": "system", "content": "\n\n".join(system_parts)}
        return [merged_system] + other_messages
    return other_messages
```

---

## 5. 状态管理

### 5.1 存储后端：磁盘文件

- 使用 `data/responses_session/` 目录存储会话文件
- 单文件存储单个 response_id 的完整记录
- TTL 过期自动清理，LRU 淘汰超出容量的文件

### 5.2 Session 数据模型

```python
class SessionRecord:
    response_id: str
    conversation: ConversationRecord
    response: ResponseRecord
    created_at: int                     # Unix 时间戳
    expires_at: int                     # 过期时间
    last_access_at: int                 # 最后访问时间

class ConversationRecord:
    messages: list[ChatMessage]         # 完整对话历史
    reasoning_history: list[dict]       # 推理历史（用于多轮推理恢复）
    tool_calls: list[ToolCallState]     # 工具调用状态

class ResponseRecord:
    id: str
    model: str
    status: str                         # "completed" | "failed" | "in_progress"
    output: list[dict]                  # Responses 格式输出项
    output_text: str                    # 纯文本输出
    usage: dict | None
```

### 5.3 存储文件格式

```json
{
  "response_id": "resp_xxx",
  "conversation": {
    "messages": [
      {"role": "system", "content": "..."},
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ],
    "reasoning_history": [],
    "tool_calls": []
  },
  "response": {
    "id": "resp_xxx",
    "model": "gpt-4o",
    "status": "completed",
    "output": [...],
    "output_text": "...",
    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
  },
  "created_at": 1715000000,
  "expires_at": 1715003600,
  "last_access_at": 1715003000
}
```

### 5.4 FileStore 接口

```python
class FileStore:
    """磁盘文件状态存储"""
    
    async def get_conversation(response_id: str) -> ConversationRecord | None
    async def get_response(response_id: str) -> ResponseRecord | None
    async def put(response_id: str, conversation: ConversationRecord, response: ResponseRecord)
    async def delete(response_id: str) -> bool
    async def _cleanup_if_needed()  # 清理过期和超容量文件
```

---

## 6. `<think>` 内容过滤

部分第三方模型将推理内容直接输出到可见文本：

```
<think>
internal reasoning...
</think>

final answer
```

### 6.1 非流式过滤

```python
def filter_think_content(content: str) -> str:
    """清理 choices[].message.content 中的 <think> 标签"""
    import re
    return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
```

### 6.2 流式过滤

```python
class ThinkFilter:
    """增量跨块过滤器，防止泄露部分标签或推理文本"""
    
    def __init__(self):
        self.buffer = ""
        self.in_think = False
    
    def feed(self, chunk: str) -> str:
        """处理一个 chunk，返回过滤后的内容"""
        self.buffer += chunk
        result = ""
        
        while self.buffer:
            if self.in_think:
                end_idx = self.buffer.find("</think>")
                if end_idx == -1:
                    self.buffer = ""
                    return result
                self.buffer = self.buffer[end_idx + 8:]
                self.in_think = False
            else:
                start_idx = self.buffer.find("<think>")
                if start_idx == -1:
                    # 检查是否有部分标签
                    for i in range(len(self.buffer) - 1, max(0, len(self.buffer) - 7), -1):
                        if self.buffer[i:].startswith("<"):
                            result += self.buffer[:i]
                            self.buffer = self.buffer[i:]
                            return result
                    result += self.buffer
                    self.buffer = ""
                    return result
                
                result += self.buffer[:start_idx]
                self.buffer = self.buffer[start_idx + 7:]
                self.in_think = True
        
        return result
```

---

## 7. Web Search 适配

### 7.1 背景

Responses API 的 `web_search` 是托管工具，在 OpenAI 服务器端执行。当代理到 Chat API 提供者时，需要适配器自身执行搜索。

### 7.2 设计

```
Codex ──(tools: web_search)─► │  1. web_search → function tool def │
                              │  2. 发送给 LLM                      │
                              │        ↓                            │
                              │  3. LLM 返回 function_call          │
                              │     ("__adapter_web_search")        │
                              │        ↓                            │
                              │  4. 适配器调用搜索 API               │
                              │     (Tavily / Brave)                │
                              │        ↓                            │
                              │  5. 注入搜索结果到消息               │
                              │  6. 再次请求 LLM → 最终答案          │
Codex ◄──────── 最终答案 ──────┘
```

### 7.3 工具转换

当检测到 `web_search` 工具且配置了搜索服务时，转换为：

```json
{
  "type": "function",
  "function": {
    "name": "__adapter_web_search",
    "description": "Search the web for current information.",
    "parameters": {
      "type": "object",
      "properties": {
        "query": { "type": "string", "description": "The search query" }
      },
      "required": ["query"]
    }
  }
}
```

### 7.4 多轮搜索循环

```python
MAX_SEARCH_ROUNDS = 3

async def handle_with_search(chat_req, search_service):
    for round in range(MAX_SEARCH_ROUNDS):
        response = await call_llm(chat_req)
        
        search_call = extract_search_call(response)
        if search_call:
            results = await search_service.search(search_call.query)
            
            # 追加 assistant tool_call + tool response
            chat_req.messages.append({"role": "assistant", "tool_calls": [...]})
            chat_req.messages.append({"role": "tool", "content": results})
            continue
        
        return response  # 非搜索响应，正常返回
```

### 7.5 配置

```python
class WebSearchConfig:
    provider: str = "tavily"            # "tavily" | "brave"
    api_key_env: str | None             # 搜索 API key 环境变量
    api_key: str | None                 # 内联 key（不推荐）
    max_results: int = 5
```

---

## 8. 错误处理

### 8.1 不支持场景的处理

| 场景 | Codex 请求 | 适配器行为 |
|------|------------|------------|
| 推理项 | `input: [Reasoning {...}]` | 返回 HTTP 400 |
| 托管工具 | `input: [WebSearchCall ...]` | 返回 HTTP 400（未配置搜索时） |
| 不支持的角色 | `role: "critic"` | 返回 HTTP 400 |
| 能力不匹配 | `tool_choice=auto` 且不支持 | 降级或返回 400（根据配置） |
| 历史不存在 | `previous_response_id: "xxx"` 不存在 | 返回 HTTP 400 |

### 8.2 错误响应格式

```python
class AdapterError(Exception):
    status_code: int
    message: str
    error_type: str

# 示例
raise AdapterError(400, "Hosted tools are not supported", "unsupported_feature")
```

---

## 9. 架构

### 9.1 请求流程

```
POST /v1/responses
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│  1. parse_responses_request(body)                       │
│     - 验证 model、input 必填                            │
│     - 提取 previous_response_id                         │
│     - 检测不支持的功能（托管工具、推理项）               │
│                                                         │
│  2. load_history(previous_response_id)                  │
│     - 从 FileStore 加载历史                             │
│     - 找不到返回 400                                    │
│                                                         │
│  3. convert_request(resp_req, provider_caps)            │
│     - instructions → system message                     │
│     - Role 规范化 (developer → system)                  │
│     - input → messages 转换                             │
│     - 工具格式转换                                      │
│     - 能力降级处理                                      │
│     - MiniMax 特殊合并（如适用）                        │
│                                                         │
│  4. merge_history(history, current_messages)            │
│     - 合并历史消息 + 当前输入                           │
│                                                         │
│  5. generate_response_id() → "resp_" + 24字符hex        │
│                                                         │
│  6. proxy_request(chat_req) → 上游                      │
│                                                         │
│  7. 流式/非流式处理                                     │
│     - `<think>` 内容过滤                                  │
│     - 累积 assistant 消息                               │
│     - 流式事件转换                                      │
│     - 存储对话历史                                      │
│     - 返回 Responses 格式                               │
└─────────────────────────────────────────────────────────┘
```

### 9.2 文件结构

```
llm-plug/
├── state_store.py              # 新增：磁盘文件状态存储
├── responses_handler.py        # 新增：请求处理逻辑
├── request_converter.py        # 新增：请求格式转换
├── response_converter.py       # 新增：响应格式转换
├── capability_manager.py       # 新增：Provider Capability 管理
├── think_filter.py             # 新增：`<think>` 内容过滤
├── web_search_service.py       # 新增：Web Search 适配
├── converters/
│   └── response_stream.py      # 新增：流式事件转换
├── routers/
│   └── proxy_response.py       # 修改：新增 GET/DELETE 端点
├── config.py                   # 修改：添加配置项
├── main.py                     # 修改：添加清理任务
└── data/
    └── responses_session/      # 新增：会话存储目录
```

---

## 10. 配置项

| 配置项 | 类型 | 默认值 | 环境变量 | 说明 |
|--------|------|--------|----------|------|
| response_state_max_entries | int | 1000 | RESPONSE_STATE_MAX_ENTRIES | 最大会话数 |
| response_state_ttl_minutes | int | 60 | RESPONSE_STATE_TTL_MINUTES | 会话 TTL |
| response_state_cleanup_interval_minutes | int | 30 | RESPONSE_STATE_CLEANUP_INTERVAL_MINUTES | 清理间隔 |
| allow_capability_downgrade | bool | true | ALLOW_CAPABILITY_DOWNGRADE | 允许能力降级 |
| web_search_provider | str | None | WEB_SEARCH_PROVIDER | 搜索提供者 |
| web_search_api_key_env | str | None | WEB_SEARCH_API_KEY_ENV | 搜索 API key 环境变量 |

---

## 11. API 端点

### POST /v1/responses

请求体：
```json
{
  "model": "gpt-4o",
  "input": "hello" | [...],
  "instructions": "...",
  "tools": [...],
  "tool_choice": "auto" | {...},
  "stream": true | false,
  "previous_response_id": "resp_xxx",
  "store": true
}
```

响应体（非流式）：
```json
{
  "id": "resp_xxx",
  "object": "response",
  "created": 1715000000,
  "model": "gpt-4o",
  "status": "completed",
  "output": [
    {
      "type": "message",
      "id": "msg_xxx",
      "role": "assistant",
      "content": [{"type": "output_text", "text": "..."}]
    }
  ],
  "output_text": "...",
  "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
}
```

### GET /v1/responses/{id}

返回已存储的响应。

### DELETE /v1/responses/{id}

删除存储的响应。

---

## 12. 后台任务

```python
async def _session_cleanup_loop():
    """定期清理过期会话文件"""
    interval = get_setting("response_state_cleanup_interval_minutes", 30)
    while True:
        await asyncio.sleep(interval * 60)
        await store._cleanup_if_needed()
```

---

## 13. 测试用例

| # | 场景 | 输入 | 预期输出 |
|---|------|------|----------|
| 1 | 基本文本请求 | `instructions` + user message | 正确转换并返回文本 |
| 2 | Role 规范化 | `role: "developer"` | 转换为 `"system"` |
| 3 | 不支持的角色 | `role: "critic"` | HTTP 400 错误 |
| 4 | 空工具数组 | `tools: []` | 省略 `tools` 字段 |
| 5 | 工具格式转换 | Responses function tools | 正确转换为 Chat 格式 |
| 6 | 拒绝推理项 | input 包含 `Reasoning` | HTTP 400 错误 |
| 7 | 流式文本 | `stream: true` | 正确的 Responses SSE 输出 |
| 8 | 能力降级 | `tool_choice=auto`，不支持 | 根据配置降级或报错 |
| 9 | MiniMax 多 system | `instructions` + `developer` | 合并为单条前置 system |
| 10 | `<think>` 过滤 | content 包含 think 块 | 仅返回最终答案 |
| 11 | 托管工具拒绝 | `input: [WebSearchCall]` | HTTP 400 错误（未配置搜索） |
| 12 | Web Search 适配 | tools 包含 web_search | 转换为 __adapter_web_search |
| 13 | 多轮对话 | `previous_response_id` 存在 | 正确加载历史 |
| 14 | 历史不存在 | `previous_response_id` 不存在 | HTTP 400 错误 |
| 15 | GET 端点 | `GET /v1/responses/{id}` | 返回存储的响应 |
| 16 | DELETE 端点 | `DELETE /v1/responses/{id}` | 删除成功 |
| 17 | TTL 过期 | 超过 TTL 的会话 | 自动清理 |
| 18 | LRU 淘汰 | 超出容量 | 删除最旧文件 |
| 19 | 并行工具调用 | `parallel_tool_calls: true` | 仅在支持时透传 |
| 20 | 流式 `<think>` 过滤 | 流式包含 think 标签 | 增量过滤无泄露 |

---

## 14. 第一阶段不实现

| 功能 | 原因 | 后续迭代 |
|------|------|----------|
| MCP 工具本地执行 | 复杂度高，先简化 | 第二阶段 |
| 内置工具重写（file_search） | 需 MCP Manager | 第二阶段 |
| conversation 字段 | 仅支持 previous_response_id | 需要时添加 |
| 多模态输入 | 图片降级为文本提示 | 需要时完善 |
| reasoning 连续性 | Chat API 无对应语义 | 记录日志警告 |

---

## 15. 参考

- [codex-responses-adapter 设计文档](https://github.com/SSebo/codex-responses-adapter/blob/main/docs/specs/2026-03-11-responses-adapter-design.md)
- [OpenAI Responses API 规范](https://platform.openai.com/docs/api-reference/responses)
- Go 项目：`codex-responses-adapter/internal/`

---

*文档版本：2.0*
*最后更新：2026-05-05*
