# Converter 完整性审计与修复设计

## 背景

本项目是 LLM API 转换代理，支持 OpenAI Chat Completions、OpenAI Response、Anthropic Messages 三种格式互转。核心使用场景：Claude Code 发送 Anthropic 格式请求，代理转换为上游 OpenAI 格式渠道。

当前 converter 代码覆盖了 6 个转换方向的请求/非流式响应/流式响应，但存在路由缺失、字段映射不完整、流式状态机缺陷和测试严重不足等问题。

## 目标

1. 补全 `proxy_core` 路由，消除静默直通
2. 基于 API 规范补齐所有缺失字段映射
3. 修复流式转换缺陷
4. 建立字段级对照表驱动的测试套件，通过测试发现并验证所有修复

## 方案

采用**测试先行**方案：先构建完整测试套件覆盖所有字段和流式场景，用测试暴露缺陷，再按优先级分阶段修复。测试数据引用 `docs/api-spec-anthropic-and-openai/` 中的官方示例结构。

## 已知缺陷清单

### P0 — 导致功能不可用

| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| 1 | `anthropic → openai-response` 路由缺失 | `proxy_core.py:101-139` | converter 已有实现，路由层静默直通导致格式完全错误 |
| 2 | `openai-chat → openai-response` 路由缺失 | 同上 | 同上 |
| 3 | `openai-response → openai-chat` 路由缺失 | 同上 | 同上 |

### P1 — 字段丢失导致功能降级

| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| 4 | Anthropic→Chat: `thinking` 字段被丢弃 | `to_chat._anthropic_request_to_chat` | 应映射为 `reasoning_effort` 或透传 |
| 5 | Chat→Anthropic: 多条 system 消息只保留最后一条 | `to_anthropic._chat_request_to_anthropic` | Anthropic `system` 支持数组，应合并 |
| 6 | Anthropic→Chat: `tool_choice.type="any"` 未映射 | `to_chat` | 应映射为 `"required"` |
| 7 | Anthropic→Chat: `tool_choice.type="none"` 未映射 | `to_chat` | 应映射为 `"none"` |
| 8 | Chat→Anthropic: `tool_choice="required"` 未映射 | `to_anthropic` | 应映射为 `{"type":"any"}` |

### P2 — 流式转换缺陷

| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| 9 | `signature_delta` 事件未处理 | `to_anthropic` 流式 | Anthropic 规范要求 thinking 块结束前发 signature_delta |
| 10 | `message_start` 中 usage 全为 0 | `to_anthropic` 流式 | 应从首个 chunk 提取 input_tokens |
| 11 | `to_response` 中 tool_calls 的 output_index 硬编码为 0 | `to_response` 流式 | 多 tool call 时索引不正确 |

### P3 — 健壮性

| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| 12 | JSON 解析失败时静默回退为 `{}` | `to_anthropic` 多处 | 应记录 debug 日志 |
| 13 | 非 data: URL 的 image_url 原样保留 | `to_anthropic` | Anthropic 仅支持 base64 图片，应报 422 错误 |

## 修复策略与工作顺序

### 阶段 1：路由补全（P0 #1-3）

将 `_get_converter_and_upstream_type()` 的 3 个硬编码 if-elif 分支替换为查表逻辑：

```python
CONVERTER_MAP: dict[tuple[str, str], tuple[type, type]] = {
    # key: (source=上游渠道格式, target=客户端入口格式)
    # value: (RequestConverter, ResponseConverter)
    ("openai-chat-completions", "anthropic"): (ToAnthropicConverter, ToChatCompletionsConverter),
    ("openai-response", "anthropic"): (ToAnthropicConverter, ToResponseConverter),
    ("openai-response", "openai-chat-completions"): (ToChatCompletionsConverter, ToResponseConverter),
    ("anthropic", "openai-chat-completions"): (ToChatCompletionsConverter, ToAnthropicConverter),
    ("anthropic", "openai-response"): (ToResponseConverter, ToAnthropicConverter),
    ("openai-chat-completions", "openai-response"): (ToResponseConverter, ToChatCompletionsConverter),
}
```

函数逻辑：
1. `source == target` → 直通，返回 `(None, None, source)`
2. `(source, target)` 在表中 → 实例化 converter 返回
3. 不在表中 → 返回 501 错误，明确告知不支持的转换方向

每个请求创建新 converter 实例（流式状态机依赖此约定）。

### 阶段 2：字段补全（P1 #4-8）

按 API 规范对照表补齐：

| 转换方向 | 缺失字段 | 映射规则 |
|----------|----------|----------|
| Anthropic→Chat | `thinking.type="enabled"` | 映射为 `reasoning_effort`：数值型 budget_tokens 直接透传；Anthropic `thinking.type="adaptive"` 映射为 `"medium"` |
| Anthropic→Chat | `tool_choice.type="any"` | 映射为 `"required"` |
| Anthropic→Chat | `tool_choice.type="none"` | 映射为 `"none"` |
| Chat→Anthropic | 多条 system 消息 | 合并为 Anthropic `system` 数组（type:text block 数组） |
| Chat→Anthropic | `tool_choice="required"` | 映射为 `{"type":"any"}` |

### 阶段 3：流式修复（P2 #9-11）

- **signature_delta**（#9）：thinking content_block 关闭前，生成 `signature_delta` 事件，签名字段为空字符串 `""`（上游 OpenAI 不提供此信息，空签名确保事件序列结构完整）
- **message_start usage**（#10）：从首个包含 `usage.prompt_tokens` 的 chunk 中提取 input_tokens，填入 `message_start` 的 `message.usage`
- **output_index 硬编码**（#11）：用 `_stream_state` 跟踪当前 tool_call 计数器，递增分配 output_index

### 阶段 4：健壮性（P3 #12-13）

- **JSON 解析回退**（#12）：解析失败时 log debug 警告，仍返回空 dict，但不再静默
- **URL image_url**（#13）：在 Anthropic 上下文中遇到非 base64 的 image_url，返回 422 错误，消息说明 Anthropic 仅支持 base64 图片

## 测试架构

### 文件结构

```
tests/converters/
├── conftest.py                    # 共享 fixture：官方 API 规范示例数据
├── test_anthropic_to_chat.py      # 优先级最高（Claude Code 场景）
├── test_chat_to_anthropic.py
├── test_anthropic_to_response.py
├── test_chat_to_response.py
├── test_response_to_chat.py
├── test_response_to_anthropic.py
└── test_stream_sequences.py       # 所有方向的流式序列测试
```

### conftest.py 设计

提供三类 fixture，数据结构引用 `docs/api-spec-anthropic-and-openai/` 官方规范：

1. **`anthropic_request_*`** — Anthropic 请求体（基础对话、tool_use、thinking、多模态等）
2. **`openai_chat_request_*`** — 对应的 OpenAI Chat 请求体
3. **`anthropic_stream_events_*`** — Anthropic 流式事件序列（文本、tool_use、thinking、混合）

### 单元测试用例矩阵

以 `test_anthropic_to_chat.py` 为例（最高优先级）：

| 用例名 | 测试内容 | 验证点 |
|--------|----------|--------|
| `test_basic_request` | 基础 user/assistant 对话 | model, messages, max_tokens, temperature, top_p 映射 |
| `test_system_prompt` | system 字段 → system role message | 内容完整，多条 system 合并 |
| `test_tools` | Anthropic tools → OpenAI tools | input_schema → parameters, name/description 保留 |
| `test_tool_choice_auto` | `{"type":"auto"}` → `"auto"` | 值正确 |
| `test_tool_choice_any` | `{"type":"any"}` → `"required"` | P1#6 修复验证 |
| `test_tool_choice_none` | `{"type":"none"}` → `"none"` | P1#7 修复验证 |
| `test_tool_choice_tool` | `{"type":"tool","name":"X"}` → `{"type":"function","function":{"name":"X"}}` | 格式转换正确 |
| `test_thinking_enabled` | thinking → reasoning_effort | P1#4 修复验证 |
| `test_stop_sequences` | stop_sequences → stop | 数组格式正确 |
| `test_metadata` | metadata 透传 | user_id 映射 |
| `test_basic_response` | Anthropic 响应 → Chat 响应 | id 前缀, content, stop_reason → finish_reason |
| `test_tool_use_response` | 含 tool_use 的响应 | tool_use → tool_calls, arguments 反序列化 |
| `test_thinking_response` | 含 thinking content block 的响应 | thinking → reasoning_content |

### 流式序列测试设计

`test_stream_sequences.py` 提供 `feed_events(converter, events) -> list[output]` 辅助函数，模拟逐 chunk 输入并收集全部输出（含 extra_events）。

| 用例名 | 输入序列 | 验证点 |
|--------|----------|--------|
| `test_anthropic_text_to_chat` | message_start → cbs(text) → cbd(text_delta)×3 → cb_stop → message_delta → message_stop | 输出为 OpenAI chunk 格式，content 逐段，finish_reason 正确 |
| `test_anthropic_tool_use_to_chat` | 含 tool_use content block 的完整序列 | tool_calls 增量，index 正确，arguments 拼接完整 |
| `test_anthropic_thinking_to_chat` | 含 thinking block 的完整序列 | thinking 内容映射为 reasoning_content |
| `test_anthropic_mixed_to_chat` | thinking → text → tool_use 三段序列 | 内容类型切换正确，index 递增 |
| `test_chat_text_to_anthropic` | OpenAI 文本流 → Anthropic SSE | message_start/content_block_start/delta/stop/message_delta/message_stop 事件序列完整 |
| `test_chat_tool_use_to_anthropic` | OpenAI tool_calls 流 → Anthropic | tool_use content_block，input_json_delta 正确 |
| `test_chat_thinking_to_anthropic` | OpenAI reasoning_content 流 → Anthropic | thinking content_block，thinking_delta，signature_delta |
| `test_chat_mixed_to_anthropic` | 混合内容流 → Anthropic | 内容类型切换时 content_block_stop + 新 content_block_start |

## 验证标准

每个阶段完成后执行：
- `uv run pytest` — 所有测试通过
- `uv run ruff check .` — 无 lint 错误
