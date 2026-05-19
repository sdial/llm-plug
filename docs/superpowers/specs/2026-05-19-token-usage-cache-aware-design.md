# Token Usage 转换器修复：Cache / Reasoning 感知

**Date**: 2026-05-19
**Scope**: 转换器层的 `usage` 字段精度修复
**DB Migration**: 无

## 背景

REVIEW1.md 列出的 Major 项：

> `[conv]` `total_tokens` 自己累加，**忽略 cached/reasoning tokens**，计费偏低

实际影响面比单一行更广，包含两个方向的对称问题：

- **Anthropic → OpenAI 方向**：Anthropic `input_tokens` 是「非缓存输入」，`cache_creation_input_tokens` 与 `cache_read_input_tokens` 是独立字段。转换器把 `prompt_tokens = input_tokens`、`total_tokens = input_tokens + output_tokens`，**漏掉了两类 cache tokens**，导致下游 SDK 看到的输入计数偏低；落库的 `request_logs.input_tokens` / `request_stats_raw.input_tokens` 也偏低。
- **OpenAI → Anthropic 方向**：转换器从 OpenAI usage 里读 `cache_creation_input_tokens` / `cache_read_input_tokens`，但 OpenAI 根本没有这两个字段（它用 `prompt_tokens_details.cached_tokens`），结果反向永远输出 cache=0。

Reasoning tokens 这块在 OpenAI 语义下 `completion_tokens` 已经包含 `reasoning_tokens`（OpenAI 文档原话）；Anthropic 的 thinking tokens 算进 `output_tokens`。所以**不存在 reasoning 漏算**，问题完全集中在 cache。

## 目标 & 非目标

**目标**
- 转换器返回给客户端的 usage 数字与上游账单口径一致。
- 落库的 `input_tokens` / `output_tokens` 自然变准（因为转换器输出变准了，落库逻辑不变）。
- 修复反向（OpenAI → Anthropic）的对称 bug。

**非目标**
- 不改 DB schema、不加 `cache_read_tokens` 等细分列。
- 不上「缓存命中率 / 分层成本」报表。后续若需要再迭代。
- 不重构 stats 聚合 SQL，不动前端。

## 字段映射规范

### Anthropic usage → OpenAI usage

| OpenAI 字段 | 计算 |
|---|---|
| `prompt_tokens` | `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` |
| `completion_tokens` | `output_tokens` |
| `total_tokens` | `prompt_tokens + completion_tokens` |
| `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |

> `cache_creation_input_tokens` 在 OpenAI 语义里没有对应概念（OpenAI 不向客户端暴露「写缓存」的成本），归并进 `prompt_tokens` 总数即可，不单列。

### OpenAI usage → Anthropic usage

| Anthropic 字段 | 计算 |
|---|---|
| `input_tokens` | `max(prompt_tokens - cached_tokens, 0)` |
| `output_tokens` | `completion_tokens` |
| `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` |
| `cache_creation_input_tokens` | `0`（OpenAI 不返回此量，无法重建） |

### OpenAI Chat usage → OpenAI Response usage

已正确，保持现状。`to_response.py:76-90` 已透传 `cached_tokens` 与 `reasoning_tokens`。

## 设计

### 1. 新增模块 `converters/usage.py`

集中放映射工具函数，避免在 6 个转换分支各写一遍。

```python
# converters/usage.py
def anthropic_to_openai_chat(usage: dict) -> dict: ...
def anthropic_to_openai_response(usage: dict) -> dict: ...
def openai_chat_to_anthropic(usage: dict) -> dict: ...
def openai_response_to_anthropic(usage: dict) -> dict: ...
```

四个函数都接受 `dict` 输入，缺字段 default 为 0，永不抛异常。Response 与 Chat 的差异仅在字段命名（`input_tokens` vs `prompt_tokens`），底层算式一致。

### 2. 转换器落点

| 文件:行 | 函数 | 改动 |
|---|---|---|
| `converters/to_chat.py:383` | `_anthropic_response_to_chat` 非流式 | 调 `anthropic_to_openai_chat(...)` 替换 usage dict |
| `converters/to_chat.py:787` | 另一非流式分支 | 同上 |
| `converters/to_chat.py:486` (`message_delta`) | Anthropic→Chat 流式 | 新增：聚合 `message_start` 与 `message_delta` 的 usage，**仅当请求带 `stream_options.include_usage=true`** 时在末帧 emit 一条不含 `choices` 的 usage chunk。否则保持现状不 emit |
| `converters/to_response.py:388` | `_anthropic_response_to_response` 非流式 | 调 `anthropic_to_openai_response(...)` |
| `converters/to_response.py` 流式聚合处 | Anthropic→Response 流式 | 同上工具函数，在 `_stream_state` 累加，`response.completed` 帧使用 |
| `converters/to_anthropic.py:362` | Chat→Anthropic 非流式 | 调 `openai_chat_to_anthropic(...)` |
| `converters/to_anthropic.py:668` | Response→Anthropic 非流式 | 调 `openai_response_to_anthropic(...)` |
| `converters/to_anthropic.py:391` `message_delta` | Chat→Anthropic 流式 | 使用工具函数计算 cache 字段 |
| `converters/to_anthropic.py:836` `message_delta` | Response→Anthropic 流式 | 同上 |
| `proxy_core.py:329-334, 361-365` | `_build_openai_stream_response` | total 计算优先用上游 chunk 的 `usage.total_tokens`；缺失才 fallback 到 prompt+completion。同时把 `prompt_tokens_details` 与 `completion_tokens_details` 透传出去 |

### 3. `stream_options.include_usage` 处理

OpenAI Chat Completions 流式协议中，只有客户端在请求体显式带 `stream_options.include_usage=true`，上游才在最后一帧 emit usage chunk。当前 Anthropic→Chat 流式路径**没处理这个选项也没 emit usage**。修复中：

- 从原始请求体读取 `stream_options.include_usage`（已在 `proxy_core.py` 的请求转换前可访问）。
- 转换器在初始化 `_stream_state` 时携带这个 flag。
- 末帧仅当 flag=true 时 emit `{"choices": [], "usage": {...}}` 一帧（按 OpenAI 协议 choices 为空数组）。

这是**增量行为**：之前不传 usage 的客户端继续不收到 usage；要的客户端能拿到了。

### 4. 数据流

```
上游响应 (Anthropic 含 cache 字段)
   ↓
转换器调 anthropic_to_openai_*  ← 唯一计算点
   ↓
返回给客户端 / 同时被 proxy_core 提取 usage.prompt_tokens 写入 DB
   ↓
DB 的 input_tokens 列自然反映「合计输入」
   ↓
stats 报表数字变准（不需要改 SQL）
```

### 5. 错误处理

- 工具函数对缺失字段全部 default 0，不抛。
- `prompt_tokens - cached_tokens` 用 `max(..., 0)` 防御性兜底，避免上游异常数据导致负数。
- 上游完全没返回 `usage`（例如错误响应）时，所有数字落 0，与现状一致。

## 测试

**新增** `tests/converters/test_usage_mapping.py`：直接对 4 个工具函数做表驱动测试。

**修改/补充** `tests/converters/test_converter_matrix.py` 或对应方向用例：

| 方向 | 用例 |
|---|---|
| Anthropic → Chat 非流式 | input=10, cache_creation=100, cache_read=1000, output=50 → prompt=1110, cached=1000, total=1160 |
| Anthropic → Chat 流式 | 同上数据分散到 `message_start.usage` 与 `message_delta.usage`；带 `include_usage=true` 断言末帧；不带断言无末帧 usage chunk |
| Anthropic → Response 非流式 | 同上字段映射到 `input_tokens` / `input_tokens_details.cached_tokens` |
| Anthropic → Response 流式 | `response.completed` 帧字段正确 |
| Chat → Anthropic 非流式 | prompt=1000, cached=900, completion=50 → input=100, cache_read=900, cache_creation=0, output=50 |
| Chat → Anthropic 流式 | message_delta usage 字段正确 |
| Response → Anthropic 非流式 + 流式 | 同 Chat 方向规则 |

**回归**：跑 `tests/converters/test_stream_sequences.py` 全套，确保现有用例不破。`tests/test_proxy_core.py` 跑全套。

## 兼容性

- 客户端看到的 `prompt_tokens` / `input_tokens` 数字**会变大**（变准）。这对计费场景是修复，不是 break。
- 流式 Chat 路径在客户端传 `stream_options.include_usage=true` 时会多收到一帧——这是符合 OpenAI 协议的正确行为。不传则无变化。
- DB schema 不动，旧 stats 数据保持原值，新数据起逐渐变准。

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 流式末帧的 usage chunk 破坏某些非标客户端的解析 | 默认不 emit，仅在 `include_usage=true` 时才 emit |
| `max(pt - cached, 0)` 的兜底掩盖上游 bug | logger.warning 记录异常情形（pt < cached），便于排查 |
| stats 历史数据 vs 新数据口径不一致 | 接受。属于「修复后逐步收敛」 |

回滚策略：单文件 `converters/usage.py` 是新增的；落点改动是局部替换。Revert 该提交即可。
