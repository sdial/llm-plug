"""
将其他格式转换为 Anthropic Messages 格式
"""
from typing import Any

from loguru import logger

from converters.base import BaseConverter, safe_parse_tool_args
from converters.usage import openai_chat_to_anthropic, openai_response_to_anthropic


class ToAnthropicConverter(BaseConverter):
    """任意格式 → Anthropic Messages"""

    def __init__(self):
        self._stream_state: dict[str, Any] | None = None
        self._last_event_type: str | None = None

    def _reset_stream_state(self):
        self._stream_state = {
            "started": False,
            "content_block_started": False,
            "content_block_index": 0,
            "current_content_type": None,
            "tool_id": None,
            "tool_name": None,
            "tool_call_indices": {},
            "_prev_completion_tokens": 0,
            "message_stop_sent": False,
            "pending_finish_reason": None,
        }

    # --- Chat Completions → Anthropic ---

    def _ensure_message_started(self, chunk: dict[str, Any], events: list) -> None:
        """确保 message_start 已发出。如果尚未发出，在 events 列表头部插入。"""
        if not self._stream_state["started"]:
            self._stream_state["started"] = True
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
                "input_tokens": chunk.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
                    },
                },
            ))

    def _convert_content(self, content: Any) -> Any:
        if isinstance(content, list):
            result = []
            for item in content:
                if isinstance(item, str):
                    result.append({"type": "text", "text": item})
                    continue
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        result.append({"type": "text", "text": item.get("text", "")})
                    elif item.get("type") == "image_url":
                        url = item.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # data URI -> base64
                            parts = url.split(",", 1)
                            media_type = parts[0].split(";")[0].split(":")[1] if parts else "image/png"
                            data = parts[1] if len(parts) > 1 else ""
                            result.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": data},
                            })
                        elif url.startswith("http://") or url.startswith("https://"):
                            result.append({
                                "type": "image",
                                "source": {"type": "url", "url": url},
                            })
                        else:
                            logger.warning("Unsupported image_url format: %s...", url[:50])
                            result.append({"type": "text", "text": f"[Unsupported image_url format: {url[:100]}]"})

                    elif item.get("type") == "input_text":
                        # OpenAI Response API input_text -> text
                        result.append({"type": "text", "text": item.get("text", "")})

                    elif item.get("type") == "refusal":
                        # OpenAI refusal -> text with marker
                        refusal_text = item.get("refusal", "")
                        if refusal_text:
                            result.append({"type": "text", "text": f"[REFUSAL] {refusal_text}"})

                    elif item.get("type") == "input_audio":
                        # Anthropic 暂不支持音频输入，保留文本提示
                        result.append({"type": "text", "text": "[Audio input not supported]"})

                    elif item.get("type") == "file":
                        file_info = item.get("file", {})
                        file_data = file_info.get("file_data", "")
                        filename = file_info.get("filename", "")
                        if file_data.startswith("data:"):
                            # data URI -> base64 document
                            parts = file_data.split(",", 1)
                            media_type = parts[0].split(";")[0].split(":")[1] if parts else "application/pdf"
                            data = parts[1] if len(parts) > 1 else ""
                            result.append({
                                "type": "document",
                                "source": {"type": "base64", "media_type": media_type, "data": data},
                            })
                        else:
                            result.append({"type": "text", "text": f"[File input not supported: {filename or 'unknown'}]"})

                    else:
                        item_type = item.get("type", "unknown")
                        logger.warning("Unsupported content item type '%s', converting to text", item_type)
                        result.append({"type": "text", "text": f"[Unsupported content type: {item_type}]"})
            return result
        return content

    def _chat_request_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        system = None
        messages = []
        for msg in data.get("messages", []):
            role = msg.get("role", "user")
            if role in ("system", "developer"):
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
            elif role == "assistant":
                content_parts = []
                reasoning_content = msg.get("reasoning_content")
                if reasoning_content:
                    content_parts.append({
                        "type": "thinking",
                        "thinking": reasoning_content,
                        "signature": "",
                    })
                text_content = msg.get("content")
                if text_content is not None:
                    if isinstance(text_content, str):
                        content_parts.append({"type": "text", "text": text_content})
                    elif isinstance(text_content, list):
                        converted = self._convert_content(text_content)
                        if isinstance(converted, list):
                            content_parts.extend(converted)
                        else:
                            content_parts.append({"type": "text", "text": str(converted)})
                for tc in msg.get("tool_calls", []):
                    args = tc.get("function", {}).get("arguments", "{}")
                    args, _ = safe_parse_tool_args(args)
                    content_parts.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "input": args,
                    })
                messages.append({"role": "assistant", "content": content_parts if content_parts else ""})
            elif role == "tool":
                tool_use_id = msg.get("tool_call_id", "")
                if not tool_use_id:
                    # Anthropic 会因 tool_use_id 为空拒绝整次请求，主动丢弃并告警
                    logger.warning("tool message missing tool_call_id, dropped")
                    continue
                tool_content = msg.get("content", "")
                # 多模态 tool 结果（含 image_url 等）需经 _convert_content 标准化，
                # 否则 OpenAI 结构会原样透传给 Anthropic 导致 400。
                if isinstance(tool_content, list):
                    tool_content = self._convert_content(tool_content)
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": tool_content,
                }
                # 将连续的 tool 消息合并到同一个 user 消息中（Anthropic 格式要求）
                if messages and messages[-1]["role"] == "user":
                    last_content = messages[-1].get("content")
                    if isinstance(last_content, list) and any(
                        c.get("type") == "tool_result" for c in last_content
                    ):
                        messages[-1]["content"].append(tool_result_block)
                    else:
                        messages.append({"role": "user", "content": [tool_result_block]})
                else:
                    messages.append({"role": "user", "content": [tool_result_block]})
            else:
                content = msg.get("content", "")
                content = self._convert_content(content)
                messages.append({"role": role, "content": content})

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
            result["tools"] = self._openai_tools_to_anthropic(data["tools"])
        if data.get("tool_choice"):
            tc = data["tool_choice"]
            if isinstance(tc, dict):
                if tc.get("type") == "auto":
                    result["tool_choice"] = {"type": "auto"}
                elif tc.get("type") == "required":
                    result["tool_choice"] = {"type": "any"}
                elif tc.get("type") == "function":
                    # 兼容官方嵌套形态 {"type":"function","function":{"name":"..."}}
                    # 和扁平形态 {"type":"function","name":"..."}
                    func_info = tc.get("function")
                    name = func_info.get("name", "") if isinstance(func_info, dict) else ""
                    if not name:
                        name = tc.get("name", "")
                    if name:
                        result["tool_choice"] = {"type": "tool", "name": name}
                    else:
                        # name 为空时构造 {"type":"tool","name":""} 会被 Anthropic 拒绝；
                        # 退化为不指定 tool_choice（让上游用默认 auto 行为）
                        logger.warning(
                            "tool_choice.type=function missing function name, dropped (Anthropic would reject empty name)"
                        )
                # disable_parallel_tool_use OpenAI 无对应
                if tc.get("disable_parallel_tool_use"):
                    logger.debug("OpenAI tool_choice.disable_parallel_tool_use not supported, ignored")
            elif tc == "auto":
                result["tool_choice"] = {"type": "auto"}
            elif tc == "required":
                result["tool_choice"] = {"type": "any"}
            elif tc == "none":
                result["tool_choice"] = {"type": "none"}

        reasoning_effort = data.get("reasoning_effort")
        if data.get("thinking") is not None:
            result["thinking"] = data["thinking"]
        elif reasoning_effort is not None:
            if isinstance(reasoning_effort, int) or (isinstance(reasoning_effort, str) and reasoning_effort.isdigit()):
                budget = int(reasoning_effort)
            else:
                budget = {"low": 1024, "medium": 4096, "high": 16384}.get(reasoning_effort, 4096)
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
                ", ".join(unsupported_params)
            )

        return result

    def _openai_tools_to_anthropic(self, tools: list) -> list:
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                anthropic_tool = {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                }
                # strict 字段透传（Anthropic 也支持）
                if func.get("strict") is not None:
                    anthropic_tool["strict"] = func["strict"]
                anthropic_tools.append(anthropic_tool)
        return anthropic_tools

    def _chat_response_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices", [])
        if len(choices) > 1:
            logger.warning("Multiple choices (%d) received, only the first will be converted (Anthropic does not support n>1)", len(choices))
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
            content.append({
                "type": "thinking",
                "thinking": reasoning_content,
                "signature": "",
            })
        if text:
            content.append({"type": "text", "text": text})
        if tool_calls:
            for tc in tool_calls:
                args = tc.get("function", {}).get("arguments", "{}")
                args, _ = safe_parse_tool_args(args)
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "input": args,
                })
        if refusal and not text and not tool_calls:
            # Anthropic 没有 refusal content block；用带标记的 text 块兜底，
            # 并把 stop_reason 映射成 refusal 以便客户端识别。
            logger.warning("OpenAI response contained refusal, projecting to text block: %s", refusal[:200])
            content.append({"type": "text", "text": f"[REFUSED] {refusal}"})
        if not content:
            content.append({"type": "text", "text": ""})

        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "refusal",
        }
        if choices:
            fr = choices[0].get("finish_reason", "")
            finish_reason = stop_reason_map.get(fr, "end_turn")
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

    def _chat_build_message_stop_events(
        self,
        usage: dict[str, Any] | None,
        finish_reason: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """生成 message_delta + message_stop。仅在 message_stop 未发送时调用。"""
        if self._stream_state["message_stop_sent"]:
            return []
        if finish_reason is None:
            finish_reason = self._stream_state.get("pending_finish_reason") or "stop"
        stop_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "content_filter": "refusal"}
        stop_reason = stop_map.get(finish_reason, "end_turn")

        usage_output = 0
        if usage:
            cumulative = usage.get("completion_tokens", 0)
            prev = self._stream_state.get("_prev_completion_tokens", 0)
            usage_output = cumulative - prev
            self._stream_state["_prev_completion_tokens"] = cumulative

        events: list[tuple[str, dict[str, Any]]] = [
            ("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": usage_output},
            }),
            ("message_stop", {"type": "message_stop"}),
        ]
        self._stream_state["message_stop_sent"] = True
        self._stream_state["pending_finish_reason"] = None
        return events

    def _chat_stream_chunk_to_anthropic(self, chunk: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Convert a single OpenAI chat chunk to a list of (event_type, data) tuples.

        Returns a list because one OpenAI chunk may need to produce
        multiple Anthropic SSE events (e.g. content_block_stop + message_delta + message_stop).
        """
        if self._stream_state is None:
            self._reset_stream_state()

        events: list[tuple[str, dict[str, Any]]] = []
        choices = chunk.get("choices", [])

        if not choices:
            usage = chunk.get("usage")
            if not (usage and self._stream_state["started"]):
                return events
            if self._stream_state["message_stop_sent"]:
                # 已经结束流，避免重复 message_stop（DeepSeek/Qwen 等会在 finish 后再发 usage chunk）
                return events
            if self._stream_state["pending_finish_reason"] is not None:
                # 有 pending finish_reason，现在带着 usage 收尾
                events.extend(self._chat_build_message_stop_events(usage=usage))
            else:
                # 罕见：usage 出现在 finish_reason 之前，按现有约定立即收尾
                if self._stream_state["content_block_started"]:
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
                events.extend(self._chat_build_message_stop_events(usage=usage))
            return events

        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")

        # 第一个 chunk 通常带 role: "assistant"，此时应发出 message_start
        # 这样 chunk 中的 usage 信息（如 prompt_tokens）可以正确传递
        if delta.get("role") == "assistant":
            self._ensure_message_started(chunk, events)

        if delta.get("reasoning_content") is not None:
            reasoning_text = delta.get("reasoning_content")
            if reasoning_text:
                self._ensure_message_started(chunk, events)
                # 当前若有非 thinking 块在进行，先关闭并切换 index
                if self._stream_state["content_block_started"] and self._stream_state["current_content_type"] != "thinking":
                    events.append(
                        ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                    )
                    self._stream_state["content_block_started"] = False
                    self._stream_state["content_block_index"] += 1

                if not self._stream_state["content_block_started"]:
                    self._stream_state["content_block_started"] = True
                    self._stream_state["current_content_type"] = "thinking"
                    events.append(
                        ("content_block_start", {
                            "type": "content_block_start",
                            "index": self._stream_state["content_block_index"],
                            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                        })
                    )
                events.append(
                    ("content_block_delta", {
                        "type": "content_block_delta",
                        "index": self._stream_state["content_block_index"],
                        "delta": {"type": "thinking_delta", "thinking": reasoning_text},
                    })
                )

        # 处理 content（忽略空字符串，避免创建空的 text block）
        content_text = delta.get("content")
        if content_text is not None and content_text != "":
            self._ensure_message_started(chunk, events)
            if self._stream_state["content_block_started"] and self._stream_state["current_content_type"] != "text":
                # 从 thinking 切到 text 前，先补发 signature_delta
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
                self._stream_state["content_block_index"] += 1

            if not self._stream_state["content_block_started"]:
                self._stream_state["content_block_started"] = True
                self._stream_state["current_content_type"] = "text"
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": self._stream_state["content_block_index"],
                        "content_block": {"type": "text", "text": ""},
                    })
                )
            events.append(
                ("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self._stream_state["content_block_index"],
                    "delta": {"type": "text_delta", "text": content_text},
                })
            )

        if delta.get("tool_calls"):
            self._ensure_message_started(chunk, events)
            for tc in delta["tool_calls"]:
                tc_index = tc.get("index", 0)
                is_new_tool_call = tc_index not in self._stream_state["tool_call_indices"]

                if is_new_tool_call:
                    # 关闭上一个 content block 并分配新的 block index
                    if self._stream_state["content_block_started"]:
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
                        self._stream_state["content_block_index"] += 1

                    self._stream_state["tool_call_indices"][tc_index] = self._stream_state["content_block_index"]

                    tool_id = tc.get("id", "")
                    tool_name = tc.get("function", {}).get("name", "")
                    self._stream_state["current_content_type"] = "tool_use"
                    self._stream_state["tool_id"] = tool_id
                    self._stream_state["tool_name"] = tool_name
                    self._stream_state["content_block_started"] = True
                    events.append(
                        ("content_block_start", {
                            "type": "content_block_start",
                            "index": self._stream_state["content_block_index"],
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": {},
                            },
                        })
                    )

                if tc.get("function", {}).get("arguments") is not None:
                    args = tc["function"].get("arguments", "")
                    if args:
                        events.append(
                            ("content_block_delta", {
                                "type": "content_block_delta",
                                "index": self._stream_state["tool_call_indices"][tc_index],
                                "delta": {"type": "input_json_delta", "partial_json": args},
                            })
                        )

        if finish_reason is not None:
            if self._stream_state["message_stop_sent"]:
                return events
            self._ensure_message_started(chunk, events)
            if self._stream_state["content_block_started"]:
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

            usage = chunk.get("usage")
            if usage:
                # 同 chunk 内含 usage，立即收尾
                events.extend(self._chat_build_message_stop_events(usage=usage, finish_reason=finish_reason))
            else:
                # 无 usage：暂存 finish_reason，等待后续 usage chunk 或 finalize_stream
                # （避免 DeepSeek/Qwen 等在 finish 后单独发 usage chunk 时重复 message_stop）
                self._stream_state["pending_finish_reason"] = finish_reason

        return events

    # --- OpenAI Response → Anthropic ---

    def _response_request_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        system = data.get("instructions")
        messages = []
        for item in data.get("input", []):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "function_call_output":
                    tool_result_content = item.get("output", "")
                    tool_result_block = {
                        "type": "tool_result",
                        "tool_use_id": item.get("call_id", ""),
                        "content": tool_result_content,
                    }
                    if messages and messages[-1]["role"] == "user":
                        last_content = messages[-1].get("content")
                        if isinstance(last_content, list) and any(
                            c.get("type") == "tool_result" for c in last_content
                        ):
                            messages[-1]["content"].append(tool_result_block)
                        else:
                            messages.append({"role": "user", "content": [tool_result_block]})
                    else:
                        messages.append({"role": "user", "content": [tool_result_block]})
                elif item_type == "function_call":
                    args = item.get("arguments", "{}")
                    args, _ = safe_parse_tool_args(args)
                    messages.append({
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "id": item.get("call_id", item.get("id", "")),
                            "name": item.get("name", ""),
                            "input": args,
                        }],
                    })
                else:
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    messages.append({"role": role, "content": content})

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
            "max_tokens": data.get("max_output_tokens", 16384),
        }
        if system:
            result["system"] = system
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        if data.get("tools"):
            result["tools"] = self._response_tools_to_anthropic(data["tools"])
        if data.get("tool_choice"):
            tc = data["tool_choice"]
            if isinstance(tc, str):
                if tc == "auto":
                    result["tool_choice"] = {"type": "auto"}
                elif tc == "required":
                    result["tool_choice"] = {"type": "any"}
            elif isinstance(tc, dict):
                if tc.get("type") == "function":
                    name = tc.get("name", tc.get("function", {}).get("name", ""))
                    if name:
                        result["tool_choice"] = {"type": "tool", "name": name}
                    else:
                        logger.warning(
                            "Response API tool_choice.type=function missing name, dropped (Anthropic would reject empty name)"
                        )
                elif tc.get("type") == "auto":
                    result["tool_choice"] = {"type": "auto"}
                elif tc.get("type") == "required":
                    result["tool_choice"] = {"type": "any"}
        return result

    def _response_tools_to_anthropic(self, tools: list) -> list:
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                anthropic_tools.append({
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
                })
        return anthropic_tools

    def _response_response_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        content = []
        stop_reason = "end_turn"
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        content.append({"type": "text", "text": c.get("text", "")})
            elif item.get("type") == "function_call":
                args = item.get("arguments", "{}")
                args, _ = safe_parse_tool_args(args)
                content.append({
                    "type": "tool_use",
                    "id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "input": args,
                })
                stop_reason = "tool_use"
        if not content:
            content.append({"type": "text", "text": ""})

        status = data.get("status", "completed")
        if status == "incomplete":
            stop_reason = "max_tokens"

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

    def _response_stream_chunk_to_anthropic(self, chunk: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if self._stream_state is None:
            self._reset_stream_state()

        events: list[tuple[str, dict[str, Any]]] = []
        event_type = chunk.get("type", "")

        if event_type == "response.created":
            resp = chunk.get("response", {})
            self._stream_state["started"] = True
            events.append(
                ("message_start", {
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
                })
            )

        elif event_type == "response.output_item.added":
            item = chunk.get("item", {})
            if item.get("type") == "function_call":
                if self._stream_state["content_block_started"]:
                    events.append(
                        ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                    )
                    self._stream_state["content_block_started"] = False
                    self._stream_state["content_block_index"] += 1
                self._stream_state["current_content_type"] = "tool_use"
                self._stream_state["content_block_started"] = True
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": self._stream_state["content_block_index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": item.get("call_id", item.get("id", "")),
                            "name": item.get("name", ""),
                            "input": {},
                        },
                    })
                )
            elif item.get("type") == "reasoning":
                # OpenAI Response 的 reasoning output_item 对应 Anthropic 的 thinking 内容块。
                # 注意：Anthropic 客户端通常需要 signature 才能完整渲染 thinking；
                # 这里因为上游不提供 signature，emit 不带 signature 的 thinking 块，
                # 保证 thinking 文本至少能到达客户端（否则会被静默丢弃）。
                if self._stream_state["content_block_started"]:
                    events.append(
                        ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                    )
                    self._stream_state["content_block_started"] = False
                    self._stream_state["content_block_index"] += 1
                self._stream_state["current_content_type"] = "thinking"
                self._stream_state["content_block_started"] = True
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": self._stream_state["content_block_index"],
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    })
                )
            elif item.get("type") == "message":
                # 切换到 message 输出项：若上一项仍开启（如 reasoning），需关闭再开新块
                if self._stream_state["content_block_started"]:
                    events.append(
                        ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                    )
                    self._stream_state["content_block_started"] = False
                    self._stream_state["content_block_index"] += 1

        elif event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            text = chunk.get("delta", "")
            if not self._stream_state["content_block_started"]:
                # 兜底：未收到 output_item.added 就直接 emit delta
                self._stream_state["current_content_type"] = "thinking"
                self._stream_state["content_block_started"] = True
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": self._stream_state["content_block_index"],
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    })
                )
            if text:
                events.append(
                    ("content_block_delta", {
                        "type": "content_block_delta",
                        "index": self._stream_state["content_block_index"],
                        "delta": {"type": "thinking_delta", "thinking": text},
                    })
                )

        elif event_type == "response.output_text.delta":
            if not self._stream_state["content_block_started"]:
                self._stream_state["content_block_started"] = True
                self._stream_state["current_content_type"] = "text"
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": self._stream_state["content_block_index"],
                        "content_block": {"type": "text", "text": ""},
                    })
                )
            events.append(
                ("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self._stream_state["content_block_index"],
                    "delta": {"type": "text_delta", "text": chunk.get("delta", "")},
                })
            )

        elif event_type == "response.function_call_arguments.delta":
            if not self._stream_state["content_block_started"]:
                self._stream_state["content_block_started"] = True
            args = chunk.get("delta", "")
            if args:
                events.append(
                    ("content_block_delta", {
                        "type": "content_block_delta",
                        "index": self._stream_state["content_block_index"],
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    })
                )

        elif event_type == "response.output_item.done":
            if self._stream_state["content_block_started"]:
                events.append(
                    ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                )
                self._stream_state["content_block_started"] = False
                self._stream_state["content_block_index"] += 1

        elif event_type == "response.completed":
            resp = chunk.get("response", {})
            if self._stream_state["content_block_started"]:
                events.append(
                    ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                )
                self._stream_state["content_block_started"] = False
            stop_reason = "end_turn"
            status = resp.get("status", "completed")
            if status == "incomplete":
                stop_reason = "max_tokens"
            elif resp.get("output", []):
                for item in resp.get("output", []):
                    if item.get("type") == "function_call":
                        stop_reason = "tool_use"
                        break
            resp_usage = resp.get("usage") or {}
            usage_output = resp_usage.get("output_tokens", 0)
            events.append(
                ("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage_output},
                })
            )
            events.append(("message_stop", {"type": "message_stop"}))
            self._stream_state["message_stop_sent"] = True

        return events

    # --- 公共接口 ---

    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "openai-chat-completions":
            return self._chat_request_to_anthropic(source_data)
        elif source_type == "openai-response":
            return self._response_request_to_anthropic(source_data)
        raise ValueError(f"ToAnthropicConverter 不支持 source_type={source_type!r}")

    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "openai-chat-completions":
            return self._chat_response_to_anthropic(target_response)
        elif source_type == "openai-response":
            return self._response_response_to_anthropic(target_response)
        raise ValueError(f"ToAnthropicConverter 不支持 source_type={source_type!r}")

    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> dict[str, Any] | None:
        if source_type == "openai-chat-completions":
            events = self._chat_stream_chunk_to_anthropic(chunk)
        elif source_type == "openai-response":
            events = self._response_stream_chunk_to_anthropic(chunk)
        else:
            raise ValueError(f"ToAnthropicConverter 不支持 source_type={source_type!r}")
        if not events:
            return None
        # 缓存首个事件的 event type，供 get_stream_event_type 读取，避免重复转换
        self._last_event_type = events[0][0]
        result = dict(events[0][1])  # 复制一份，避免修改原始数据
        if len(events) > 1:
            # proxy_core 每个请求创建新的 converter 实例；额外事件不能跨请求复用。
            self._pending_extra_events = events[1:]
        else:
            self._pending_extra_events = []
        return result

    def get_stream_event_type(self, chunk: dict[str, Any], source_type: str = "") -> str | None:
        return getattr(self, "_last_event_type", None)

    def get_extra_events(self, chunk: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        # 从实例变量获取额外事件，而不是从 chunk 字典中
        events = getattr(self, "_pending_extra_events", [])
        self._pending_extra_events = []  # 清空，避免重复发送
        return events

    def finalize_stream(self, source_type: str = "") -> list[tuple[str, dict[str, Any]]]:
        """流末（[DONE]）补出 pending finish_reason 对应的 message_stop。

        finish_reason chunk 不一定带 usage（DeepSeek/Qwen 等会在之后单独发 usage chunk），
        因此 _chat_stream_chunk_to_anthropic 在没拿到 usage 时不会立即 emit message_stop；
        如果上游就此结束（仅发 [DONE]），由 finalize_stream 在末尾补出。
        """
        if self._stream_state is None:
            return []
        if source_type != "openai-chat-completions":
            return []
        if self._stream_state.get("message_stop_sent"):
            return []
        if not self._stream_state.get("started"):
            return []

        events: list[tuple[str, dict[str, Any]]] = []
        if self._stream_state.get("content_block_started"):
            if self._stream_state.get("current_content_type") == "thinking":
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
        if self._stream_state.get("pending_finish_reason") is not None:
            events.extend(self._chat_build_message_stop_events(usage=None))
        return events
