"""
将其他格式转换为 OpenAI Response 格式
"""
import json
import secrets
import time
from typing import Any

from converters.base import BaseConverter


class ToResponseConverter(BaseConverter):
    """任意格式 → OpenAI Response"""

    def __init__(self):
        self._stream_state: dict[str, Any] | None = None
        self._pending_extra_events: list[dict[str, Any]] = []

    def _reset_stream_state(self):
        self._stream_state = {
            "response_id": "",
            "model": "",
            "created_at": 0,
            "reasoning_started": False,
            "reasoning_id": "",
            "message_id": "",
            "output_index": 0,
            "response_created_sent": False,
            "output_item_added_sent": False,
            "completed_sent": False,
            "accumulated_text": "",
            "tool_calls": {},  # call_id -> {name, arguments}
            "tool_call_index_to_id": {},
            "reasoning_content": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "sequence_number": 0,
            "text_output_index": None,
            "content_part_added_sent": False,
        }
        self._pending_extra_events = []
        self._need_in_progress = False

    # --- Chat Completions → Response ---

    def _make_response_id(self, upstream_id: str) -> str:
        if upstream_id.startswith("resp_"):
            return upstream_id
        suffix = upstream_id
        for prefix in ("chatcmpl_", "chatcmpl-", "cmpl_", "cmpl-"):
            if suffix.startswith(prefix):
                suffix = suffix[len(prefix):]
                break
        suffix = suffix.replace("-", "_") or secrets.token_hex(12)
        return f"resp_{suffix}"

    def _make_message_id(self, response_id: str, upstream_id: str = "") -> str:
        if upstream_id and upstream_id.startswith("msg_"):
            return upstream_id
        suffix = response_id.removeprefix("resp_")
        return f"msg_{suffix}"

    def _make_function_call_id(self, call_id: str) -> str:
        if call_id.startswith("fc_"):
            return call_id
        suffix = call_id.removeprefix("call_") or secrets.token_hex(8)
        return f"fc_{suffix}"

    def _chat_usage_to_response_usage(self, usage: dict[str, Any]) -> dict[str, Any]:
        result = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            result["input_tokens_details"] = {
                "cached_tokens": prompt_details.get("cached_tokens", 0),
            }
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            result["output_tokens_details"] = {
                "reasoning_tokens": completion_details.get("reasoning_tokens", 0),
            }
        return result

    def _chat_tools_to_response(self, tools: list) -> list:
        response_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                response_tools.append({
                    "type": "function",
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters", {}),
                })
        return response_tools

    def _chat_request_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        input_items = []
        instructions = None
        for msg in data.get("messages", []):
            if msg["role"] == "system":
                instructions = msg.get("content", "")
            elif msg["role"] == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })
            elif msg["role"] == "assistant":
                tool_calls = msg.get("tool_calls")
                content = msg.get("content", "")
                if tool_calls:
                    if content:
                        input_items.append({"role": "assistant", "content": content})
                    for tc in tool_calls:
                        input_items.append({
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", "{}"),
                        })
                else:
                    input_items.append({"role": "assistant", "content": content or ""})
            else:
                input_items.append({
                    "role": msg["role"],
                    "content": msg.get("content", ""),
                })

        result = {
            "model": data.get("model", ""),
            "input": input_items,
            "stream": data.get("stream", False),
        }
        if instructions:
            result["instructions"] = instructions
        if data.get("max_tokens") is not None:
            result["max_output_tokens"] = data["max_tokens"]
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        if data.get("tools"):
            result["tools"] = self._chat_tools_to_response(data["tools"])
        if data.get("tool_choice"):
            tc = data["tool_choice"]
            if isinstance(tc, str):
                result["tool_choice"] = tc
            elif isinstance(tc, dict):
                result["tool_choice"] = tc
        if data.get("reasoning_effort") is not None:
            result["reasoning"] = {"effort": data["reasoning_effort"]}
        return result

    def _chat_response_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices", [])
        text = ""
        tool_calls = None
        reasoning_content = None
        finish_reason = "stop"
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls")
            reasoning_content = msg.get("reasoning_content")
            finish_reason = choices[0].get("finish_reason", "stop")

        upstream_id = data.get("id", "")
        response_id = self._make_response_id(upstream_id)
        output = []
        if text:
            output.append({
                "type": "message",
                "id": self._make_message_id(response_id, upstream_id),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            })
        if reasoning_content:
            output.append({
                "type": "reasoning",
                "id": f"rs_{response_id.removeprefix('resp_')}",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": reasoning_content}],
            })
        if tool_calls:
            for tc in tool_calls:
                call_id = tc.get("id", "")
                output.append({
                    "type": "function_call",
                    "id": self._make_function_call_id(call_id),
                    "call_id": call_id,
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                    "status": "completed",
                })

        status = "completed"
        incomplete_details = None
        if finish_reason == "length":
            status = "incomplete"
            incomplete_details = {"reason": "max_output_tokens"}
        elif finish_reason == "content_filter":
            status = "incomplete"
            incomplete_details = {"reason": "content_filter"}

        if not output:
            output.append({
                "type": "message",
                "id": self._make_message_id(response_id, upstream_id),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            })

        result = {
            "id": response_id,
            "object": "response",
            "created_at": data.get("created", 0),
            "model": data.get("model", ""),
            "status": status,
            "output": output,
            "output_text": text,
            "usage": self._chat_usage_to_response_usage(data.get("usage", {})),
        }
        if upstream_id and upstream_id != response_id:
            result["_upstream_id"] = upstream_id
        if incomplete_details:
            result["incomplete_details"] = incomplete_details
        return result

    # --- Anthropic → Response ---

    def _anthropic_request_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        input_items = []
        instructions = None
        system = data.get("system")
        if system:
            if isinstance(system, str):
                instructions = system
            elif isinstance(system, list):
                instructions = "\n".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in system
                )

        for msg in data.get("messages", []):
            content = msg.get("content", "")
            role = msg.get("role", "user")
            if isinstance(content, str):
                input_items.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = []
                for part in content:
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "tool_use":
                        input_items.append({
                            "type": "function_call",
                            "call_id": part.get("id", ""),
                            "name": part.get("name", ""),
                            "arguments": json.dumps(part.get("input", {})),
                        })
                    elif part.get("type") == "tool_result":
                        tr_content = part.get("content", "")
                        if isinstance(tr_content, list):
                            result_text = "\n".join(
                                c.get("text", "") for c in tr_content if c.get("type") == "text"
                            )
                        else:
                            result_text = str(tr_content) if tr_content else ""
                        input_items.append({
                            "type": "function_call_output",
                            "call_id": part.get("tool_use_id", ""),
                            "output": result_text,
                        })
                    else:
                        text_parts.append(str(part))
                if text_parts:
                    input_items.append({"role": role, "content": "\n".join(text_parts)})

        result = {
            "model": data.get("model", ""),
            "input": input_items,
            "stream": data.get("stream", False),
        }
        if instructions:
            result["instructions"] = instructions
        if data.get("max_tokens") is not None:
            result["max_output_tokens"] = data["max_tokens"]
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        if data.get("tools"):
            result["tools"] = self._anthropic_tools_to_response(data["tools"])
        if data.get("tool_choice"):
            tc = data["tool_choice"]
            if isinstance(tc, dict):
                if tc.get("type") == "auto":
                    result["tool_choice"] = "auto"
                elif tc.get("type") == "any":
                    result["tool_choice"] = "required"
                elif tc.get("type") == "tool":
                    result["tool_choice"] = {"type": "function", "name": tc.get("name", "")}
        if data.get("thinking") is not None:
            budget = data["thinking"].get("budget_tokens", 0) if isinstance(data["thinking"], dict) else 0
            if budget >= 10000:
                effort = "high"
            elif budget >= 4000:
                effort = "medium"
            else:
                effort = "low"
            result["reasoning"] = {"effort": effort}
        return result

    def _anthropic_tools_to_response(self, tools: list) -> list:
        response_tools = []
        for tool in tools:
            if "name" in tool:
                response_tools.append({
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                })
        return response_tools

    def _anthropic_response_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        output = []
        text = ""
        stop_reason = data.get("stop_reason", "end_turn")

        for part in data.get("content", []):
            if part.get("type") == "text":
                text += part.get("text", "")
            elif part.get("type") == "tool_use":
                output.append({
                    "type": "function_call",
                    "call_id": part.get("id", ""),
                    "name": part.get("name", ""),
                    "arguments": json.dumps(part.get("input", {})),
                })

        if text:
            output.insert(0, {
                "type": "message",
                "id": f"msg_{data.get('id', '')}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            })
        elif not output:
            output.append({
                "type": "message",
                "id": f"msg_{data.get('id', '')}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            })

        status = "completed"
        if stop_reason == "max_tokens":
            status = "incomplete"

        return {
            "id": f"resp_{data.get('id', '')}",
            "object": "response",
            "created_at": int(time.time()),
            "model": data.get("model", ""),
            "status": status,
            "output": output,
            "usage": {
                "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0),
            }
        }

    # --- Chat Completions 流式 → Response 流式 ---

    def _chat_stream_chunk_to_response(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        from loguru import logger
        if self._stream_state is None:
            self._reset_stream_state()
            logger.debug("[CHUNK] reset stream_state for first chunk")

        if chunk.get("id"):
            self._stream_state["response_id"] = chunk["id"]
            if not self._stream_state["message_id"]:
                self._stream_state["message_id"] = chunk["id"]
        if chunk.get("model"):
            self._stream_state["model"] = chunk["model"]
        if chunk.get("created") is not None:
            self._stream_state["created_at"] = chunk.get("created", 0)
        if self._stream_state["response_id"] and not self._stream_state["response_id"].startswith("resp_"):
            self._stream_state["response_id"] = self._make_response_id(self._stream_state["response_id"])
        if self._stream_state["message_id"] and not self._stream_state["message_id"].startswith("msg_"):
            self._stream_state["message_id"] = self._make_message_id(
                self._stream_state["response_id"] or "resp_stream",
                self._stream_state["message_id"],
            )

        def _next_sequence_number() -> int:
            self._stream_state["sequence_number"] += 1
            return self._stream_state["sequence_number"]

        # 提取 usage（可能在任何 chunk 中，包括 choices 为空的 usage-only chunk）
        usage = chunk.get("usage")
        if usage:
            self._stream_state["input_tokens"] = usage.get("prompt_tokens", self._stream_state["input_tokens"])
            self._stream_state["output_tokens"] = usage.get("completion_tokens", self._stream_state["output_tokens"])
            self._stream_state["total_tokens"] = usage.get("total_tokens", self._stream_state["total_tokens"])

        choices = chunk.get("choices", [])
        if not choices:
            return None
        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")

        def _make_created_event() -> dict[str, Any]:
            self._need_in_progress = True
            return {
                "type": "response.created",
                "response": {
                    "id": self._stream_state["response_id"],
                    "object": "response",
                    "status": "in_progress",
                    "model": self._stream_state["model"],
                    "output": [],
                },
            }

        def _ensure_created() -> bool:
            """确保 response.created 已发送，返回是否需要发送"""
            if not self._stream_state["response_created_sent"]:
                self._stream_state["response_created_sent"] = True
                if not self._stream_state["message_id"]:
                    self._stream_state["message_id"] = self._stream_state["response_id"]
                return True
            return False

        def _ensure_output_item_added() -> bool:
            """确保 response.output_item.added 已发送，返回是否需要发送"""
            if not self._stream_state["output_item_added_sent"]:
                self._stream_state["output_item_added_sent"] = True
                if self._stream_state["text_output_index"] is None:
                    idx = self._stream_state["output_index"]
                    self._stream_state["text_output_index"] = idx
                    self._stream_state["output_index"] = idx + 1
                return True
            return False

        # 处理第一个 chunk 带 role 的情况
        if delta.get("role") == "assistant":
            self._stream_state["response_created_sent"] = True
            self._stream_state["message_id"] = chunk.get("id", "")
            return _make_created_event()

        # 处理文本内容
        if delta.get("content") is not None:
            text = delta["content"]
            self._stream_state["accumulated_text"] += text
            if self._stream_state["text_output_index"] is None:
                self._stream_state["text_output_index"] = self._stream_state["output_index"]
            text_output_index = self._stream_state["text_output_index"]
            item_id = self._stream_state["message_id"]
            if not item_id.startswith("msg_"):
                item_id = self._make_message_id(self._stream_state["response_id"] or "resp_stream", item_id)
            event = {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "delta": text,
                "sequence_number": _next_sequence_number(),
            }
            pending_before_delta = []
            if _ensure_output_item_added():
                pending_before_delta.append({
                    "type": "response.output_item.added",
                    "output_index": text_output_index,
                    "item": {
                        "type": "message",
                        "id": item_id,
                        "role": "assistant",
                        "content": [],
                    },
                })
            if not self._stream_state["content_part_added_sent"]:
                self._stream_state["content_part_added_sent"] = True
                pending_before_delta.append({
                    "type": "response.content_part.added",
                    "item_id": item_id,
                    "output_index": text_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                })
            pending_before_delta.append(event)
            if _ensure_created():
                self._pending_extra_events = pending_before_delta
                return _make_created_event()
            if len(pending_before_delta) > 1:
                self._pending_extra_events = pending_before_delta[1:]
                return pending_before_delta[0]
            return event

        # 处理工具调用
        if delta.get("tool_calls"):
            events = []
            for tc in delta["tool_calls"]:
                call_id = tc.get("id", "")
                tc_index = tc.get("index")
                if not call_id and tc_index is not None:
                    call_id = self._stream_state["tool_call_index_to_id"].get(tc_index, "")
                function = tc.get("function", {})
                if function.get("name"):
                    name = function["name"]
                    if not call_id:
                        call_id = f"call_{tc_index}" if tc_index is not None else f"call_{len(self._stream_state['tool_calls'])}"
                    if tc_index is not None:
                        self._stream_state["tool_call_index_to_id"][tc_index] = call_id
                    self._stream_state["tool_calls"][call_id] = {"name": name, "arguments": "", "output_index": idx}
                    idx = self._stream_state["output_index"]
                    self._stream_state["output_index"] = idx + 1
                    self._stream_state["tool_calls"][call_id]["output_index"] = idx
                    events.append({
                        "type": "response.output_item.added",
                        "output_index": idx,
                        "item": {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": "",
                        },
                    })
                elif function.get("arguments") is not None:
                    args = function.get("arguments", "")
                    if args:
                        if call_id in self._stream_state["tool_calls"]:
                            self._stream_state["tool_calls"][call_id]["arguments"] += args
                        event = {
                            "type": "response.function_call_arguments.delta",
                            "delta": args,
                        }
                        if tc_index is not None:
                            event["output_index"] = self._stream_state["tool_calls"].get(call_id, {}).get("output_index", tc_index)
                        events.append(event)
            if events:
                if _ensure_created():
                    self._pending_extra_events = events
                    return _make_created_event()
                if len(events) == 1:
                    return events[0]
                result = dict(events[0])
                self._pending_extra_events = events[1:]
                return result

        # 处理推理内容
        if delta.get("reasoning_content") is not None:
            rc = delta["reasoning_content"]
            self._stream_state["reasoning_content"] += rc
            if not self._stream_state["reasoning_started"]:
                self._stream_state["reasoning_started"] = True
                self._stream_state["reasoning_id"] = f"rs_{chunk.get('id', '')}"
                idx = self._stream_state["output_index"]
                self._stream_state["output_index"] = idx + 1
                added_event = {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {
                        "type": "reasoning",
                        "id": self._stream_state["reasoning_id"],
                        "summary": [],
                    },
                }
                delta_event = {
                    "type": "response.reasoning_summary_text.delta",
                    "delta": rc,
                }
                if _ensure_created():
                    self._pending_extra_events = [added_event, delta_event]
                    return _make_created_event()
                self._pending_extra_events = [delta_event]
                return added_event
            event = {
                "type": "response.reasoning_summary_text.delta",
                "delta": rc,
            }
            if _ensure_created():
                self._pending_extra_events = [event]
                return _make_created_event()
            return event

        # 处理结束原因
        if finish_reason is not None:
            logger.debug(f"[CHUNK] finish_reason={finish_reason} response_created_sent={self._stream_state['response_created_sent']}")
            need_created = _ensure_created()
            done_events = self._build_final_events(finish_reason=finish_reason)
            logger.debug(f"[CHUNK] built {len(done_events)} final events, need_created={need_created}")

            if need_created:
                self._pending_extra_events = done_events
                return _make_created_event()

            if len(done_events) == 1:
                logger.debug(f"[CHUNK] returning single done_event: {done_events[0].get('type')}")
                return done_events[0]
            logger.debug(f"[CHUNK] returning first of {len(done_events)} events, rest in pending")
            self._pending_extra_events = done_events[1:]
            return done_events[0]

        return None

    # --- Anthropic 流式 → Response 流式 ---

    def _anthropic_stream_chunk_to_response(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if self._stream_state is None:
            self._reset_stream_state()

        event_type = chunk.get("type") or chunk.get("_event_type", "")

        if event_type == "message_start":
            msg = chunk.get("message", {})
            self._stream_state["message_id"] = msg.get("id", "")
            self._need_in_progress = True
            return {
                "type": "response.created",
                "response": {
                    "id": f"resp_{msg.get('id', '')}",
                    "object": "response",
                    "status": "in_progress",
                    "model": msg.get("model", ""),
                    "output": [],
                },
            }

        elif event_type == "content_block_start":
            content_block = chunk.get("content_block", {})
            if content_block.get("type") == "tool_use":
                return {
                    "type": "response.output_item.added",
                    "output_index": chunk.get("index", 0),
                    "item": {
                        "type": "function_call",
                        "call_id": content_block.get("id", ""),
                        "name": content_block.get("name", ""),
                        "arguments": "",
                    },
                }
            return None

        elif event_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                return {
                    "type": "response.output_text.delta",
                    "delta": delta.get("text", ""),
                }
            elif delta.get("type") == "input_json_delta":
                return {
                    "type": "response.function_call_arguments.delta",
                    "delta": delta.get("partial_json", ""),
                }
            elif delta.get("type") == "thinking_delta":
                if not self._stream_state["reasoning_started"]:
                    self._stream_state["reasoning_started"] = True
                    self._stream_state["reasoning_id"] = f"rs_{self._stream_state['message_id']}"
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
                    delta_event = {
                        "type": "response.reasoning_summary_text.delta",
                        "delta": delta.get("thinking", ""),
                    }
                    self._pending_extra_events = [delta_event]
                    return result
                return {
                    "type": "response.reasoning_summary_text.delta",
                    "delta": delta.get("thinking", ""),
                }
            return None

        elif event_type == "content_block_stop":
            return None

        elif event_type == "message_delta":
            stop_reason = chunk.get("delta", {}).get("stop_reason")
            status = "completed"
            if stop_reason == "max_tokens":
                status = "incomplete"
            return {
                "type": "response.completed",
                "response": {
                    "status": status,
                },
            }

        elif event_type == "message_stop":
            return None

        elif event_type == "ping":
            return None

        return None

    # --- 公共接口 ---

    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "openai-chat-completions":
            return self._chat_request_to_response(source_data)
        elif source_type == "anthropic":
            return self._anthropic_request_to_response(source_data)
        return source_data

    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "openai-chat-completions":
            return self._chat_response_to_response(target_response)
        elif source_type == "anthropic":
            return self._anthropic_response_to_response(target_response)
        return target_response

    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> dict[str, Any] | None:
        if source_type == "openai-chat-completions":
            return self._chat_stream_chunk_to_response(chunk)
        elif source_type == "anthropic":
            return self._anthropic_stream_chunk_to_response(chunk)
        return chunk

    def get_extra_events(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        from loguru import logger
        # 从实例变量获取额外事件
        events = self._pending_extra_events
        self._pending_extra_events = []  # 清空，避免重复发送
        logger.debug(f"[GET_EXTRA_EVENTS] returning {len(events)} events, types={[e.get('type') for e in events]}")
        # 在 response.created 之后注入 response.in_progress
        if self._need_in_progress:
            self._need_in_progress = False
            in_progress = {
                "type": "response.in_progress",
                "response": {
                    "id": self._stream_state.get("response_id", ""),
                    "object": "response",
                    "status": "in_progress",
                    "model": "",
                    "output": [],
                },
            }
            events.insert(0, in_progress)
        return events

    def finalize_stream(self, source_type: str = "") -> list[dict[str, Any]]:
        from loguru import logger
        logger.debug(f"[FINALIZE] source_type={source_type} stream_state={self._stream_state is not None}")
        if source_type != "openai-chat-completions" or self._stream_state is None:
            return []
        if self._stream_state["completed_sent"]:
            logger.debug("[FINALIZE] already completed, skipping")
            return []
        # 如果 response_id 为空，生成一个默认的
        if not self._stream_state["response_id"]:
            self._stream_state["response_id"] = f"resp_{secrets.token_hex(12)}"
            logger.debug(f"[FINALIZE] generated response_id={self._stream_state['response_id']}")
        if not self._stream_state["message_id"]:
            self._stream_state["message_id"] = self._stream_state["response_id"]
        if self._stream_state["message_id"] and not self._stream_state["message_id"].startswith("msg_"):
            self._stream_state["message_id"] = self._make_message_id(
                self._stream_state["response_id"],
                self._stream_state["message_id"],
            )
        logger.debug(f"[FINALIZE] accumulated_text={repr(self._stream_state['accumulated_text'][:100])} response_created_sent={self._stream_state['response_created_sent']}")
        return self._build_final_events(finish_reason="stop")

    def _build_output_items(self) -> list[dict[str, Any]]:
        output = []
        if self._stream_state["accumulated_text"]:
            item_id = self._stream_state["message_id"]
            if not item_id.startswith("msg_"):
                item_id = self._make_message_id(self._stream_state["response_id"] or "resp_stream", item_id)
            output.append({
                "type": "message",
                "id": item_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self._stream_state["accumulated_text"]}],
            })
        for call_id, tc_data in self._stream_state["tool_calls"].items():
            output.append({
                "type": "function_call",
                "id": self._make_function_call_id(call_id),
                "call_id": call_id,
                "name": tc_data["name"],
                "arguments": tc_data["arguments"],
                "status": "completed",
            })
        if self._stream_state["reasoning_content"]:
            output.append({
                "type": "reasoning",
                "id": self._stream_state["reasoning_id"],
                "summary": [],
                "content": [{"type": "reasoning_text", "text": self._stream_state["reasoning_content"]}],
            })
        if not output:
            item_id = self._stream_state["message_id"]
            if not item_id.startswith("msg_"):
                item_id = self._make_message_id(self._stream_state["response_id"] or "resp_stream", item_id)
            output.append({
                "type": "message",
                "id": item_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            })
        return output

    def _build_final_events(self, finish_reason: str) -> list[dict[str, Any]]:
        if self._stream_state["completed_sent"]:
            return []

        output = self._build_output_items()
        status = "completed" if finish_reason != "length" else "incomplete"
        usage_data = {
            "input_tokens": self._stream_state["input_tokens"],
            "output_tokens": self._stream_state["output_tokens"],
            "total_tokens": self._stream_state["total_tokens"],
        }
        completed_event = {
            "type": "response.completed",
            "response": {
                "id": self._stream_state["response_id"],
                "object": "response",
                "created_at": self._stream_state["created_at"],
                "model": self._stream_state["model"],
                "status": status,
                "output": output,
                "usage": usage_data,
            },
        }

        done_events = []
        if self._stream_state["output_item_added_sent"]:
            item_id = self._stream_state["message_id"]
            if not item_id.startswith("msg_"):
                item_id = self._make_message_id(self._stream_state["response_id"] or "resp_stream", item_id)
            text_output_index = self._stream_state["text_output_index"]
            if text_output_index is None:
                text_output_index = 0
            if self._stream_state["accumulated_text"]:
                done_events.append({
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": text_output_index,
                    "content_index": 0,
                    "text": self._stream_state["accumulated_text"],
                })
                done_events.append({
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": text_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": self._stream_state["accumulated_text"]},
                })
            done_events.append({
                "type": "response.output_item.done",
                "output_index": text_output_index,
                "item": output[0] if output else {},
            })
        for call_id, tc_data in self._stream_state["tool_calls"].items():
            done_events.append({
                "type": "response.function_call_arguments.done",
                "item_id": self._make_function_call_id(call_id),
                "output_index": tc_data.get("output_index", 0),
                "arguments": tc_data.get("arguments", ""),
            })
        done_events.append(completed_event)
        self._stream_state["completed_sent"] = True
        return done_events
