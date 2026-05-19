# Token Usage Cache-Aware 转换器修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复转换器层 `usage` 字段：Anthropic 与 OpenAI 之间的 6 个转换方向都正确处理 cache_creation / cache_read tokens；反向消除当前从 OpenAI usage 读取不存在字段的对称 bug；OpenAI Chat 流式按协议在客户端 `stream_options.include_usage=true` 时 emit usage 末帧。

**Architecture:** 新建 `converters/usage.py` 集中放 4 个纯函数；6 个转换分支与 `proxy_core.py:_build_openai_stream_response` 调用这些函数替换原地手算逻辑。Anthropic→Chat 流式新增 `set_stream_include_usage` 入口，proxy_core 在创建 converter 后读取原请求 `stream_options.include_usage` 透传给它。

**Tech Stack:** Python 3.11+ / pytest / pyproject + uv。

**Spec:** `docs/superpowers/specs/2026-05-19-token-usage-cache-aware-design.md`

---

## File Structure

| 路径 | 操作 | 职责 |
|---|---|---|
| `converters/usage.py` | **Create** | 4 个纯函数：`anthropic_to_openai_chat` / `anthropic_to_openai_response` / `openai_chat_to_anthropic` / `openai_response_to_anthropic` |
| `tests/converters/test_usage_mapping.py` | **Create** | 4 个函数的表驱动单元测试 |
| `converters/to_chat.py` | Modify | 2 处非流式 (`:383`, `:787`) + 1 处流式 (`_anthropic_stream_chunk_to_chat`) + 新增 `set_stream_include_usage` |
| `converters/to_response.py` | Modify | 1 处非流式 (`:383`) + 流式 `_build_final_events` / `_release_pending_completed_event` |
| `converters/to_anthropic.py` | Modify | 2 处非流式 (`:362`, `:668`) + 2 处流式 message_delta (`:391`, `:836`) |
| `proxy_core.py` | Modify | `_build_openai_stream_response`：total 优先取上游 + 透传 details；`_do_request`：set_stream_include_usage 调用 |
| `tests/converters/test_anthropic_to_chat.py` | Modify | 加 cache token 用例 |
| `tests/converters/test_chat_to_anthropic.py` | Modify | 加 cached_tokens 反向用例 |
| `tests/converters/test_response_to_chat.py` | Modify | 加 cache token 用例（Anthropic→Response 在哪个文件视实际情况补） |
| `tests/converters/test_stream_sequences.py` | Modify | 流式 cache token 用例 |
| `tests/test_proxy_core.py` | Modify | include_usage 末帧用例 + `_build_openai_stream_response` 用例 |

---

## Task 1: 新建 `converters/usage.py` 与表驱动测试

**Files:**
- Create: `converters/usage.py`
- Create: `tests/converters/test_usage_mapping.py`

- [ ] **Step 1: 写失败测试 `test_usage_mapping.py`**

```python
# tests/converters/test_usage_mapping.py
import pytest
from converters.usage import (
    anthropic_to_openai_chat,
    anthropic_to_openai_response,
    openai_chat_to_anthropic,
    openai_response_to_anthropic,
)


class TestAnthropicToOpenAIChat:
    def test_full_fields(self):
        result = anthropic_to_openai_chat({
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "output_tokens": 50,
        })
        assert result == {
            "prompt_tokens": 1110,
            "completion_tokens": 50,
            "total_tokens": 1160,
            "prompt_tokens_details": {"cached_tokens": 1000},
        }

    def test_missing_cache_fields(self):
        result = anthropic_to_openai_chat({"input_tokens": 5, "output_tokens": 7})
        assert result == {
            "prompt_tokens": 5,
            "completion_tokens": 7,
            "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 0},
        }

    def test_empty_input(self):
        result = anthropic_to_openai_chat({})
        assert result == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 0},
        }


class TestAnthropicToOpenAIResponse:
    def test_full_fields(self):
        result = anthropic_to_openai_response({
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "output_tokens": 50,
        })
        assert result == {
            "input_tokens": 1110,
            "output_tokens": 50,
            "total_tokens": 1160,
            "input_tokens_details": {"cached_tokens": 1000},
        }


class TestOpenAIChatToAnthropic:
    def test_full_fields(self):
        result = openai_chat_to_anthropic({
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 900},
        })
        assert result == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        }

    def test_no_details(self):
        result = openai_chat_to_anthropic({"prompt_tokens": 50, "completion_tokens": 20})
        assert result == {
            "input_tokens": 50,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def test_cached_exceeds_prompt_clamps_to_zero(self):
        result = openai_chat_to_anthropic({
            "prompt_tokens": 10,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 100},
        })
        assert result["input_tokens"] == 0
        assert result["cache_read_input_tokens"] == 100


class TestOpenAIResponseToAnthropic:
    def test_full_fields(self):
        result = openai_response_to_anthropic({
            "input_tokens": 1000,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 900},
        })
        assert result == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        }
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/converters/test_usage_mapping.py -v
```
Expected: 全部失败，`ModuleNotFoundError: converters.usage`

- [ ] **Step 3: 写最小实现 `converters/usage.py`**

```python
"""Token usage 字段在 Anthropic / OpenAI 两种语义之间的映射。

Anthropic:
  - input_tokens 不含 cache
  - cache_creation_input_tokens / cache_read_input_tokens 独立计费字段
  - output_tokens 含 thinking

OpenAI:
  - prompt_tokens 含全部输入（包括缓存命中）
  - prompt_tokens_details.cached_tokens 是 prompt_tokens 的子集
  - completion_tokens 含 reasoning
  - completion_tokens_details.reasoning_tokens 是 completion_tokens 的子集
"""
from __future__ import annotations
from typing import Any
from loguru import logger


def _read_int(d: dict[str, Any] | None, key: str) -> int:
    if not isinstance(d, dict):
        return 0
    value = d.get(key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def anthropic_to_openai_chat(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic usage → OpenAI Chat Completions usage."""
    if not isinstance(usage, dict):
        usage = {}
    inp = _read_int(usage, "input_tokens")
    cc = _read_int(usage, "cache_creation_input_tokens")
    cr = _read_int(usage, "cache_read_input_tokens")
    out = _read_int(usage, "output_tokens")
    prompt = inp + cc + cr
    return {
        "prompt_tokens": prompt,
        "completion_tokens": out,
        "total_tokens": prompt + out,
        "prompt_tokens_details": {"cached_tokens": cr},
    }


def anthropic_to_openai_response(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic usage → OpenAI Response usage."""
    if not isinstance(usage, dict):
        usage = {}
    inp = _read_int(usage, "input_tokens")
    cc = _read_int(usage, "cache_creation_input_tokens")
    cr = _read_int(usage, "cache_read_input_tokens")
    out = _read_int(usage, "output_tokens")
    total_input = inp + cc + cr
    return {
        "input_tokens": total_input,
        "output_tokens": out,
        "total_tokens": total_input + out,
        "input_tokens_details": {"cached_tokens": cr},
    }


def openai_chat_to_anthropic(usage: dict[str, Any] | None) -> dict[str, Any]:
    """OpenAI Chat usage → Anthropic usage。OpenAI 不区分 cache_creation。"""
    if not isinstance(usage, dict):
        usage = {}
    pt = _read_int(usage, "prompt_tokens")
    ct = _read_int(usage, "completion_tokens")
    cached = _read_int(usage.get("prompt_tokens_details"), "cached_tokens")
    if cached > pt:
        logger.warning(
            "openai_chat_to_anthropic: cached_tokens (%d) > prompt_tokens (%d), clamping input_tokens to 0",
            cached, pt,
        )
    return {
        "input_tokens": max(pt - cached, 0),
        "output_tokens": ct,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


def openai_response_to_anthropic(usage: dict[str, Any] | None) -> dict[str, Any]:
    """OpenAI Response usage → Anthropic usage。"""
    if not isinstance(usage, dict):
        usage = {}
    inp = _read_int(usage, "input_tokens")
    out = _read_int(usage, "output_tokens")
    cached = _read_int(usage.get("input_tokens_details"), "cached_tokens")
    if cached > inp:
        logger.warning(
            "openai_response_to_anthropic: cached_tokens (%d) > input_tokens (%d), clamping to 0",
            cached, inp,
        )
    return {
        "input_tokens": max(inp - cached, 0),
        "output_tokens": out,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/converters/test_usage_mapping.py -v
```
Expected: 9 passed

- [ ] **Step 5: ruff 检查**

```bash
uv run ruff check converters/usage.py tests/converters/test_usage_mapping.py
```
Expected: no issues

- [ ] **Step 6: Commit**

```bash
git add converters/usage.py tests/converters/test_usage_mapping.py
git commit -m "feat(converters): add cache-aware token usage mapping utils"
```

---

## Task 2: `to_chat.py` 非流式两处替换

**Files:**
- Modify: `converters/to_chat.py` lines 373-392 (`_response_to_chat` / Anthropic 响应入口 1) 和 lines 777-793 (`_anthropic_response_to_chat` 入口 2)
- Modify: `tests/converters/test_anthropic_to_chat.py`

- [ ] **Step 1: 写失败测试（加到 `test_anthropic_to_chat.py` 顶部 import 区下方）**

```python
# Append at end of tests/converters/test_anthropic_to_chat.py
def test_anthropic_to_chat_response_includes_cache_tokens():
    from converters.to_chat import ToChatCompletionsConverter
    conv = ToChatCompletionsConverter()
    anthropic_resp = {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "output_tokens": 50,
        },
    }
    result = conv.convert_response(anthropic_resp, source_type="anthropic")
    assert result["usage"]["prompt_tokens"] == 1110
    assert result["usage"]["completion_tokens"] == 50
    assert result["usage"]["total_tokens"] == 1160
    assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 1000
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/converters/test_anthropic_to_chat.py::test_anthropic_to_chat_response_includes_cache_tokens -v
```
Expected: AssertionError on `prompt_tokens == 1110`，当前实现返回 10。

- [ ] **Step 3: 修第一处（`to_chat.py:383-387`）**

把：
```python
            "usage": {
                "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0),
            }
```
替换为：
```python
            "usage": anthropic_to_openai_chat(data.get("usage")),
```

- [ ] **Step 4: 修第二处（`to_chat.py:787-791`）**

同样替换那个 usage dict 字面量为 `anthropic_to_openai_chat(data.get("usage"))`。

- [ ] **Step 5: 在 `to_chat.py` 顶部 import 区添加 import**

找到现有的 converters 内部 import 块（或文件顶 import 区），加：
```python
from converters.usage import anthropic_to_openai_chat
```

- [ ] **Step 6: 跑该测试 + 全部 anthropic_to_chat 测试确认通过**

```bash
uv run pytest tests/converters/test_anthropic_to_chat.py -v
```
Expected: 全部通过，含新加的用例。

- [ ] **Step 7: 跑整个 converters 测试套防回归**

```bash
uv run pytest tests/converters/ -v
```
Expected: 全部通过。

- [ ] **Step 8: Commit**

```bash
git add converters/to_chat.py tests/converters/test_anthropic_to_chat.py
git commit -m "fix(converters): Anthropic->Chat usage 含 cache_creation/read"
```

---

## Task 3: `to_chat.py` 流式 + include_usage 末帧

**Files:**
- Modify: `converters/to_chat.py`（`_reset_stream_state`、`_anthropic_stream_chunk_to_chat`、新增 `set_stream_include_usage`）
- Modify: `tests/converters/test_stream_sequences.py`

- [ ] **Step 1: 写失败测试**

加到 `tests/converters/test_stream_sequences.py`：

```python
def test_anthropic_to_chat_stream_emits_usage_when_include_usage():
    from converters.to_chat import ToChatCompletionsConverter
    conv = ToChatCompletionsConverter()
    conv.set_stream_include_usage(True)

    events = [
        {"type": "message_start", "message": {
            "id": "msg_x", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 0},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 50, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}},
        {"type": "message_stop"},
    ]
    out = [conv.convert_stream_chunk(e, source_type="anthropic") for e in events]
    out = [c for c in out if c is not None]
    # 最后一帧应为 usage chunk
    last = out[-1]
    assert last.get("choices") == []
    assert last["usage"]["prompt_tokens"] == 1110
    assert last["usage"]["completion_tokens"] == 50
    assert last["usage"]["total_tokens"] == 1160
    assert last["usage"]["prompt_tokens_details"]["cached_tokens"] == 1000


def test_anthropic_to_chat_stream_no_usage_when_flag_false():
    from converters.to_chat import ToChatCompletionsConverter
    conv = ToChatCompletionsConverter()
    # 不调用 set_stream_include_usage，默认 False

    events = [
        {"type": "message_start", "message": {"id": "msg_x", "model": "claude-opus-4-7",
                                              "usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    out = [conv.convert_stream_chunk(e, source_type="anthropic") for e in events]
    out = [c for c in out if c is not None]
    # 没有 usage chunk —— 所有 chunk 的 choices 都非空（含 delta 或 finish_reason）
    for c in out:
        assert c.get("choices"), f"unexpected usage chunk: {c}"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/converters/test_stream_sequences.py::test_anthropic_to_chat_stream_emits_usage_when_include_usage tests/converters/test_stream_sequences.py::test_anthropic_to_chat_stream_no_usage_when_flag_false -v
```
Expected: 第一个 AttributeError（无 `set_stream_include_usage`）、第二个能跑但行为可能依然 pass（旧实现不 emit usage）；只需确认第一个失败即可。

- [ ] **Step 3: 改 `to_chat.py` `__init__` 和 `_reset_stream_state`**

`__init__`（约 line 40-41）改为：
```python
    def __init__(self):
        self._stream_state: dict[str, Any] | None = None
        self._stream_include_usage: bool = False
```

`_reset_stream_state`（约 line 43-50）追加两个状态键：
```python
    def _reset_stream_state(self):
        self._stream_state = {
            "msg_id": "chatcmpl",
            "model": "",
            "tool_call_index": 0,
            "content_block_to_tc_index": {},
            "output_index_to_tc_index": {},
            "anthropic_usage": {},  # 累积 Anthropic 侧 usage（message_start 起跑，message_delta 覆盖）
        }
```

- [ ] **Step 4: 新增 `set_stream_include_usage` 方法**

在 `_reset_stream_state` 后插入：
```python
    def set_stream_include_usage(self, flag: bool) -> None:
        """供 proxy_core 在创建 converter 后透传客户端的 stream_options.include_usage。

        仅影响 Anthropic→Chat 流式：当 flag=True 时，在 message_stop 处 emit 末帧 usage chunk。
        """
        self._stream_include_usage = bool(flag)
```

- [ ] **Step 5: 修改 `_anthropic_stream_chunk_to_chat`：在 message_start 与 message_delta 累积 usage、在 message_stop 按 flag emit**

定位 `event_type == "message_start"` 分支（约 line 411-422），在 `self._stream_state["tool_call_index"] = 0` 后插入：
```python
            anthropic_usage = msg.get("usage")
            if isinstance(anthropic_usage, dict):
                self._stream_state["anthropic_usage"].update(anthropic_usage)
```

定位 `event_type == "message_delta"` 分支（line 486 起），在 `stop_reason = ...` 之后插入：
```python
            delta_usage = chunk.get("usage")
            if isinstance(delta_usage, dict):
                self._stream_state["anthropic_usage"].update(delta_usage)
```

修改 `event_type == "message_stop"` 分支（约 line 500-501，当前是 `return None`）：
```python
        elif event_type == "message_stop":
            if self._stream_include_usage:
                usage_payload = anthropic_to_openai_chat(self._stream_state.get("anthropic_usage"))
                return {
                    "id": self._stream_state["msg_id"],
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._stream_state["model"],
                    "choices": [],
                    "usage": usage_payload,
                }
            return None
```

- [ ] **Step 6: 跑两条新测试确认通过**

```bash
uv run pytest tests/converters/test_stream_sequences.py::test_anthropic_to_chat_stream_emits_usage_when_include_usage tests/converters/test_stream_sequences.py::test_anthropic_to_chat_stream_no_usage_when_flag_false -v
```
Expected: 两条 pass。

- [ ] **Step 7: 跑整个 stream_sequences 测试套防回归**

```bash
uv run pytest tests/converters/test_stream_sequences.py -v
```
Expected: 全部通过。

- [ ] **Step 8: Commit**

```bash
git add converters/to_chat.py tests/converters/test_stream_sequences.py
git commit -m "feat(converters): Anthropic->Chat stream emits usage chunk per include_usage"
```

---

## Task 4: `to_response.py` Anthropic→Response 非流式与流式

**Files:**
- Modify: `converters/to_response.py` line 383-395（非流式 `_anthropic_response_to_response`）, line 707-803（`_anthropic_stream_chunk_to_response` 流式），`_reset_stream_state` (around line 30-50) 加 `anthropic_usage` key
- Modify: `tests/converters/test_stream_sequences.py` 加非流式和流式两条用例

> **现状已确认**：`to_response.py:707-803` 的 `_anthropic_stream_chunk_to_response` 中 `message_delta` 分支 emit 的 `response.completed` 事件**完全没有 `usage` 字段**——既漏 cache，也漏 input/output 本身。这是比 spec 描述更严重的缺陷，本 Task 一并修。

- [ ] **Step 1: 写非流式失败测试**

```python
# 加到 tests/converters/test_stream_sequences.py 或合适位置
def test_anthropic_to_response_nonstream_cache_tokens():
    from converters.to_response import ToResponseConverter
    conv = ToResponseConverter()
    anthropic_resp = {
        "id": "msg_y",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "output_tokens": 50,
        },
    }
    result = conv.convert_response(anthropic_resp, source_type="anthropic")
    assert result["usage"]["input_tokens"] == 1110
    assert result["usage"]["output_tokens"] == 50
    assert result["usage"]["total_tokens"] == 1160
    assert result["usage"]["input_tokens_details"]["cached_tokens"] == 1000
```

- [ ] **Step 2: 跑确认失败**

```bash
uv run pytest tests/converters/test_stream_sequences.py::test_anthropic_to_response_nonstream_cache_tokens -v
```
Expected: AssertionError on `input_tokens == 1110`。

- [ ] **Step 3: 改非流式 `to_response.py:390-394`**

替换原 usage dict 字面量为：
```python
            "usage": anthropic_to_openai_response(data.get("usage")),
```

并在 `to_response.py` 顶部 import 区添加：
```python
from converters.usage import anthropic_to_openai_response
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/converters/test_stream_sequences.py::test_anthropic_to_response_nonstream_cache_tokens -v
```
Expected: pass。

- [ ] **Step 5: 写流式失败测试**

```python
def test_anthropic_to_response_stream_emits_full_usage():
    from converters.to_response import ToResponseConverter
    conv = ToResponseConverter()
    events = [
        {"type": "message_start", "message": {"id": "msg_z", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 50, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}},
        {"type": "message_stop"},
    ]
    completed_events = []
    for e in events:
        out = conv.convert_stream_chunk(e, source_type="anthropic")
        if isinstance(out, dict) and out.get("type") == "response.completed":
            completed_events.append(out)
    assert completed_events, "expected at least one response.completed event"
    usage = completed_events[-1]["response"].get("usage")
    assert usage is not None, "response.completed must carry usage"
    assert usage["input_tokens"] == 1110
    assert usage["output_tokens"] == 50
    assert usage["total_tokens"] == 1160
    assert usage["input_tokens_details"]["cached_tokens"] == 1000
```

- [ ] **Step 6: 跑确认失败**

```bash
uv run pytest tests/converters/test_stream_sequences.py::test_anthropic_to_response_stream_emits_full_usage -v
```
Expected: `usage is None` 断言失败——当前 `message_delta` emit 的 `response.completed` 没有 `usage` 字段。

- [ ] **Step 7: 在 `_reset_stream_state` 加 `anthropic_usage` key**

定位 `to_response.py` 内部的 `_reset_stream_state`（约 line 30-50），在已有的状态字典里加：
```python
            "anthropic_usage": {},  # Anthropic 上游流式累积 usage（含 cache 字段）
```

- [ ] **Step 8: 改 `_anthropic_stream_chunk_to_response` 累积 usage**

在 `message_start` 分支（line 713 起）的 `self._need_in_progress = True` 后插入：
```python
            anthropic_usage = msg.get("usage")
            if isinstance(anthropic_usage, dict):
                self._stream_state["anthropic_usage"].update(anthropic_usage)
```

- [ ] **Step 9: 改 `message_delta` 分支（line 785-795）**

替换为：
```python
        elif event_type == "message_delta":
            stop_reason = chunk.get("delta", {}).get("stop_reason")
            status = "completed"
            if stop_reason == "max_tokens":
                status = "incomplete"
            delta_usage = chunk.get("usage")
            if isinstance(delta_usage, dict):
                self._stream_state["anthropic_usage"].update(delta_usage)
            return {
                "type": "response.completed",
                "response": {
                    "status": status,
                    "usage": anthropic_to_openai_response(self._stream_state.get("anthropic_usage")),
                },
            }
```

- [ ] **Step 10: 跑流式测试确认通过**

```bash
uv run pytest tests/converters/test_stream_sequences.py::test_anthropic_to_response_stream_emits_full_usage -v
```
Expected: pass。

- [ ] **Step 11: 跑全部 converters 测试套防回归**

```bash
uv run pytest tests/converters/ -v
```
Expected: 全部通过。注意 `_build_final_events` / `_release_pending_completed_event`（line 922/996）是 Chat→Response 路径用的，本任务**不动**——若有测试覆盖那条路径仍应通过。

- [ ] **Step 12: Commit**

```bash
git add converters/to_response.py tests/converters/test_stream_sequences.py
git commit -m "fix(converters): Anthropic->Response 非流式与流式 usage 含 cache tokens"
```

---

## Task 5: `to_anthropic.py` Chat→Anthropic 非流式 + 流式

**Files:**
- Modify: `converters/to_anthropic.py` line 362-367 (`_chat_response_to_anthropic` 非流式) 和 line 390-398 (`_chat_build_message_stop_events` 流式)
- Modify: `tests/converters/test_chat_to_anthropic.py`

- [ ] **Step 1: 写失败测试**

```python
# Append to tests/converters/test_chat_to_anthropic.py
def test_chat_to_anthropic_nonstream_translates_cached_tokens():
    from converters.to_anthropic import ToAnthropicConverter
    conv = ToAnthropicConverter()
    chat_resp = {
        "id": "chatcmpl-xxx",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "total_tokens": 1050,
            "prompt_tokens_details": {"cached_tokens": 900},
        },
    }
    result = conv.convert_response(chat_resp, source_type="openai-chat-completions")
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["output_tokens"] == 50
    assert result["usage"]["cache_read_input_tokens"] == 900
    assert result["usage"]["cache_creation_input_tokens"] == 0
```

- [ ] **Step 2: 跑确认失败**

```bash
uv run pytest tests/converters/test_chat_to_anthropic.py::test_chat_to_anthropic_nonstream_translates_cached_tokens -v
```
Expected: `input_tokens` 当前为 1000（漏掉减去 cached），`cache_read_input_tokens` 为 0（因从不存在字段读）。

- [ ] **Step 3: 改 `to_anthropic.py:362-367`**

替换：
```python
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
                "cache_creation_input_tokens": data.get("usage", {}).get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": data.get("usage", {}).get("cache_read_input_tokens", 0),
            },
```
为：
```python
            "usage": openai_chat_to_anthropic(data.get("usage")),
```

并在文件顶部 import 区加：
```python
from converters.usage import openai_chat_to_anthropic, openai_response_to_anthropic
```

- [ ] **Step 4: 写流式失败测试**

```python
def test_chat_to_anthropic_stream_cache_read_from_cached_tokens():
    from converters.to_anthropic import ToAnthropicConverter
    conv = ToAnthropicConverter()
    # 模拟一个带 cached_tokens 的最终 usage chunk
    chunks = [
        {"id": "chatcmpl-a", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}]},
        {"id": "chatcmpl-a", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 900}}},
    ]
    events: list[tuple[str, dict]] = []
    for c in chunks:
        out = conv._chat_stream_chunk_to_anthropic(c)
        events.extend(out)
    delta_events = [e for e in events if e[0] == "message_delta"]
    assert delta_events, "expected message_delta"
    md = delta_events[-1][1]
    assert md["usage"]["cache_read_input_tokens"] == 900
    assert md["usage"]["cache_creation_input_tokens"] == 0
```

- [ ] **Step 5: 跑确认失败**

```bash
uv run pytest tests/converters/test_chat_to_anthropic.py::test_chat_to_anthropic_stream_cache_read_from_cached_tokens -v
```
Expected: `cache_read_input_tokens == 0`（当前从不存在字段读）。

- [ ] **Step 6: 改流式 `_chat_build_message_stop_events` (line 390-401)**

替换里面 message_delta 的 usage 构造段。但要保留 `usage_output = cumulative - prev`（增量计算 output）逻辑。改为：

```python
        events: list[tuple[str, dict[str, Any]]] = [
            ("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {
                    "output_tokens": usage_output,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": (
                        (usage or {}).get("prompt_tokens_details", {}).get("cached_tokens", 0)
                    ),
                },
            }),
            ("message_stop", {"type": "message_stop"}),
        ]
```

> 这里不调 `openai_chat_to_anthropic` 全量函数是因为 `output_tokens` 在流式里走的是「增量」语义（`cumulative - prev`），而工具函数返回的是绝对值。只复用 cache_read 的映射逻辑（即 `cached_tokens` → `cache_read_input_tokens`）。

- [ ] **Step 7: 跑全部 chat_to_anthropic 测试**

```bash
uv run pytest tests/converters/test_chat_to_anthropic.py -v
```
Expected: 全部 pass，包括新加 2 条。

- [ ] **Step 8: Commit**

```bash
git add converters/to_anthropic.py tests/converters/test_chat_to_anthropic.py
git commit -m "fix(converters): Chat->Anthropic usage 用 prompt_tokens_details.cached_tokens"
```

---

## Task 6: `to_anthropic.py` Response→Anthropic 非流式 + 流式

**Files:**
- Modify: `converters/to_anthropic.py` line 668-675 (`_response_response_to_anthropic` 非流式) 和 line 834-844 (Response 流式 message_delta)
- Modify: `tests/converters/test_response_to_chat.py` 或新增 `tests/converters/test_response_to_anthropic.py`

> 探查：`grep -rn "response_response_to_anthropic\|Response.*Anthropic" tests/converters/`

- [ ] **Step 1: 写失败测试（放到能跑的现有文件，比如 `test_response_to_chat.py` 末尾或新建文件）**

```python
def test_response_to_anthropic_nonstream_uses_input_tokens_details():
    from converters.to_anthropic import ToAnthropicConverter
    conv = ToAnthropicConverter()
    resp = {
        "id": "resp_xxx",
        "object": "response",
        "model": "gpt-4o",
        "status": "completed",
        "output": [{"type": "message", "id": "msg_a", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}]}],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_tokens_details": {"cached_tokens": 900},
        },
    }
    result = conv.convert_response(resp, source_type="openai-response")
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["output_tokens"] == 50
    assert result["usage"]["cache_read_input_tokens"] == 900
    assert result["usage"]["cache_creation_input_tokens"] == 0
```

- [ ] **Step 2: 跑确认失败**

```bash
uv run pytest -k "response_to_anthropic_nonstream_uses_input_tokens_details" -v
```
Expected: 当前实现读不存在的 `cache_read_input_tokens` 字段，得到 0；`input_tokens` 没减去 cached，得到 1000。

- [ ] **Step 3: 改 `to_anthropic.py:668-675`**

把：
```python
            "usage": {
                "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("usage", {}).get("output_tokens", 0),
                "cache_creation_input_tokens": data.get("usage", {}).get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": data.get("usage", {}).get("cache_read_input_tokens", 0),
            },
```
替换为：
```python
            "usage": openai_response_to_anthropic(data.get("usage")),
```

- [ ] **Step 4: 写流式失败测试**

```python
def test_response_to_anthropic_stream_uses_input_tokens_details():
    from converters.to_anthropic import ToAnthropicConverter
    conv = ToAnthropicConverter()
    chunks = [
        {"type": "response.created", "response": {"id": "resp_a", "model": "gpt-4o", "status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "response.completed", "response": {
            "id": "resp_a", "status": "completed", "output": [
                {"type": "message", "id": "msg_a", "status": "completed", "role": "assistant",
                 "content": [{"type": "output_text", "text": "hi"}]}
            ],
            "usage": {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 900}},
        }},
    ]
    all_events: list[tuple[str, dict]] = []
    for c in chunks:
        out = conv._response_stream_chunk_to_anthropic(c)
        all_events.extend(out)
    delta_events = [e for e in all_events if e[0] == "message_delta"]
    assert delta_events, "expected message_delta"
    md = delta_events[-1][1]
    assert md["usage"]["cache_read_input_tokens"] == 900
    assert md["usage"]["cache_creation_input_tokens"] == 0
    assert md["usage"]["output_tokens"] == 50
```

- [ ] **Step 5: 跑确认失败**

```bash
uv run pytest -k "response_to_anthropic_stream_uses_input_tokens_details" -v
```
Expected: `cache_read_input_tokens == 0`（当前实现从不存在的 `cache_read_input_tokens` 字段读，得 0）。

- [ ] **Step 6: 改流式 line 834-844**

把：
```python
            usage_output = resp.get("usage", {}).get("output_tokens", 0)
            events.append(
                ("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        "output_tokens": usage_output,
                        "cache_creation_input_tokens": resp.get("usage", {}).get("cache_creation_input_tokens", 0),
                        "cache_read_input_tokens": resp.get("usage", {}).get("cache_read_input_tokens", 0),
                    },
                })
            )
```
替换为：
```python
            resp_usage = resp.get("usage") or {}
            usage_output = resp_usage.get("output_tokens", 0)
            cached = (resp_usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
            events.append(
                ("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        "output_tokens": usage_output,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": cached,
                    },
                })
            )
```

- [ ] **Step 7: 跑全部 response_to_anthropic 测试**

```bash
uv run pytest -k "response_to_anthropic" -v
```
Expected: pass。

- [ ] **Step 8: 跑整个 converters 测试套**

```bash
uv run pytest tests/converters/ -v
```
Expected: 全部通过。

- [ ] **Step 9: Commit**

```bash
git add converters/to_anthropic.py tests/converters/
git commit -m "fix(converters): Response->Anthropic usage 用 input_tokens_details.cached_tokens"
```

---

## Task 7: `proxy_core.py` — `_build_openai_stream_response` 优先用上游 total + 透传 details；plumb include_usage

**Files:**
- Modify: `proxy_core.py` line 263-365 (`_build_openai_stream_response`) 和 line 784（`_do_request` 创建 converter 后插入 plumbing）
- Modify: `tests/test_proxy_core.py`

- [ ] **Step 1: 写失败测试 1：`_build_openai_stream_response` 透传 details**

```python
# Append to tests/test_proxy_core.py 合适位置
def test_build_openai_stream_response_preserves_token_details():
    from proxy_core import _build_openai_stream_response
    chunks = [
        {"id": "chatcmpl-x", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}]},
        {"id": "chatcmpl-x", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {
             "prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
             "prompt_tokens_details": {"cached_tokens": 900},
             "completion_tokens_details": {"reasoning_tokens": 30},
         }},
    ]
    result = _build_openai_stream_response(chunks, "gpt-4o")
    assert result["usage"]["prompt_tokens"] == 1000
    assert result["usage"]["completion_tokens"] == 50
    assert result["usage"]["total_tokens"] == 1050  # 用上游的 total
    assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 900
    assert result["usage"]["completion_tokens_details"]["reasoning_tokens"] == 30
```

- [ ] **Step 2: 跑确认失败**

```bash
uv run pytest tests/test_proxy_core.py::test_build_openai_stream_response_preserves_token_details -v
```
Expected: KeyError on `prompt_tokens_details` 或 `total_tokens` 不等（当前实现 self-sum）。

- [ ] **Step 3: 改 `_build_openai_stream_response` (line 263-366)**

把：
```python
    input_tokens = 0
    output_tokens = 0
    ...
        usage = chunk.get("usage")
        if usage:
            input_tokens = usage.get("prompt_tokens", input_tokens)
            output_tokens = usage.get("completion_tokens", output_tokens)
    ...
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
```

改为：
```python
    input_tokens = 0
    output_tokens = 0
    total_tokens: int | None = None
    prompt_details: dict | None = None
    completion_details: dict | None = None
    ...
        usage = chunk.get("usage")
        if usage:
            input_tokens = usage.get("prompt_tokens", input_tokens)
            output_tokens = usage.get("completion_tokens", output_tokens)
            if usage.get("total_tokens") is not None:
                total_tokens = usage["total_tokens"]
            pd = usage.get("prompt_tokens_details")
            if isinstance(pd, dict):
                prompt_details = pd
            cd = usage.get("completion_tokens_details")
            if isinstance(cd, dict):
                completion_details = cd
    ...
    final_usage = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens if total_tokens is not None else input_tokens + output_tokens,
    }
    if prompt_details is not None:
        final_usage["prompt_tokens_details"] = prompt_details
    if completion_details is not None:
        final_usage["completion_tokens_details"] = completion_details
    ...
        "usage": final_usage,
```

> 注意：保留 `input_tokens` / `output_tokens` 这两个本地变量名（不要重命名为 prompt/completion），避免误伤其他读这两个变量的语句。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/test_proxy_core.py::test_build_openai_stream_response_preserves_token_details -v
```
Expected: pass。

- [ ] **Step 5: 写失败测试 2：`include_usage` 透传**

```python
def test_do_request_sets_include_usage_on_chat_converter(monkeypatch):
    """请求体携带 stream_options.include_usage=true 时，response_converter 应被设置该 flag。"""
    from converters.to_chat import ToChatCompletionsConverter
    captured = {}

    real_setter = ToChatCompletionsConverter.set_stream_include_usage

    def spy(self, flag):
        captured["flag"] = flag
        return real_setter(self, flag)

    monkeypatch.setattr(ToChatCompletionsConverter, "set_stream_include_usage", spy)

    # 触发一次 Anthropic 渠道 + OpenAI Chat 客户端的流式调用即可。
    # 具体最简调用方式参考 tests/test_proxy_core.py 已有的 _do_request mock 套路。
    # ……（用现有测试基础设施构造一次 is_stream=True 的请求，request_data 中含 stream_options.include_usage=True）……
    # 断言：
    assert captured["flag"] is True
```

> 该测试可能需要参照 `tests/test_proxy_core.py` 中已有的 `_do_request` 调用样板（如 `test_same_type_openai_stream_does_not_inject_stream_options` 附近）写法。若工作量大，简化为直接构造 `ToChatCompletionsConverter` 实例后断言 `set_stream_include_usage(True)` 后 `_stream_include_usage == True`——但这只测了 setter，没测 plumbing。**建议先写完整端到端版本**。

- [ ] **Step 6: 跑确认失败**

```bash
uv run pytest tests/test_proxy_core.py::test_do_request_sets_include_usage_on_chat_converter -v
```
Expected: `flag` 没被设置（KeyError on `captured["flag"]`）。

- [ ] **Step 7: 在 `proxy_core.py:_do_request` 添加 plumbing**

定位 line 784 `request_converter, response_converter, source_type = _get_converter_and_upstream_type(...)`，在该行之后插入：

```python
    # 透传 OpenAI Chat 客户端的 stream_options.include_usage 到 response_converter
    if is_stream and isinstance(response_converter, ToChatCompletionsConverter):
        include_usage = bool((request_data.get("stream_options") or {}).get("include_usage", False))
        response_converter.set_stream_include_usage(include_usage)
```

- [ ] **Step 8: 跑测试确认通过**

```bash
uv run pytest tests/test_proxy_core.py::test_do_request_sets_include_usage_on_chat_converter -v
```
Expected: pass。

- [ ] **Step 9: 跑 proxy_core 全部测试**

```bash
uv run pytest tests/test_proxy_core.py -v
```
Expected: 全部通过。

- [ ] **Step 10: Commit**

```bash
git add proxy_core.py tests/test_proxy_core.py
git commit -m "feat(proxy): preserve token details in stream aggregator + plumb include_usage"
```

---

## Task 8: 全量回归 + ruff

- [ ] **Step 1: 跑全部测试**

```bash
uv run pytest
```
Expected: 0 failed, 0 errors。

- [ ] **Step 2: ruff**

```bash
uv run ruff check .
```
Expected: 无 issue。

- [ ] **Step 3: 抽查一个有意义的端到端场景手测**

启动服务（避免端口释放问题，用 no-reload）：
```bash
uv run python main.py --no-reload
```

发一个带 cache 的 Anthropic 请求到 OpenAI Chat 渠道（或反之），核对返回 usage 含 cache 字段、`prompt_tokens` 是合计后的值。

也可直接通过现有 mock_server + 端到端测试验证：
```bash
uv run pytest tests/test_e2e.py -v
```

- [ ] **Step 4: 更新 REVIEW1.md（划掉已修项）**

把 REVIEW1.md 中这一行：
```
- `[conv]` `total_tokens` 自己累加，**忽略 cached/reasoning tokens**，计费偏低
```
改为：
```
- ~~`[conv]` `total_tokens` 自己累加，**忽略 cached/reasoning tokens**，计费偏低~~ ✅ 修复于 2026-05-19
```

- [ ] **Step 5: Final commit**

```bash
git add REVIEW1.md
git commit -m "docs(review): mark token usage cache-aware fix as done"
```

---

## Risks & Notes

1. **Anthropic→Response 流式路径**（Task 4 Step 5）需要先探查清楚——`to_response.py` 中如果不存在 Anthropic 上游流式专用入口（而是借道 Chat→Response），Step 6-7 可跳过；该方向流式经过 Chat 中间层会自动受益于 Task 3 的修复。优先确认事实，再决定是否要单独改。

2. **`_chat_build_message_stop_events`（Task 5 Step 4）** 中 `output_tokens` 用的是 `cumulative - prev` 增量语义，而工具函数 `openai_chat_to_anthropic` 返回的是绝对值。因此那里**没有完全调用工具函数**，只复用了 cache_read 字段的映射逻辑——这是有意为之，不要硬塞工具函数进去。

3. **`include_usage` plumbing 的边界**：只在 `response_converter` 是 `ToChatCompletionsConverter` 实例时调 setter。其他 converter 类型（如 `ToResponseConverter`）即使客户端传了 `stream_options.include_usage`，按 OpenAI 官方语义那个字段也是 chat completions 专属，不需要处理。
