# Converter 完整性审计与修复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [x]`）语法来跟踪进度。

**目标：** 补全 proxy_core 路由、修复字段映射和流式转换缺陷、建立字段级对照表驱动的测试套件。

**架构：** 将 `_get_converter_and_upstream_type()` 的硬编码分支替换为查表逻辑；按 API 规范补齐缺失字段映射；修复流式状态机缺陷；为每个转换方向编写字段级对照表测试。

**技术栈：** Python 3.14, pytest, ruff

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `proxy_core.py:101-139` | `_get_converter_and_upstream_type()` 函数，替换为查表逻辑 |
| `converters/to_anthropic.py` | Chat→Anthropic 转换：补全 system 数组、tool_choice、thinking、image_url 校验 |
| `converters/to_chat.py` | Anthropic→Chat 转换：补全 thinking→reasoning_effort、tool_choice、响应 thinking→reasoning_content |
| `converters/to_response.py` | Chat→Response 流式：修复 output_index 硬编码 |
| `tests/converters/conftest.py` | 共享 fixture，提供官方 API 规范示例数据 |
| `tests/converters/test_anthropic_to_chat.py` | Anthropic→Chat 单元测试（最高优先级） |
| `tests/converters/test_chat_to_anthropic.py` | Chat→Anthropic 单元测试 |
| `tests/converters/test_stream_sequences.py` | 流式序列测试 |

---

## 阶段 1：路由补全（P0 #1-3）

### 任务 1：替换 `_get_converter_and_upstream_type` 为查表逻辑

**文件：**
- 修改：`proxy_core.py:101-139`
- 测试：`tests/converters/test_converter_matrix.py`（新增路由测试）

- [x] **步骤 1：编写路由测试**

在 `tests/converters/test_converter_matrix.py` 末尾添加：

```python
class TestConverterRouting:
    """测试 proxy_core 路由表覆盖所有转换方向"""

    def test_all_conversion_directions(self):
        """验证 CONVERTER_MAP 包含所有 6 个非直通组合"""
        from proxy_core import CONVERTER_MAP
        expected = {
            ("openai-chat-completions", "anthropic"),
            ("openai-response", "anthropic"),
            ("openai-response", "openai-chat-completions"),
            ("anthropic", "openai-chat-completions"),
            ("anthropic", "openai-response"),
            ("openai-chat-completions", "openai-response"),
        }
        assert set(CONVERTER_MAP.keys()) == expected

    def test_passthrough_same_type(self):
        """同格式应返回 None, None"""
        from proxy_core import _get_converter_and_upstream_type
        from models.channel import Channel
        from models.api_types import APIType

        channel = Channel(
            name="test", api_type=APIType.OPENAI_CHAT,
            base_url="http://test", api_key="test", models=["gpt-4o"]
        )
        req, resp, src = _get_converter_and_upstream_type(channel, APIType.OPENAI_CHAT)
        assert req is None
        assert resp is None
        assert src == "openai-chat-completions"
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_converter_matrix.py::TestConverterRouting -v`
预期：FAIL，`CONVERTER_MAP` 未定义

- [x] **步骤 3：实现查表逻辑**

修改 `proxy_core.py:101-139`，替换为：

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


def _get_converter_and_upstream_type(
    channel: Channel, target_api_type: APIType
) -> tuple:
    """根据渠道类型和目标API类型，获取转换器和上游请求类型

    返回 (request_converter, response_converter, source_type)
    - request_converter: 用于把客户端格式转换为上游格式
    - response_converter: 用于把上游格式转换为客户端格式
    """
    source = channel.api_type.value
    target = target_api_type.value

    if source == target:
        return None, None, source

    converters = CONVERTER_MAP.get((source, target))
    if converters is None:
        raise ValueError(f"不支持的转换方向: {source} -> {target}")

    req_cls, resp_cls = converters
    return req_cls(), resp_cls(), source
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_converter_matrix.py::TestConverterRouting -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add proxy_core.py tests/converters/test_converter_matrix.py
git commit -m "feat: replace hardcoded converter routing with lookup table"
```

---

## 阶段 2：字段补全（P1 #4-8）

### 任务 2：修复 Chat→Anthropic 多条 system 消息丢失（P1 #5）

**文件：**
- 修改：`converters/to_anthropic.py:80-86, 141-142`
- 测试：`tests/converters/test_chat_to_anthropic.py`

- [x] **步骤 1：编写失败测试**

创建 `tests/converters/test_chat_to_anthropic.py`：

```python
from converters.to_anthropic import ToAnthropicConverter
from models.api_types import APIType


class TestChatToAnthropic:
    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_multiple_system_messages(self):
        """多条 system 消息应合并为 Anthropic system 数组"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "system", "content": "Always be concise"},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert isinstance(result["system"], list)
        assert len(result["system"]) == 2
        assert result["system"][0]["type"] == "text"
        assert result["system"][0]["text"] == "You are helpful"
        assert result["system"][1]["text"] == "Always be concise"
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_chat_to_anthropic.py::TestChatToAnthropic::test_multiple_system_messages -v`
预期：FAIL，`system` 是字符串而非数组

- [x] **步骤 3：修复 system 消息处理**

修改 `converters/to_anthropic.py:80-86`：

```python
        system = None
        messages = []
        for msg in data.get("messages", []):
            role = msg.get("role", "user")
            if role == "system":
                if system is None:
                    system = []
                content = msg.get("content", "")
                if isinstance(content, str):
                    system.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            system.append(item)
                        elif isinstance(item, str):
                            system.append({"type": "text", "text": item})
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_chat_to_anthropic.py::TestChatToAnthropic::test_multiple_system_messages -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_anthropic.py tests/converters/test_chat_to_anthropic.py
git commit -m "fix: merge multiple system messages into Anthropic system array"
```

### 任务 3：修复 Anthropic→Chat thinking 字段丢失（P1 #4）

**文件：**
- 修改：`converters/to_chat.py:130-131`
- 测试：`tests/converters/test_anthropic_to_chat.py`

- [x] **步骤 1：编写失败测试**

创建 `tests/converters/test_anthropic_to_chat.py`：

```python
from converters.to_chat import ToChatCompletionsConverter
from models.api_types import APIType


class TestAnthropicToChat:
    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_thinking_enabled_to_reasoning_effort(self):
        """Anthropic thinking.enabled 应映射为 reasoning_effort"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 16000},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "reasoning_effort" in result
        assert result["reasoning_effort"] == 16000

    def test_thinking_adaptive_to_reasoning_effort_medium(self):
        """Anthropic thinking.adaptive 应映射为 reasoning_effort=medium"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "thinking": {"type": "adaptive"},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert "reasoning_effort" in result
        assert result["reasoning_effort"] == "medium"
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_anthropic_to_chat.py -v`
预期：FAIL，`reasoning_effort` 不存在

- [x] **步骤 3：修复 thinking 映射**

修改 `converters/to_chat.py:130-131`，将注释掉的代码替换为：

```python
        # thinking 映射为 reasoning_effort
        thinking = data.get("thinking")
        if thinking:
            if isinstance(thinking, dict):
                if thinking.get("type") == "enabled":
                    budget = thinking.get("budget_tokens", 0)
                    result["reasoning_effort"] = budget
                elif thinking.get("type") == "adaptive":
                    result["reasoning_effort"] = "medium"
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_anthropic_to_chat.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_chat.py tests/converters/test_anthropic_to_chat.py
git commit -m "fix: map Anthropic thinking to OpenAI reasoning_effort"
```

### 任务 4：修复 tool_choice 映射不完整（P1 #6-8）

**文件：**
- 修改：`converters/to_chat.py:121-127`、`converters/to_anthropic.py:151-163`
- 测试：`tests/converters/test_anthropic_to_chat.py`、`tests/converters/test_chat_to_anthropic.py`

- [x] **步骤 1：编写失败测试**

在 `tests/converters/test_anthropic_to_chat.py` 添加：

```python
    def test_tool_choice_any_to_required(self):
        """Anthropic tool_choice.any -> OpenAI required"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": {"type": "any"},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["tool_choice"] == "required"

    def test_tool_choice_none(self):
        """Anthropic tool_choice.none -> OpenAI none"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": {"type": "none"},
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        assert result["tool_choice"] == "none"
```

在 `tests/converters/test_chat_to_anthropic.py` 添加：

```python
    def test_tool_choice_required_to_any(self):
        """OpenAI tool_choice.required -> Anthropic any"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": "required",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tool_choice"]["type"] == "any"
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_anthropic_to_chat.py tests/converters/test_chat_to_anthropic.py -v`
预期：FAIL，`none` 未处理，`required` 未处理

- [x] **步骤 3：修复 tool_choice 映射**

修改 `converters/to_chat.py:121-127`：

```python
        if data.get("tool_choice"):
            tc = data["tool_choice"]
            if isinstance(tc, dict):
                if tc.get("type") == "auto":
                    result["tool_choice"] = "auto"
                elif tc.get("type") == "any":
                    result["tool_choice"] = "required"
                elif tc.get("type") == "none":
                    result["tool_choice"] = "none"
                elif tc.get("type") == "tool":
                    result["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
```

修改 `converters/to_anthropic.py:151-163`（已有 `required` 处理，但确认完整）：

```python
        if data.get("tool_choice"):
            tc = data["tool_choice"]
            if isinstance(tc, dict):
                if tc.get("type") == "auto":
                    result["tool_choice"] = {"type": "auto"}
                elif tc.get("type") == "required":
                    result["tool_choice"] = {"type": "any"}
                elif tc.get("type") == "function":
                    result["tool_choice"] = {"type": "tool", "name": tc.get("function", {}).get("name", "")}
            elif tc == "auto":
                result["tool_choice"] = {"type": "auto"}
            elif tc == "required":
                result["tool_choice"] = {"type": "any"}
            elif tc == "none":
                result["tool_choice"] = {"type": "none"}
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_anthropic_to_chat.py tests/converters/test_chat_to_anthropic.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_chat.py converters/to_anthropic.py tests/converters/
git commit -m "fix: complete tool_choice mapping for all directions"
```

---

## 阶段 3：流式修复（P2 #9-11）

### 任务 5：修复 signature_delta 缺失（P2 #9）

**文件：**
- 修改：`converters/to_anthropic.py:390-421`
- 测试：`tests/converters/test_stream_sequences.py`

- [x] **步骤 1：编写失败测试**

创建 `tests/converters/test_stream_sequences.py`：

```python
from converters.to_anthropic import ToAnthropicConverter


def feed_events(converter, events):
    """辅助函数：逐 chunk 输入并收集全部输出"""
    outputs = []
    for evt in events:
        result = converter.convert_stream_chunk(evt, "openai-chat-completions")
        if result is not None:
            outputs.append((converter.get_stream_event_type(evt, "openai-chat-completions"), result))
        extra = converter.get_extra_events(result or {})
        for extra_evt in extra:
            if isinstance(extra_evt, tuple) and len(extra_evt) == 2:
                outputs.append(extra_evt)
    return outputs


class TestChatToAnthropicStream:
    def test_thinking_stream_with_signature_delta(self):
        """OpenAI reasoning_content 流应生成 thinking_delta + signature_delta"""
        converter = ToAnthropicConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "Let me think..."}}]},
            {"choices": [{"delta": {"content": "The answer is 42"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = feed_events(converter, events)
        event_types = [et for et, _ in outputs]
        assert "content_block_start" in event_types
        assert "thinking_delta" in [d.get("delta", {}).get("type") for _, d in outputs]
        # signature_delta 应在 thinking block 结束前出现
        assert "signature_delta" in [d.get("delta", {}).get("type") for _, d in outputs]
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_stream_sequences.py::TestChatToAnthropicStream::test_thinking_stream_with_signature_delta -v`
预期：FAIL，无 `signature_delta`

- [x] **步骤 3：修复 signature_delta**

修改 `converters/to_anthropic.py:390-421`，在 `finish_reason is not None` 分支中，关闭 thinking content_block 前插入 signature_delta：

```python
        if finish_reason is not None:
            self._ensure_message_started(chunk, events)
            if self._stream_state["content_block_started"]:
                # 如果当前是 thinking block，先发送 signature_delta
                if self._stream_state["current_content_type"] == "thinking":
                    events.append(
                        ("content_block_delta", {
                            "type": "content_block_delta",
                            "index": self._stream_state["content_block_index"],
                            "delta": {"type": "signature_delta", "signature": ""},
                        })
                    )
                events.append(
                    ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                )
                self._stream_state["content_block_started"] = False
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_stream_sequences.py::TestChatToAnthropicStream::test_thinking_stream_with_signature_delta -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_anthropic.py tests/converters/test_stream_sequences.py
git commit -m "fix: add signature_delta before closing thinking content block"
```

### 任务 6：修复 message_start 中 usage 全为 0（P2 #10）

**文件：**
- 修改：`converters/to_anthropic.py:30-54`
- 测试：`tests/converters/test_stream_sequences.py`

- [x] **步骤 1：编写失败测试**

在 `tests/converters/test_stream_sequences.py` 添加：

```python
    def test_message_start_with_usage(self):
        """message_start 应包含 input_tokens"""
        converter = ToAnthropicConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}], "usage": {"prompt_tokens": 42}},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = feed_events(converter, events)
        msg_start = [d for et, d in outputs if et == "message_start"][0]
        assert msg_start["message"]["usage"]["input_tokens"] == 42
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_stream_sequences.py::TestChatToAnthropicStream::test_message_start_with_usage -v`
预期：FAIL，`input_tokens` 为 0

- [x] **步骤 3：修复 message_start usage**

修改 `converters/to_anthropic.py:30-54` 的 `_ensure_message_started`：

```python
    def _ensure_message_started(self, chunk: dict[str, Any], events: list) -> None:
        """确保 message_start 已发出。如果尚未发出，在 events 列表头部插入。"""
        if not self._stream_state["started"]:
            self._stream_state["started"] = True
            # 从 chunk 中提取 usage（如果可用）
            usage = chunk.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            events.insert(0, (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": chunk.get("id", "").replace("chatcmpl-", "msg_"),
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": chunk.get("model", ""),
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                },
            ))
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_stream_sequences.py::TestChatToAnthropicStream::test_message_start_with_usage -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_anthropic.py tests/converters/test_stream_sequences.py
git commit -m "fix: populate input_tokens in message_start from chunk usage"
```

### 任务 7：修复 to_response output_index 硬编码（P2 #11）

**文件：**
- 修改：`converters/to_response.py:314-404`
- 测试：`tests/converters/test_stream_sequences.py`

- [x] **步骤 1：编写失败测试**

在 `tests/converters/test_stream_sequences.py` 添加：

```python
class TestChatToResponseStream:
    def test_multiple_tool_calls_output_index(self):
        """多个 tool_call 应有递增的 output_index"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "search"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "calc"}}]}}]},
            {"choices": [{"finish_reason": "tool_calls"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
            extra = converter.get_extra_events(result or {})
            outputs.extend(extra)

        added_events = [o for o in outputs if o.get("type") == "response.output_item.added"]
        assert len(added_events) == 2
        assert added_events[0]["output_index"] == 0
        assert added_events[1]["output_index"] == 1
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_stream_sequences.py::TestChatToResponseStream::test_multiple_tool_calls_output_index -v`
预期：FAIL，两个 `output_index` 都是 0

- [x] **步骤 3：修复 output_index**

修改 `converters/to_response.py:18-24` 的 `_reset_stream_state`：

```python
    def _reset_stream_state(self):
        self._stream_state = {
            "reasoning_started": False,
            "reasoning_id": "",
            "message_id": "",
            "output_index": 0,
        }
        self._pending_extra_events = []
```

修改 `converters/to_response.py:342-355`：

```python
        if delta.get("tool_calls"):
            events = []
            for tc in delta["tool_calls"]:
                if tc.get("function", {}).get("name"):
                    idx = self._stream_state["output_index"]
                    self._stream_state["output_index"] = idx + 1
                    events.append({
                        "type": "response.output_item.added",
                        "output_index": idx,
                        "item": {
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": tc["function"]["name"],
                            "arguments": "",
                        },
                    })
```

修改 `converters/to_response.py:370-388` 的 reasoning 部分（同样修复）：

```python
        if delta.get("reasoning_content") is not None:
            if not self._stream_state["reasoning_started"]:
                self._stream_state["reasoning_started"] = True
                self._stream_state["reasoning_id"] = f"rs_{chunk.get('id', '')}"
                idx = self._stream_state["output_index"]
                self._stream_state["output_index"] = idx + 1
                result = {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {
                        "type": "reasoning",
                        "id": self._stream_state["reasoning_id"],
                        "summary": [],
                    },
                }
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_stream_sequences.py::TestChatToResponseStream::test_multiple_tool_calls_output_index -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_response.py tests/converters/test_stream_sequences.py
git commit -m "fix: use incremental output_index for multiple tool_calls in stream"
```

---

## 阶段 4：健壮性（P3 #12-13）

### 任务 8：修复 JSON 解析失败静默回退（P3 #12）

**文件：**
- 修改：`converters/to_anthropic.py:100-105, 210-215, 446-451, 516-521`
- 测试：`tests/converters/test_chat_to_anthropic.py`

- [x] **步骤 1：编写失败测试**

在 `tests/converters/test_chat_to_anthropic.py` 添加：

```python
    def test_invalid_json_arguments_fallback(self):
        """无效 JSON 参数应回退为空 dict 但不崩溃"""
        request = {
            "model": "gpt-4o",
            "messages": [{
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "test", "arguments": "not valid json"},
                }],
            }],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assistant_msg = [m for m in result["messages"] if m["role"] == "assistant"][0]
        assert assistant_msg["content"][0]["input"] == {}
```

- [x] **步骤 2：运行测试确认通过**

运行：`uv run pytest tests/converters/test_chat_to_anthropic.py::TestChatToAnthropic::test_invalid_json_arguments_fallback -v`
预期：PASS（当前行为已经是回退为 {}，只是静默）

- [x] **步骤 3：添加 debug 日志**

修改 `converters/to_anthropic.py` 中 4 处 JSON 解析：

```python
import logging

logger = logging.getLogger(__name__)

# 每处修改为：
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        logger.debug("JSON decode error for arguments: %r", args)
                        args = {}
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_chat_to_anthropic.py::TestChatToAnthropic::test_invalid_json_arguments_fallback -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_anthropic.py tests/converters/test_chat_to_anthropic.py
git commit -m "fix: add debug logging for JSON decode errors instead of silent fallback"
```

### 任务 9：修复非 data: URL image_url 原样保留（P3 #13）

**文件：**
- 修改：`converters/to_anthropic.py:63-74`
- 测试：`tests/converters/test_chat_to_anthropic.py`

- [x] **步骤 1：编写失败测试**

在 `tests/converters/test_chat_to_anthropic.py` 添加：

```python
    def test_non_data_image_url_raises(self):
        """非 data: URL 的 image_url 应报错"""
        request = {
            "model": "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                ],
            }],
        }
        with pytest.raises(ValueError, match="Anthropic only supports base64-encoded images"):
            self.converter.convert_request(request, APIType.OPENAI_CHAT)
```

- [x] **步骤 2：运行测试确认失败**

运行：`uv run pytest tests/converters/test_chat_to_anthropic.py::TestChatToAnthropic::test_non_data_image_url_raises -v`
预期：FAIL，未报错

- [x] **步骤 3：修复 image_url 校验**

修改 `converters/to_anthropic.py:63-74`：

```python
                    elif item.get("type") == "image_url":
                        url = item.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            parts = url.split(",", 1)
                            media_type = parts[0].split(";")[0].split(":")[1] if parts else "image/png"
                            data = parts[1] if len(parts) > 1 else ""
                            result.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": data},
                            })
                        else:
                            raise ValueError(
                                f"Anthropic only supports base64-encoded images (data: URI). "
                                f"Got URL: {url[:50]}..."
                            )
```

- [x] **步骤 4：运行测试确认通过**

运行：`uv run pytest tests/converters/test_chat_to_anthropic.py::TestChatToAnthropic::test_non_data_image_url_raises -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add converters/to_anthropic.py tests/converters/test_chat_to_anthropic.py
git commit -m "fix: reject non-data URI image URLs for Anthropic with clear error"
```

---

## 阶段 5：测试补全

### 任务 10：补全 Anthropic→Chat 响应转换测试

**文件：**
- 测试：`tests/converters/test_anthropic_to_chat.py`

- [x] **步骤 1：编写响应转换测试**

在 `tests/converters/test_anthropic_to_chat.py` 添加：

```python
    def test_thinking_response_to_reasoning_content(self):
        """Anthropic thinking block -> OpenAI reasoning_content"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me analyze..."},
                {"type": "text", "text": "The answer is 42"},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        assert result["choices"][0]["message"]["reasoning_content"] == "Let me analyze..."
        assert result["choices"][0]["message"]["content"] == "The answer is 42"

    def test_tool_use_response(self):
        """Anthropic tool_use -> OpenAI tool_calls"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_001", "name": "search", "input": {"q": "test"}},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "search"
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"q": "test"}
```

- [x] **步骤 2：运行测试**

运行：`uv run pytest tests/converters/test_anthropic_to_chat.py -v`
预期：PASS

- [x] **步骤 3：Commit**

```bash
git add tests/converters/test_anthropic_to_chat.py
git commit -m "test: add Anthropic->Chat response conversion tests for thinking and tool_use"
```

### 任务 11：补全流式序列测试

**文件：**
- 测试：`tests/converters/test_stream_sequences.py`

- [x] **步骤 1：编写完整流式测试**

在 `tests/converters/test_stream_sequences.py` 添加：

```python
class TestAnthropicToChatStream:
    def test_text_stream(self):
        """Anthropic 文本流 -> OpenAI Chat 流"""
        converter = ToChatCompletionsConverter()
        events = [
            {"type": "message_start", "message": {"id": "msg_001", "model": "claude-opus-4-7"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)

        contents = [o["choices"][0]["delta"].get("content", "") for o in outputs]
        assert "Hello" in contents
        assert " world" in contents
        # 最后一条应有 finish_reason
        assert outputs[-1]["choices"][0]["finish_reason"] == "stop"

    def test_tool_use_stream(self):
        """Anthropic tool_use 流 -> OpenAI Chat tool_calls 流"""
        converter = ToChatCompletionsConverter()
        events = [
            {"type": "message_start", "message": {"id": "msg_001", "model": "claude-opus-4-7"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_001", "name": "search", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q": "test"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)

        # 应有 tool_calls 增量
        tool_call_events = [o for o in outputs if o["choices"][0]["delta"].get("tool_calls")]
        assert len(tool_call_events) >= 1
        assert outputs[-1]["choices"][0]["finish_reason"] == "tool_calls"
```

- [x] **步骤 2：运行测试**

运行：`uv run pytest tests/converters/test_stream_sequences.py -v`
预期：PASS

- [x] **步骤 3：Commit**

```bash
git add tests/converters/test_stream_sequences.py
git commit -m "test: add Anthropic->Chat stream sequence tests"
```

---

## 最终验证

- [x] **步骤 1：运行全部测试**

```bash
uv run pytest tests/converters/ -v
```
预期：所有测试通过

- [x] **步骤 2：运行 lint**

```bash
uv run ruff check .
```
预期：无错误

- [x] **步骤 3：Commit**

```bash
git add .
git commit -m "test: complete converter test suite with all directions and stream sequences"
```

---

## 自检

**1. 规格覆盖度：**
- [x] P0 #1-3 路由补全 → 任务 1
- [x] P1 #4 thinking→reasoning_effort → 任务 3
- [x] P1 #5 多条 system → 任务 2
- [x] P1 #6-8 tool_choice → 任务 4
- [x] P2 #9 signature_delta → 任务 5
- [x] P2 #10 message_start usage → 任务 6
- [x] P2 #11 output_index → 任务 7
- [x] P3 #12 JSON 回退日志 → 任务 8
- [x] P3 #13 image_url 校验 → 任务 9
- [x] 测试架构 → 任务 10-11

**2. 占位符扫描：** 无 TODO/TBD/待定/后续实现

**3. 类型一致性：** `CONVERTER_MAP` 键值对、`_stream_state` 字段名在各任务中一致
