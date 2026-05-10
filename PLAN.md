# Chat 流式 Usage Chunk 转 Responses 修复计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 修复客户端以 OpenAI Responses 格式请求、上游为 Chat Completions 流式响应时，finish chunk 之后到达的 usage-only chunk 被忽略，导致 `response.completed.usage` 为 0 的问题。

**架构：** 修复点限定在 `ToResponseConverter` 的 Chat Completions 流式转换状态机。转换器在收到 `finish_reason` 时先缓存结束原因和待完成事件，不立即发 `response.completed`；若随后收到 `choices: []` 的 usage-only chunk，则更新 usage 后再释放完成事件；若上游直接 `[DONE]` 或没有 usage-only chunk，则由 `finalize_stream()` 使用缓存的结束原因补齐完成事件。

**技术栈：** Python、pytest、现有 converter 单元测试。

---

## 文件结构

- 修改：`tests/converters/test_stream_sequences.py`
  - 增加 Chat Completions 流式 `finish_reason` 后 usage-only chunk 的回归测试。
  - 增加没有 usage-only chunk 时 `finalize_stream()` 仍输出 `response.completed` 的兜底测试。
- 修改：`converters/to_response.py`
  - 在 `_stream_state` 中加入延迟完成所需字段。
  - 调整 `_chat_stream_chunk_to_response()` 对 finish chunk、usage-only chunk 的处理。
  - 调整 `finalize_stream()` 在 `[DONE]` 收尾时释放缓存完成事件。

---

### 任务 1：为 finish 后 usage-only chunk 编写失败测试

**文件：**
- 修改：`tests/converters/test_stream_sequences.py`

- [x] **步骤 1：添加回归测试**

在 `TestChatToResponseStream` 中添加：

```python
def test_usage_only_chunk_after_finish_updates_response_completed_usage(self):
    """finish_reason 后的 usage-only chunk 应进入 response.completed.usage。"""
    converter = ToResponseConverter()
    events = [
        {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {"content": "Hi"}}]},
        {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        {
            "id": "chatcmpl_1",
            "model": "gpt-4o",
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        },
    ]

    outputs = []
    for evt in events:
        result = converter.convert_stream_chunk(evt, "openai-chat-completions")
        if result is not None:
            outputs.append(result)
            outputs.extend(converter.get_extra_events(result or {}))
    outputs.extend(converter.finalize_stream("openai-chat-completions"))

    completed_events = [o for o in outputs if isinstance(o, dict) and o.get("type") == "response.completed"]
    assert len(completed_events) == 1
    assert completed_events[0]["response"]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 2,
        "total_tokens": 9,
    }
```

- [x] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/converters/test_stream_sequences.py::TestChatToResponseStream::test_usage_only_chunk_after_finish_updates_response_completed_usage -q
```

预期：FAIL，断言显示实际 usage 为 0。

---

### 任务 2：实现延迟完成并消费 usage-only chunk

**文件：**
- 修改：`converters/to_response.py`
- 测试：`tests/converters/test_stream_sequences.py`

- [x] **步骤 1：扩展流式状态字段**

在 `_reset_stream_state()` 的状态字典中加入：

```python
"pending_finish_reason": None,
"pending_final_events": [],
"waiting_for_usage_after_finish": False,
```

- [x] **步骤 2：添加延迟完成辅助方法**

在 `get_extra_events()` 前添加：

```python
def _queue_final_events_for_finish(self, finish_reason: str) -> list[dict[str, Any]]:
    final_events = self._build_final_events(finish_reason=finish_reason, mark_completed=False)
    self._stream_state["pending_finish_reason"] = finish_reason
    self._stream_state["pending_final_events"] = final_events
    self._stream_state["waiting_for_usage_after_finish"] = True
    return final_events[:-1]

def _release_pending_completed_event(self) -> list[dict[str, Any]]:
    pending = self._stream_state.get("pending_final_events") or []
    if not pending:
        return []
    completed = pending[-1]
    if completed.get("type") == "response.completed":
        completed["response"]["usage"] = {
            "input_tokens": self._stream_state["input_tokens"],
            "output_tokens": self._stream_state["output_tokens"],
            "total_tokens": self._stream_state["total_tokens"],
        }
    self._stream_state["pending_final_events"] = []
    self._stream_state["waiting_for_usage_after_finish"] = False
    self._stream_state["completed_sent"] = True
    return [completed]
```

- [x] **步骤 3：调整 finish chunk 分支**

将 `_chat_stream_chunk_to_response()` 中 `finish_reason is not None` 分支改为调用 `_queue_final_events_for_finish(finish_reason)`，只返回 `response.output_text.done`、`response.content_part.done`、`response.output_item.done`、`response.function_call_arguments.done` 等完成项，暂不返回 `response.completed`。

- [x] **步骤 4：调整 choices 为空分支**

在 `choices = chunk.get("choices", [])` 后，`if not choices:` 分支中，如果 `waiting_for_usage_after_finish` 为真，则调用 `_release_pending_completed_event()` 并返回其中的 `response.completed`；否则保持返回 `None`。

- [x] **步骤 5：调整 finalize_stream()**

在 `finalize_stream("openai-chat-completions")` 中，如果已有 `pending_final_events`，直接释放 `_release_pending_completed_event()`；否则沿用原来的 `_build_final_events(finish_reason="stop")`。

- [x] **步骤 6：让 `_build_final_events()` 支持延迟标记 completed**

把签名改为：

```python
def _build_final_events(self, finish_reason: str, mark_completed: bool = True) -> list[dict[str, Any]]:
```

并将末尾的：

```python
self._stream_state["completed_sent"] = True
```

改为：

```python
if mark_completed:
    self._stream_state["completed_sent"] = True
```

- [x] **步骤 7：运行红灯测试验证通过**

运行：

```bash
uv run pytest tests/converters/test_stream_sequences.py::TestChatToResponseStream::test_usage_only_chunk_after_finish_updates_response_completed_usage -q
```

预期：PASS。

---

### 任务 3：补齐无 usage-only chunk 的收尾兜底测试

**文件：**
- 修改：`tests/converters/test_stream_sequences.py`

- [x] **步骤 1：添加兜底测试**

在 `TestChatToResponseStream` 中添加：

```python
def test_finish_without_usage_only_chunk_finalizes_completed_on_done(self):
    """没有 finish 后 usage-only chunk 时，finalize_stream 应释放 response.completed。"""
    converter = ToResponseConverter()
    events = [
        {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {"content": "Hi"}}]},
        {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    outputs = []
    for evt in events:
        result = converter.convert_stream_chunk(evt, "openai-chat-completions")
        if result is not None:
            outputs.append(result)
            outputs.extend(converter.get_extra_events(result or {}))
    assert not [o for o in outputs if isinstance(o, dict) and o.get("type") == "response.completed"]

    outputs.extend(converter.finalize_stream("openai-chat-completions"))

    completed_events = [o for o in outputs if isinstance(o, dict) and o.get("type") == "response.completed"]
    assert len(completed_events) == 1
    assert completed_events[0]["response"]["output"][0]["content"][0]["text"] == "Hi"
```

- [x] **步骤 2：运行新增测试**

运行：

```bash
uv run pytest tests/converters/test_stream_sequences.py::TestChatToResponseStream::test_finish_without_usage_only_chunk_finalizes_completed_on_done -q
```

预期：PASS。

---

### 任务 4：运行相关测试集和代码检查

**文件：**
- 验证：`tests/converters/test_stream_sequences.py`
- 验证：`tests/converters/test_response_to_chat.py`
- 验证：`tests/test_responses_full_flow.py`
- 验证：`tests/test_proxy_core_responses.py`

- [x] **步骤 1：运行转换和 Responses 相关测试**

运行：

```bash
uv run pytest tests/converters/test_stream_sequences.py tests/converters/test_response_to_chat.py tests/test_responses_full_flow.py tests/test_proxy_core_responses.py -q
```

预期：全部 PASS。

- [x] **步骤 2：运行 ruff 检查**

运行：

```bash
uv run ruff check converters/to_response.py tests/converters/test_stream_sequences.py
```

预期：退出码 0，无 lint 错误。

---

## 自检清单

- [x] 回归测试先失败，证明覆盖原始 bug。
- [x] usage-only chunk 到达后，`response.completed.usage` 使用最新 usage。
- [x] 没有 usage-only chunk 时，`finalize_stream()` 仍输出唯一的 `response.completed`。
- [x] 已有文本流、工具调用流、混合流事件顺序测试仍通过。
- [x] 非流式转换不受影响。
