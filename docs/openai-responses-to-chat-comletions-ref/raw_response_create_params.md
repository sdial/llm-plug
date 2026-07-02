# ResponseCreateParams 字段定义

> 来源：https://raw.githubusercontent.com/openai/openai-python/master/src/openai/types/responses/response_create_params.py

ResponseCreateParams 是一个联合类型：`Union[ResponseCreateParamsNonStreaming, ResponseCreateParamsStreaming]`，两者都继承自 `ResponseCreateParamsBase`。

---

## 核心字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | `ResponsesModel` | 是（无默认值但非Required） | 模型ID，如 `gpt-4o` 或 `o3` |
| `input` | `Union[str, ResponseInputParam]` | 否 | 文本、图片或文件输入 |
| `instructions` | `Optional[str]` | 否 | 系统/开发者消息 |
| `stream` | `Literal[False]` 或 `Literal[True]` | 否 | 非流式默认False；流式为Required True |

---

## 采样控制

| 字段 | 类型 | 说明 |
|------|------|------|
| `temperature` | `Optional[float]` | 采样温度，0到2之间 |
| `top_p` | `Optional[float]` | 核采样概率质量 |
| `top_logprobs` | `Optional[int]` | 每个位置返回的最可能token数，0-20 |

---

## 输出限制

| 字段 | 类型 | 说明 |
|------|------|------|
| `max_output_tokens` | `Optional[int]` | 生成token上限，含可见输出和推理token |
| `max_tool_calls` | `Optional[int]` | 内置工具最大调用总数 |
| `truncation` | `Optional[Literal["auto", "disabled"]]` | 截断策略，默认disabled（超限报400错误） |

---

## 工具配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `tools` | `Iterable[ToolParam]` | 模型可调用的工具数组 |
| `tool_choice` | `ToolChoice` | 模型选择工具的方式，支持多种子类型 |
| `parallel_tool_calls` | `Optional[bool]` | 是否允许并行工具调用 |

ToolChoice 是联合类型，包含：
- `ToolChoiceOptions`
- `ToolChoiceAllowedParam`
- `ToolChoiceTypesParam`
- `ToolChoiceFunctionParam`
- `ToolChoiceMcpParam`
- `ToolChoiceCustomParam`
- `ToolChoiceApplyPatchParam`
- `ToolChoiceShellParam`

---

## 推理配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `reasoning` | `Optional[Reasoning]` | 推理模型配置，仅限"gpt-5 and o-series models" |

---

## 文本输出配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | `ResponseTextConfigParam` | 文本响应配置，支持纯文本或结构化JSON |

---

## 上下文与对话管理

| 字段 | 类型 | 说明 |
|------|------|------|
| `conversation` | `Optional[Conversation]` | 对话归属，类型为 `Union[str, ResponseConversationParamParam]` |
| `context_management` | `Optional[Iterable[ContextManagement]]` | 上下文管理配置，当前仅支持 `'compaction'` 类型 |
| `previous_response_id` | `Optional[str]` | 前一次响应ID，用于多轮对话，不可与 `conversation` 同时使用 |

ContextManagement 子字段：
- `type`（Required, str）
- `compact_threshold`（Optional, int）

---

## 缓存与存储

| 字段 | 类型 | 说明 |
|------|------|------|
| `store` | `Optional[bool]` | 是否存储响应以供后续检索 |
| `prompt` | `Optional[ResponsePromptParam]` | 可复用提示模板引用 |
| `prompt_cache_key` | `str` | 缓存优化键，"replaces the `user` field" |
| `prompt_cache_retention` | `Optional[Literal["in_memory", "24h"]]` | 提示缓存保留策略 |

---

## 用户标识与安全

| 字段 | 类型 | 说明 |
|------|------|------|
| `user` | `str` | 旧版用户标识，"being replaced by `safety_identifier` and `prompt_cache_key`" |
| `safety_identifier` | `str` | 稳定用户标识符，最长64字符，建议哈希处理 |
| `metadata` | `Optional[Metadata]` | 最多16个键值对，键最长64字符，值最长512字符 |

---

## 服务与流式选项

| 字段 | 类型 | 说明 |
|------|------|------|
| `service_tier` | `Optional[Literal["auto", "default", "flex", "scale", "priority"]]` | 请求处理类型 |
| `background` | `Optional[bool]` | 是否后台运行模型响应 |
| `stream_options` | `Optional[StreamOptions]` | 流式响应选项，仅stream为true时设置 |
| `include` | `Optional[List[ResponseIncludable]]` | 额外输出数据，如 `web_search_call.action.sources`、`reasoning.encrypted_content` 等 |

StreamOptions 子字段：
- `include_obfuscation`（bool）：控制流混淆以缓解侧信道攻击

---

## 非流式 vs 流式区别

- **ResponseCreateParamsNonStreaming**：`stream` 为 `Optional[Literal[False]]`，所有字段均可选（`total=False`）
- **ResponseCreateParamsStreaming**：`stream` 为 `Required[Literal[True]]`，其余字段继承自 Base

---

## 核心方法（5个）

| 方法 | HTTP 端点 | 返回类型 |
|------|-----------|----------|
| `create` | `POST /responses` | `Response` |
| `retrieve` | `GET /responses/{response_id}` | `Response` |
| `delete` | `DELETE /responses/{response_id}` | `None` |
| `cancel` | `POST /responses/{response_id}/cancel` | `Response` |
| `compact` | `POST /responses/compact` | `CompactedResponse` |

## 子资源

### InputItems
- 方法：`list` → `GET /responses/{response_id}/input_items`，返回 `SyncCursorPage[ResponseItem]`
- 类型：`ResponseItemList`

### InputTokens
- 方法：`count` → `POST /responses/input_tokens`，返回 `InputTokenCountResponse`
- 类型：`InputTokenCountResponse`
