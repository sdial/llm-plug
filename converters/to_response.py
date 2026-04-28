"""
将其他格式转换为 OpenAI Response 格式
"""
import json
import time
from typing import Any

from converters.base import BaseConverter


class ToResponseConverter(BaseConverter):
    """任意格式 → OpenAI Response"""

    def __init__(self):
        self._stream_state: dict[str, Any] | None = None

    def _reset_stream_state(self):
        self._stream_state = {
            "reasoning_started": False,
            "reasoning_id": "",
            "message_id": "",
        }

    # --- Chat Completions → Response ---

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
        if data.get("max_tokens"):
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

        output = []
        if text:
            output.append({
                "type": "message",
                "id": f"msg_{data.get('id', '')}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            })
        if reasoning_content:
            output.append({
                "type": "reasoning",
                "id": f"rs_{data.get('id', '')}",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": reasoning_content}],
            })
        if tool_calls:
            for tc in tool_calls:
                output.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                })

        status = "completed"
        if finish_reason == "length":
            status = "incomplete"

        if not output:
            output.append({
                "type": "message",
                "id": f"msg_{data.get('id', '')}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            })

        return {
            "id": data.get("id", ""),
            "object": "response",
            "created_at": data.get("created", 0),
            "model": data.get("model", ""),
            "status": status,
            "output": output,
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
                "total_tokens": data.get("usage", {}).get("total_tokens", 0),
            }
        }

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
        if data.get("max_tokens"):
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
            result["reasoning"] = {"effort": "high"}
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
        if self._stream_state is None:
            self._reset_stream_state()

        choices = chunk.get("choices", [])
        if not choices:
            return None
        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")

        if delta.get("role") == "assistant":
            return {
                "type": "response.created",
                "response": {
                    "id": chunk.get("id", ""),
                    "object": "response",
                    "status": "in_progress",
                    "model": chunk.get("model", ""),
                    "output": [],
                },
            }

        if delta.get("content") is not None:
            return {
                "type": "response.output_text.delta",
                "delta": delta["content"],
            }

        if delta.get("tool_calls"):
            events = []
            for tc in delta["tool_calls"]:
                if tc.get("function", {}).get("name"):
                    events.append({
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": tc["function"]["name"],
                            "arguments": "",
                        },
                    })
                elif tc.get("function", {}).get("arguments") is not None:
                    args = tc["function"].get("arguments", "")
                    if args:
                        events.append({
                            "type": "response.function_call_arguments.delta",
                            "delta": args,
                        })
            if events:
                if len(events) == 1:
                    return events[0]
                result = events[0]
                result["_extra_events"] = events[1:]
                return result

        if delta.get("reasoning_content") is not None:
            if not self._stream_state["reasoning_started"]:
                self._stream_state["reasoning_started"] = True
                self._stream_state["reasoning_id"] = f"rs_{chunk.get('id', '')}"
                result = {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "reasoning",
                        "id": self._stream_state["reasoning_id"],
                        "summary": [],
                    },
                }
                delta_event = {
                    "type": "response.reasoning_summary_text.delta",
                    "delta": delta["reasoning_content"],
                }
                result["_extra_events"] = [delta_event]
                return result
            return {
                "type": "response.reasoning_summary_text.delta",
                "delta": delta["reasoning_content"],
            }

        if finish_reason is not None:
            return {
                "type": "response.completed",
                "response": {
                    "id": chunk.get("id", ""),
                    "object": "response",
                    "status": "completed" if finish_reason != "length" else "incomplete",
                    "model": chunk.get("model", ""),
                },
            }

        return None

    # --- Anthropic 流式 → Response 流式 ---

    def _anthropic_stream_chunk_to_response(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if self._stream_state is None:
            self._reset_stream_state()

        event_type = chunk.get("type") or chunk.get("_event_type", "")

        if event_type == "message_start":
            msg = chunk.get("message", {})
            self._stream_state["message_id"] = msg.get("id", "")
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
                    result = {
                        "type": "response.output_item.added",
                        "output_index": 0,
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
                    result["_extra_events"] = [delta_event]
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
        if isinstance(chunk, dict) and chunk.get("_extra_events"):
            return chunk["_extra_events"]
        return []
