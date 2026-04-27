"""
将其他格式转换为 Anthropic Messages 格式
"""
import json
from typing import Any

from converters.base import BaseConverter


class ToAnthropicConverter(BaseConverter):
    """任意格式 → Anthropic Messages"""

    def __init__(self):
        self._stream_state: dict[str, Any] | None = None

    def _reset_stream_state(self):
        self._stream_state = {
            "started": False,
            "content_block_started": False,
            "content_block_index": 0,
            "current_content_type": None,
            "tool_id": None,
            "tool_name": None,
            "tool_call_indices": {},
        }

    # --- Chat Completions → Anthropic ---

    def _convert_content(self, content: Any) -> Any:
        if isinstance(content, list):
            result = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        result.append({"type": "text", "text": item.get("text", "")})
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
                        result.append(item)
                elif isinstance(item, str):
                    result.append({"type": "text", "text": item})
            return result
        return content

    def _chat_request_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        system = None
        messages = []
        for msg in data.get("messages", []):
            role = msg.get("role", "user")
            if role == "system":
                system = msg.get("content", "")
            else:
                content = msg.get("content", "")
                content = self._convert_content(content)
                if isinstance(content, str):
                    content = content
                messages.append({"role": role, "content": content})

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
            "max_tokens": data.get("max_tokens", 16384),
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
                    result["tool_choice"] = {"type": "tool", "name": tc.get("function", {}).get("name", "")}
            elif tc == "auto":
                result["tool_choice"] = {"type": "auto"}
            elif tc == "required":
                result["tool_choice"] = {"type": "any"}

        if data.get("thinking") is not None:
            result["thinking"] = data["thinking"]
        elif data.get("enable_thinking"):
            result["thinking"] = {"type": "enabled", "budget_tokens": 4096}

        reasoning_effort = data.get("reasoning_effort")
        if reasoning_effort is not None:
            if isinstance(reasoning_effort, int) or (isinstance(reasoning_effort, str) and reasoning_effort.isdigit()):
                budget = int(reasoning_effort)
            else:
                budget = {"low": 1024, "medium": 4096, "high": 16384}.get(reasoning_effort, 4096)
            result["thinking"] = {"type": "enabled", "budget_tokens": budget}

        if data.get("metadata"):
            result["metadata"] = data["metadata"]

        return result

    def _openai_tools_to_anthropic(self, tools: list) -> list:
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                })
        return anthropic_tools

    def _chat_response_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices", [])
        text = ""
        finish_reason = "end_turn"
        tool_calls = None
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls")

        content = []
        if text:
            content.append({"type": "text", "text": text})
        if tool_calls:
            for tc in tool_calls:
                args = tc.get("function", {}).get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "input": args,
                })
        if not content:
            content.append({"type": "text", "text": ""})

        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }
        if choices:
            fr = choices[0].get("finish_reason", "")
            finish_reason = stop_reason_map.get(fr, "end_turn")

        return {
            "id": data.get("id", "").replace("chatcmpl-", "msg_"),
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": data.get("model", ""),
            "stop_reason": finish_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
            },
        }

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
            if usage and self._stream_state["started"]:
                if self._stream_state["content_block_started"]:
                    events.append(
                        ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                    )
                    self._stream_state["content_block_started"] = False
                events.append(
                    ("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": usage.get("completion_tokens", 0)},
                    })
                )
                events.append(("message_stop", {"type": "message_stop"}))
            return events

        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")

        if delta.get("role") == "assistant" and not self._stream_state["started"]:
            self._stream_state["started"] = True
            events.append(
                ("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": chunk.get("id", "").replace("chatcmpl-", "msg_"),
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": chunk.get("model", ""),
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                })
            )

        if delta.get("content") is not None:
            if not self._stream_state["content_block_started"]:
                self._stream_state["content_block_started"] = True
                self._stream_state["current_content_type"] = "text"
                self._stream_state["content_block_index"] = 0
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    })
                )
            events.append(
                ("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": delta["content"]},
                })
            )

        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                tc_index = tc.get("index", 0)
                if tc_index not in self._stream_state["tool_call_indices"]:
                    if self._stream_state["content_block_started"]:
                        events.append(
                            ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                        )
                        self._stream_state["content_block_started"] = False
                        self._stream_state["content_block_index"] += 1

                    self._stream_state["tool_call_indices"][tc_index] = self._stream_state["content_block_index"]

                if tc.get("function", {}).get("name"):
                    self._stream_state["current_content_type"] = "tool_use"
                    self._stream_state["tool_id"] = tc.get("id", "")
                    self._stream_state["tool_name"] = tc["function"]["name"]
                    self._stream_state["content_block_started"] = True
                    events.append(
                        ("content_block_start", {
                            "type": "content_block_start",
                            "index": self._stream_state["content_block_index"],
                            "content_block": {
                                "type": "tool_use",
                                "id": tc.get("id", ""),
                                "name": tc["function"]["name"],
                                "input": {},
                            },
                        })
                    )
                elif tc.get("function", {}).get("arguments") is not None:
                    if not self._stream_state["content_block_started"]:
                        self._stream_state["content_block_started"] = True
                    args = tc["function"].get("arguments", "")
                    if args:
                        events.append(
                            ("content_block_delta", {
                                "type": "content_block_delta",
                                "index": self._stream_state["content_block_index"],
                                "delta": {"type": "input_json_delta", "partial_json": args},
                            })
                        )

        if delta.get("reasoning_content") is not None:
            if not self._stream_state["content_block_started"]:
                self._stream_state["content_block_started"] = True
                self._stream_state["current_content_type"] = "thinking"
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": self._stream_state["content_block_index"],
                        "content_block": {"type": "thinking", "thinking": ""},
                    })
                )
            events.append(
                ("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self._stream_state["content_block_index"],
                    "delta": {"type": "thinking_delta", "thinking": delta["reasoning_content"]},
                })
            )

        if finish_reason is not None:
            if self._stream_state["content_block_started"]:
                events.append(
                    ("content_block_stop", {"type": "content_block_stop", "index": self._stream_state["content_block_index"]})
                )
                self._stream_state["content_block_started"] = False

            stop_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
            stop_reason = stop_map.get(finish_reason, "end_turn")

            usage_output = 0
            usage = chunk.get("usage")
            if usage:
                usage_output = usage.get("completion_tokens", 0)

            events.append(
                ("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage_output},
                })
            )
            events.append(("message_stop", {"type": "message_stop"}))

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
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": item.get("call_id", ""),
                            "content": tool_result_content,
                        }],
                    })
                elif item_type == "function_call":
                    messages.append({
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "id": item.get("call_id", item.get("id", "")),
                            "name": item.get("name", ""),
                            "input": json.loads(item.get("arguments", "{}")) if isinstance(item.get("arguments"), str) else item.get("arguments", {}),
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
                    result["tool_choice"] = {"type": "tool", "name": tc.get("name", tc.get("function", {}).get("name", ""))}
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
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
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
            "usage": {
                "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("usage", {}).get("output_tokens", 0),
            },
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

        elif event_type == "response.output_text.delta":
            if not self._stream_state["content_block_started"]:
                self._stream_state["content_block_started"] = True
                self._stream_state["current_content_type"] = "text"
                self._stream_state["content_block_index"] = 0
                events.append(
                    ("content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    })
                )
            events.append(
                ("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
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
            usage_output = resp.get("usage", {}).get("output_tokens", 0)
            events.append(
                ("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage_output},
                })
            )
            events.append(("message_stop", {"type": "message_stop"}))

        return events

    # --- 公共接口 ---

    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "openai-chat-completions":
            return self._chat_request_to_anthropic(source_data)
        elif source_type == "openai-response":
            return self._response_request_to_anthropic(source_data)
        return source_data

    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "openai-chat-completions":
            return self._chat_response_to_anthropic(target_response)
        elif source_type == "openai-response":
            return self._response_response_to_anthropic(target_response)
        return target_response

    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> dict[str, Any] | None:
        if source_type == "openai-chat-completions":
            events = self._chat_stream_chunk_to_anthropic(chunk)
        elif source_type == "openai-response":
            events = self._response_stream_chunk_to_anthropic(chunk)
        else:
            return chunk
        if not events:
            return None
        result = events[0][1]
        if len(events) > 1:
            result["_extra_events"] = events[1:]
        return result

    def get_stream_event_type(self, chunk: dict[str, Any], source_type: str = "") -> str | None:
        if source_type == "openai-chat-completions":
            events = self._chat_stream_chunk_to_anthropic(chunk)
        elif source_type == "openai-response":
            events = self._response_stream_chunk_to_anthropic(chunk)
        else:
            if isinstance(chunk, dict) and chunk.get("_event_type"):
                return chunk["_event_type"]
            return None
        if not events:
            return None
        return events[0][0]

    def get_extra_events(self, chunk: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if isinstance(chunk, dict) and chunk.get("_extra_events"):
            return chunk["_extra_events"]
        return []
