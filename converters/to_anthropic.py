"""
将其他格式转换为 Anthropic Messages 格式

请求体解析走源语法解析模块（ADR-0016 D2 二期）：Chat 源见
``converters/parsing_chat``，Responses 源见 ``converters/parsing_responses``；
本模块只做渲染（中间条目 → Anthropic 语法）。finish_reason / tools / tool_choice
映射为"源语法 → 中间条目"（解析模块）与"中间条目 → 目标语法"（本模块渲染函数）
两段。
"""

from typing import Any

from loguru import logger

from converters.anthropic_stream_state import AnthropicStreamState
from converters.base import BaseConverter, safe_parse_tool_args
from converters.parsing_chat import parse_chat_finish_reason, parse_chat_messages, parse_chat_tool_choice, parse_chat_tools
from converters.parsing_responses import parse_responses_finish, parse_responses_input, parse_responses_tool_choice, parse_responses_tools
from converters.stream_usage import _responses_usage_output_final
from converters.usage import openai_chat_to_anthropic, openai_response_to_anthropic

# OpenAI reasoning_effort -> Anthropic thinking budget_tokens 的统一映射
_REASONING_EFFORT_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}

# 中间 finish 条目（Chat finish_reason 词表）→ Anthropic stop_reason。
# 单一住所：非流式响应转换与流式 message_delta 共用（原两份 stop_reason_map）。
_STOP_REASONS_BY_FINISH = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def render_finish_anthropic(finish_reason: str | None) -> str:
    """中间 finish 条目 → Anthropic stop_reason；未知值兜底 end_turn。"""
    return _STOP_REASONS_BY_FINISH.get(finish_reason, "end_turn")


def render_tool_choice_anthropic(tool_choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """中间 tool_choice 条目 → Anthropic tool_choice；无法渲染返回 None。"""
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "none":
        return {"type": "none"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = tool_choice.get("name", "")
        if not name:
            # name 为空时构造 {"type":"tool","name":""} 会被
            # Anthropic 拒绝；退化为不指定 tool_choice
            # （让上游用默认 auto 行为）
            logger.warning("tool_choice.type=function missing function name, dropped (Anthropic would reject empty name)")
            return None
        return {"type": "tool", "name": name}
    return None


def render_tools_anthropic(entries: list[dict[str, Any]], *, include_strict: bool = True) -> list[dict[str, Any]]:
    """中间工具条目 → Anthropic tools。

    ``include_strict=False`` 供 Responses→Anthropic 方向使用（原行为不透传 strict；
    Chat→Anthropic 方向透传）。
    """
    tools = []
    for entry in entries:
        tool = {
            "name": entry["name"],
            "description": entry["description"],
            "input_schema": entry["parameters"] if entry["parameters"] is not None else {"type": "object", "properties": {}},
        }
        if include_strict and "strict" in entry:
            tool["strict"] = entry["strict"]
        tools.append(tool)
    return tools


def _effort_to_budget(reasoning_effort: Any) -> int:
    """reasoning_effort（low/medium/high 或数字预算）-> thinking budget_tokens。"""
    if isinstance(reasoning_effort, int) or (isinstance(reasoning_effort, str) and reasoning_effort.isdigit()):
        return int(reasoning_effort)
    return _REASONING_EFFORT_BUDGETS.get(reasoning_effort, 4096)


def _thinking_budget_for_effort(reasoning_effort: Any, max_tokens: int | None) -> int | None:
    """按 max_tokens 钳制 thinking budget（Anthropic 要求 budget < max_tokens 且 >= 1024）。

    max_tokens 余量不足 1024 时返回 None，调用方应跳过 thinking 映射（避免 400）。
    """
    budget = _effort_to_budget(reasoning_effort)
    if max_tokens:
        headroom = max_tokens - 1024
        if headroom < 1024:
            return None
        if budget > headroom:
            budget = headroom
    return budget


class ToAnthropicConverter(BaseConverter):
    """任意格式 → Anthropic Messages"""

    def __init__(self):
        self._stream_state: AnthropicStreamState | None = None

    def _reset_stream_state(self):
        self._stream_state = AnthropicStreamState()

    # --- 中间条目 → Anthropic 消息渲染（Chat / Responses 源共用） ---

    @staticmethod
    def _is_empty_anthropic_content(content: Any) -> bool:
        """目标格式中不能发送的空 content 形态。"""
        return content is None or content == "" or content == []

    @staticmethod
    def _has_nonempty_text(value: Any) -> bool:
        """空字符串不能作为 Anthropic text 块发送；空白字符仍保留原始语义。"""
        return not isinstance(value, str) or bool(value)

    def _system_content_to_anthropic_blocks(self, content: Any) -> list[dict[str, Any]]:
        """Chat system/developer 原始内容 → 非空 Anthropic system text 块。"""
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if self._has_nonempty_text(content) else []
        if not isinstance(content, list):
            return []

        blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                if self._has_nonempty_text(item.get("text")):
                    blocks.append(item)
            elif isinstance(item, str) and self._has_nonempty_text(item):
                blocks.append({"type": "text", "text": item})
        return blocks

    @staticmethod
    def _require_sendable_messages(messages: list[dict[str, Any]]) -> None:
        """拒绝清理空占位后已无有效对话内容的请求。"""
        if not messages:
            raise ValueError("cannot convert an all-empty conversation to Anthropic")

    @staticmethod
    def _image_url_to_anthropic_block(url: str) -> dict[str, Any]:
        """把 Chat/Responses 的图片 URL（data: URI 或 http(s)）转为 Anthropic image 块。"""
        if url.startswith("data:"):
            # data URI -> base64
            parts = url.split(",", 1)
            media_type = parts[0].split(";")[0].split(":")[1] if parts else "image/png"
            data = parts[1] if len(parts) > 1 else ""
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        return {"type": "image", "source": {"type": "url", "url": url}}

    @staticmethod
    def _file_part_to_anthropic_block(file_value: dict[str, Any]) -> dict[str, Any]:
        """可移植 file URL/data URI → Anthropic document；其余由 Plan 预先拒绝。"""
        file_data = file_value.get("file_data")
        if isinstance(file_data, str) and file_data.startswith("data:"):
            # data URI -> base64 document
            parts = file_data.split(",", 1)
            media_type = parts[0].split(";")[0].split(":")[1] if parts else "application/pdf"
            data = parts[1] if len(parts) > 1 else ""
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
        file_url = file_value.get("file_url")
        if isinstance(file_url, str) and file_url.startswith(("http://", "https://")):
            return {"type": "document", "source": {"type": "url", "url": file_url}}
        raise ValueError("file content requires portable file_data or file_url")

    def _parts_to_anthropic_blocks(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """kind part 条目 → Anthropic content 块（原 _convert_content 渲染语义）。"""
        blocks: list[dict[str, Any]] = []
        for part in parts:
            kind = part["kind"]
            if kind == "text":
                if self._has_nonempty_text(part["text"]):
                    blocks.append({"type": "text", "text": part["text"]})
            elif kind == "image":
                url = part.get("url", "")
                if url.startswith("data:") or url.startswith("http://") or url.startswith("https://"):
                    blocks.append(self._image_url_to_anthropic_block(url))
                else:
                    logger.warning("Unsupported image_url format: %s...", url[:50])
                    blocks.append(
                        {
                            "type": "text",
                            "text": (f"[Unsupported image_url format: {url[:100]}]"),
                        }
                    )
            elif kind == "file":
                blocks.append(self._file_part_to_anthropic_block(part["file"]))
            elif kind == "audio":
                # Anthropic 暂不支持音频输入，保留文本提示
                blocks.append({"type": "text", "text": "[Audio input not supported]"})
            elif kind == "refusal":
                # OpenAI refusal -> text with marker
                if part["text"]:
                    blocks.append({"type": "text", "text": f"[REFUSAL] {part['text']}"})
            elif kind == "unknown":
                item_type = part["block"].get("type", "unknown")
                logger.warning(
                    "Unsupported content item type '%s', converting to text",
                    item_type,
                )
                blocks.append(
                    {
                        "type": "text",
                        "text": f"[Unsupported content type: {item_type}]",
                    }
                )
            # tool_use / tool_result / thinking / document / search_result /
            # redacted_thinking 为 Anthropic 源特有 kind，不会进入本渲染段
        return blocks

    def _entry_content_to_anthropic(self, entry: dict[str, Any]) -> Any:
        """条目 content → Anthropic content（源为字符串时保持字符串，列表转块）。"""
        if entry["text"] is not None:
            return entry["text"]
        return self._parts_to_anthropic_blocks(entry["parts"])

    def _system_entry_blocks(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """system/developer 条目 → Anthropic system text 块。

        Chat 源 developer 条目带 raw_content：text dict 原样透传（保留
        cache_control 等扩展字段）；Responses 源 system 条目按 kind 渲染文本块。
        """
        raw_content = entry.get("raw_content")
        if raw_content is not None:
            return self._system_content_to_anthropic_blocks(raw_content)
        if entry["text"] is not None:
            return self._system_content_to_anthropic_blocks(entry["text"])
        return [block for block in self._parts_to_anthropic_blocks(entry["parts"]) if block.get("type") == "text"]

    def _assistant_entry_to_anthropic_message(self, entry: dict[str, Any]) -> dict[str, Any]:
        content_parts: list[dict[str, Any]] = []
        # thinking 块必须在 text 块之前
        if entry["reasoning"]:
            content_parts.append(
                {
                    "type": "thinking",
                    "thinking": entry["reasoning"],
                    "signature": "",
                }
            )
        if entry["text"] is not None and self._has_nonempty_text(entry["text"]):
            content_parts.append({"type": "text", "text": entry["text"]})
        else:
            content_parts.extend(self._parts_to_anthropic_blocks(entry["parts"]))
        for tc in entry["tool_calls"]:
            args, _ = safe_parse_tool_args(tc["arguments"])
            content_parts.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": args,
                }
            )
        return {
            "role": "assistant",
            "content": content_parts if content_parts else "",
        }

    def _append_tool_entry_to_anthropic_messages(self, entry: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        for tool_result in entry["tool_results"]:
            tool_use_id = tool_result["tool_use_id"]
            if not tool_use_id:
                # Anthropic 会因 tool_use_id 为空拒绝整次请求，主动丢弃并告警
                logger.warning("tool message missing tool_call_id, dropped")
                continue
            content = tool_result["content"]
            if isinstance(content, list) and tool_result.get("parts") is not None:
                # 多模态 tool 结果（含 image_url 等）需标准化为 Anthropic 块，
                # 否则 OpenAI 结构会原样透传给 Anthropic 导致 400。
                content = self._parts_to_anthropic_blocks(tool_result["parts"])
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
            }
            if not self._is_empty_anthropic_content(content):
                tool_result_block["content"] = content
            # 将连续的 tool 消息合并到同一个 user 消息中（Anthropic 格式要求）
            if messages and messages[-1]["role"] == "user":
                last_content = messages[-1].get("content")
                if isinstance(last_content, list) and any(c.get("type") == "tool_result" for c in last_content):
                    messages[-1]["content"].append(tool_result_block)
                else:
                    messages.append({"role": "user", "content": [tool_result_block]})
            else:
                messages.append({"role": "user", "content": [tool_result_block]})

    def _render_entries_to_anthropic_messages(
        self,
        entries: list[dict[str, Any]],
        system: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """消息中间条目 → Anthropic messages；developer 条目归入 system 块列表。

        Responses 源的 system 角色 item 保留在 messages 中（与既有行为一致，
        官方 Anthropic 虽会拒收，但 instructions 字段才是 Responses 的 system 通道）。
        """
        messages: list[dict[str, Any]] = []
        for entry in entries:
            role = entry["role"]
            if role == "developer":
                system.extend(self._system_entry_blocks(entry))
                continue
            if role == "assistant":
                message = self._assistant_entry_to_anthropic_message(entry)
                if self._is_empty_anthropic_content(message["content"]):
                    # Chat 历史中的纯空 turn 没有可发送的 Anthropic 内容；省略该
                    # no-op，而非中断整个下游请求。
                    continue
                messages.append(message)
            elif role == "tool":
                self._append_tool_entry_to_anthropic_messages(entry, messages)
            else:
                content = self._entry_content_to_anthropic(entry)
                if self._is_empty_anthropic_content(content):
                    # 与空 assistant turn 同理：只跳过占位 turn，保留请求中其余对话。
                    continue
                messages.append({"role": role, "content": content})
        return messages

    # --- Chat Completions → Anthropic ---

    def _chat_request_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        system_contents, entries = parse_chat_messages(data)
        system: list[dict[str, Any]] = []
        for content in system_contents:
            system.extend(self._system_content_to_anthropic_blocks(content))
        messages = self._render_entries_to_anthropic_messages(entries, system)
        self._require_sendable_messages(messages)

        if data.get("max_tokens") is not None:
            max_tokens = data["max_tokens"]
        elif data.get("max_completion_tokens") is not None:
            max_tokens = data["max_completion_tokens"]
        else:
            max_tokens = 16384

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
            "max_tokens": max_tokens,
        }
        if system:
            result["system"] = system
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        if data.get("stop"):
            result["stop_sequences"] = data["stop"] if isinstance(data["stop"], list) else [data["stop"]]
        if data.get("tools"):
            result["tools"] = render_tools_anthropic(parse_chat_tools(data["tools"]))
        if data.get("tool_choice"):
            rendered = render_tool_choice_anthropic(parse_chat_tool_choice(data["tool_choice"]))
            if rendered is not None:
                result["tool_choice"] = rendered

        reasoning_effort = data.get("reasoning_effort")
        if data.get("thinking") is not None:
            result["thinking"] = data["thinking"]
        elif reasoning_effort is not None:
            budget = _thinking_budget_for_effort(reasoning_effort, result.get("max_tokens"))
            if budget is not None:
                result["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif data.get("enable_thinking"):
            result["thinking"] = {"type": "enabled", "budget_tokens": 4096}

        if data.get("metadata"):
            result["metadata"] = data["metadata"]

        # user_id 处理：OpenAI user -> Anthropic metadata.user_id
        if data.get("user"):
            if "metadata" not in result:
                result["metadata"] = {}
            result["metadata"]["user_id"] = data["user"]

        # 参数兼容性警告（Anthropic 不支持的参数）
        unsupported_params = []
        if data.get("frequency_penalty") is not None and data["frequency_penalty"] != 0:
            unsupported_params.append("frequency_penalty")
        if data.get("presence_penalty") is not None and data["presence_penalty"] != 0:
            unsupported_params.append("presence_penalty")
        if data.get("seed") is not None:
            unsupported_params.append("seed")
        if data.get("n", 1) > 1:
            unsupported_params.append("n>1 (multiple choices)")
        if data.get("response_format"):
            unsupported_params.append("response_format")
        if data.get("logprobs"):
            unsupported_params.append("logprobs")
        if unsupported_params:
            logger.debug(
                "OpenAI parameters not supported by Anthropic, will be ignored: %s",
                ", ".join(unsupported_params),
            )

        return result

    def _chat_response_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices", [])
        if len(choices) > 1:
            logger.warning(
                "Multiple choices (%d) received, only the first will be converted (Anthropic does not support n>1)",
                len(choices),
            )
        text = ""
        reasoning_content = ""
        finish_reason = "end_turn"
        tool_calls = None
        refusal = ""
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""
            reasoning_content = msg.get("reasoning_content", "") or ""
            tool_calls = msg.get("tool_calls")
            refusal = msg.get("refusal", "") or ""

        content = []
        # thinking 块必须在 text 块之前
        if reasoning_content:
            content.append(
                {
                    "type": "thinking",
                    "thinking": reasoning_content,
                    "signature": "",
                }
            )
        if text:
            content.append({"type": "text", "text": text})
        if tool_calls:
            for tc in tool_calls:
                args = tc.get("function", {}).get("arguments", "{}")
                args, _ = safe_parse_tool_args(args)
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "input": args,
                    }
                )
        if refusal and not text and not tool_calls:
            # Anthropic 没有 refusal content block；用带标记的 text 块兜底，
            # 并把 stop_reason 映射成 refusal 以便客户端识别。
            logger.warning(
                "OpenAI response contained refusal, projecting to text block: %s",
                refusal[:200],
            )
            content.append({"type": "text", "text": f"[REFUSED] {refusal}"})
        if not content:
            content.append({"type": "text", "text": ""})

        if choices:
            fr = choices[0].get("finish_reason", "")
            finish_reason = render_finish_anthropic(parse_chat_finish_reason(fr))
        if refusal and not text and not tool_calls:
            finish_reason = "refusal"

        return {
            "id": data.get("id", "").replace("chatcmpl-", "msg_"),
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": data.get("model", ""),
            "stop_reason": finish_reason,
            "stop_sequence": None,
            "usage": openai_chat_to_anthropic(data.get("usage")),
        }

    def _chat_stream_chunk_to_anthropic(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert a single OpenAI chat chunk to a list of Anthropic events（两拍协议拍 1）。"""
        if self._stream_state is None:
            self._reset_stream_state()
        assert self._stream_state is not None
        state = self._stream_state
        events: list[dict[str, Any]] = []
        choices = chunk.get("choices", [])

        if not choices or choices[0] is None:
            return state.handle_usage_chunk(chunk.get("usage"))

        delta = choices[0].get("delta") or {}
        finish_reason = choices[0].get("finish_reason")

        if delta.get("role") == "assistant":
            events.extend(state.ensure_started(chunk))

        if delta.get("reasoning_content") is not None:
            reasoning_text = delta.get("reasoning_content")
            if reasoning_text:
                events.extend(state.ensure_started(chunk))
                events.extend(state.open_block("thinking"))
                events.extend(state.append_delta(reasoning_text))

        content_text = delta.get("content")
        if content_text is not None and content_text != "":
            events.extend(state.ensure_started(chunk))
            events.extend(state.open_block("text"))
            events.extend(state.append_delta(content_text))

        if delta.get("tool_calls"):
            events.extend(state.ensure_started(chunk))
            for tc in delta["tool_calls"]:
                tc_index = tc.get("index", 0)
                if tc_index not in state.tool_call_indices:
                    events.extend(state.ensure_tool_block(tc_index, tc.get("id", ""), tc.get("function", {}).get("name", "")))
                args = tc.get("function", {}).get("arguments")
                if args is not None and args != "":
                    events.extend(state.append_delta(args, tool_index=tc_index))

        if finish_reason is not None:
            if state.message_stop_sent:
                return events
            events.extend(state.ensure_started(chunk))
            if state.content_block_started:
                events.extend(state.close_block(advance_index=False))
            usage = chunk.get("usage")
            events.extend(state.queue_stop(usage=usage, finish_reason=finish_reason))

        return events

    # --- OpenAI Response → Anthropic ---

    def _response_request_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        instructions, entries = parse_responses_input(data)
        system: list[dict[str, Any]] = []
        messages = self._render_entries_to_anthropic_messages(entries, system)
        self._require_sendable_messages(messages)

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
            "max_tokens": data.get("max_output_tokens", 16384),
        }
        if instructions:
            result["system"] = instructions
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        # M4: Responses stop -> Anthropic stop_sequences（与 Chat 方向一致）
        if data.get("stop") is not None:
            stop = data["stop"]
            result["stop_sequences"] = stop if isinstance(stop, list) else [stop]
        if data.get("tools"):
            tool_entries, _hosted_types, _unsupported_types = parse_responses_tools(data["tools"])
            result["tools"] = render_tools_anthropic(tool_entries, include_strict=False)
        if data.get("tool_choice"):
            rendered = render_tool_choice_anthropic(parse_responses_tool_choice(data["tool_choice"]))
            if rendered is not None:
                result["tool_choice"] = rendered
        # M4: Responses reasoning.effort -> Anthropic thinking（与 Chat 方向同一映射，
        # 推理请求不再静默降级为无思考）
        reasoning = data.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
            budget = _thinking_budget_for_effort(reasoning["effort"], result.get("max_tokens"))
            if budget is not None:
                result["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return result

    def _response_response_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        content = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        content.append({"type": "text", "text": c.get("text", "")})
            elif item.get("type") == "function_call":
                args = item.get("arguments", "{}")
                args, _ = safe_parse_tool_args(args)
                content.append(
                    {
                        "type": "tool_use",
                        "id": item.get("call_id", item.get("id", "")),
                        "name": item.get("name", ""),
                        "input": args,
                    }
                )
        if not content:
            content.append({"type": "text", "text": ""})

        stop_reason = render_finish_anthropic(parse_responses_finish(data.get("status", "completed"), data.get("output", [])))

        return {
            "id": f"msg_{data.get('id', '')}",
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": data.get("model", ""),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": openai_response_to_anthropic(data.get("usage")),
        }

    def _response_stream_chunk_to_anthropic(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        if self._stream_state is None:
            self._reset_stream_state()
        assert self._stream_state is not None
        state = self._stream_state
        events: list[dict[str, Any]] = []
        event_type = chunk.get("type", "")

        if event_type == "response.created":
            resp = chunk.get("response") or {}
            if not state.started:
                state.started = True
                events.append(
                    {
                        "type": "message_start",
                        "message": {
                            "id": f"msg_{resp.get('id', '')}",
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": resp.get("model", ""),
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    }
                )

        elif event_type == "response.output_item.added":
            item = chunk.get("item") or {}
            if item.get("type") == "function_call":
                events.extend(
                    state.open_block(
                        "tool_use",
                        tool_id=item.get("call_id", item.get("id", "")),
                        tool_name=item.get("name", ""),
                    )
                )
            elif item.get("type") == "reasoning":
                events.extend(state.open_block("thinking"))
            elif item.get("type") == "message" and state.content_block_started:
                events.extend(state.close_block(advance_index=True))

        elif event_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            text = chunk.get("delta", "")
            if not state.content_block_started:
                events.extend(state.open_block("thinking"))
            if text:
                events.extend(state.append_delta(text))

        elif event_type == "response.output_text.delta":
            if not state.content_block_started:
                events.extend(state.open_block("text"))
            delta_text = chunk.get("delta", "")
            if delta_text:
                events.extend(state.append_delta(delta_text))

        elif event_type == "response.function_call_arguments.delta":
            if not state.content_block_started:
                state.content_block_started = True
            args = chunk.get("delta", "")
            if args:
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": state.content_block_index,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    }
                )

        elif event_type == "response.output_item.done":
            if state.content_block_started:
                events.extend(state.close_block(advance_index=True))

        elif event_type == "response.completed":
            resp = chunk.get("response") or {}
            if state.content_block_started:
                events.extend(state.close_block(advance_index=False))
            status = resp.get("status", "completed")
            stop_reason = render_finish_anthropic(parse_responses_finish(status, resp.get("output", [])))
            usage_output = _responses_usage_output_final(resp.get("usage"))
            events.append(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage_output},
                }
            )
            events.append({"type": "message_stop"})
            state.message_stop_sent = True

        return events

    # --- 公共接口（source_type 分发为类级映射表）---

    _REQUEST_HANDLERS = {
        "openai-chat-completions": _chat_request_to_anthropic,
        "openai-response": _response_request_to_anthropic,
    }
    _RESPONSE_HANDLERS = {
        "openai-chat-completions": _chat_response_to_anthropic,
        "openai-response": _response_response_to_anthropic,
    }
    _STREAM_HANDLERS = {
        "openai-chat-completions": _chat_stream_chunk_to_anthropic,
        "openai-response": _response_stream_chunk_to_anthropic,
    }

    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        return self._dispatch_source(self._REQUEST_HANDLERS, source_type, source_data)

    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        return self._dispatch_source(self._RESPONSE_HANDLERS, source_type, target_response)

    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> list[dict[str, Any]]:
        """流式拍 1：返回该 chunk 产生的全部 Anthropic 事件（空列表 = 本拍无产出）。"""
        return self._dispatch_source(self._STREAM_HANDLERS, source_type, chunk)

    def finalize_stream(self, source_type: str = "") -> list[dict[str, Any]]:
        """流末（[DONE]）补出 pending finish_reason 对应的 message_stop。"""
        if self._stream_state is None:
            return []
        if source_type != "openai-chat-completions":
            return []
        state = self._stream_state
        if state.message_stop_sent:
            return []
        if not state.started:
            return []
        return state.finalize()
