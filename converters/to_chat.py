"""
将其他格式转换为 OpenAI Chat Completions 格式
"""
import json
from typing import Any

from converters.base import BaseConverter


class ToChatCompletionsConverter(BaseConverter):
    """任意格式 → OpenAI Chat Completions"""

    def __init__(self):
        self._stream_state: dict[str, Any] | None = None

    def _reset_stream_state(self):
        self._stream_state = {
            "msg_id": "chatcmpl",
            "model": "",
            "tool_call_index": 0,
            "content_block_to_tc_index": {},  # Anthropic content block index → OpenAI tool_call index
            "output_index_to_tc_index": {},   # Response output_index → OpenAI tool_call index
        }

    # --- Anthropic → Chat Completions ---

    def _anthropic_request_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        messages = []
        system = data.get("system")
        if system:
            if isinstance(system, str):
                messages.append({"role": "system", "content": system})
            elif isinstance(system, list):
                text_parts = []
                for part in system:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part["text"])
                    elif isinstance(part, str):
                        text_parts.append(part)
                if text_parts:
                    messages.append({"role": "system", "content": "\n".join(text_parts)})

        for msg in data.get("messages", []):
            role = msg["role"]
            content = msg.get("content", "")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []
                tool_result_parts = []
                image_parts = []
                for part in content:
                    if part.get("type") == "text":
                        text_parts.append(part["text"])
                    elif part.get("type") == "image" and part.get("source", {}).get("type") == "base64":
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{part['source'].get('media_type', 'image/png')};base64,{part['source']['data']}"
                            }
                        })
                    elif part.get("type") == "tool_use":
                        tool_calls.append({
                            "id": part.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": part.get("name", ""),
                                "arguments": json.dumps(part.get("input", {})) if isinstance(part.get("input"), dict) else (part.get("input", "") or ""),
                            }
                        })
                    elif part.get("type") == "tool_result":
                        tool_result_parts.append(part)

                if role == "assistant":
                    assistant_msg = {"role": "assistant", "content": None}
                    if text_parts:
                        assistant_msg["content"] = "\n".join(text_parts)
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                    messages.append(assistant_msg)
                elif role == "user":
                    for tr in tool_result_parts:
                        tool_result_content = tr.get("content", "")
                        if isinstance(tool_result_content, list):
                            result_text = "\n".join(
                                c.get("text", "") for c in tool_result_content if c.get("type") == "text"
                            )
                        else:
                            result_text = str(tool_result_content) if tool_result_content else ""
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": result_text,
                        })
                    user_parts = []
                    for t in text_parts:
                        user_parts.append({"type": "text", "text": t})
                    user_parts.extend(image_parts)
                    if user_parts:
                        if len(user_parts) == 1 and user_parts[0].get("type") == "text":
                            messages.append({"role": "user", "content": user_parts[0]["text"]})
                        else:
                            messages.append({"role": "user", "content": user_parts})
                else:
                    fallback_content = " ".join(text_parts) if text_parts else ""
                    messages.append({"role": role, "content": fallback_content})

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
        }
        if data.get("max_tokens"):
            result["max_tokens"] = data["max_tokens"]
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        if data.get("stop_sequences"):
            result["stop"] = data["stop_sequences"]
        if data.get("tools"):
            result["tools"] = self._anthropic_tools_to_openai(data["tools"])
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
        thinking = data.get("thinking")
        if thinking:
            if isinstance(thinking, dict):
                if thinking.get("type") == "enabled":
                    budget = thinking.get("budget_tokens", 0)
                    result["reasoning_effort"] = budget
                elif thinking.get("type") == "adaptive":
                    result["reasoning_effort"] = "medium"
        return result

    def _anthropic_tools_to_openai(self, tools: list) -> list:
        openai_tools = []
        for tool in tools:
            if tool.get("type") == "custom" or "name" in tool:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    }
                })
        return openai_tools

    def _anthropic_response_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        content = data.get("content", [])
        message_content = ""
        tool_calls = []
        reasoning_content = ""
        for part in content:
            if part.get("type") == "text":
                message_content += part.get("text", "")
            elif part.get("type") == "thinking":
                reasoning_content += part.get("thinking", "")
            elif part.get("type") == "tool_use":
                tool_calls.append({
                    "id": part.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": part.get("name", ""),
                        "arguments": part.get("input", {}),
                    }
                })

        message = {"role": "assistant", "content": message_content}
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        if tool_calls:
            for tc in tool_calls:
                if isinstance(tc["function"]["arguments"], dict):
                    tc["function"]["arguments"] = json.dumps(tc["function"]["arguments"])
            message["tool_calls"] = tool_calls

        result = {
            "id": f"chatcmpl-{data.get('id', '')}",
            "object": "chat.completion",
            "created": data.get("created", 0),
            "model": data.get("model", ""),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self._map_stop_reason(data.get("stop_reason")),
            }],
            "usage": {
                "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0),
            }
        }
        return result

    def _map_stop_reason(self, reason: str | None) -> str:
        mapping = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
        }
        return mapping.get(reason, "stop")

    def _anthropic_stream_chunk_to_chat(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if self._stream_state is None:
            self._reset_stream_state()

        event_type = chunk.get("type") or chunk.get("_event_type", "")

        if event_type == "message_start":
            msg = chunk.get("message", {})
            self._stream_state["msg_id"] = f"chatcmpl-{msg.get('id', '')}"
            self._stream_state["model"] = msg.get("model", "")
            self._stream_state["tool_call_index"] = 0
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }

        elif event_type == "content_block_start":
            content_block = chunk.get("content_block", {})
            if content_block.get("type") == "tool_use":
                tc_idx = self._stream_state["tool_call_index"]
                self._stream_state["tool_call_index"] = tc_idx + 1
                self._stream_state["content_block_to_tc_index"][chunk.get("index", 0)] = tc_idx
                return {
                    "id": self._stream_state["msg_id"],
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._stream_state["model"],
                    "choices": [{"index": 0, "delta": {"tool_calls": [{"index": tc_idx, "id": content_block.get("id", ""), "type": "function", "function": {"name": content_block.get("name", ""), "arguments": ""}}]}, "finish_reason": None}],
                }
            elif content_block.get("type") == "thinking":
                return None
            else:
                return {
                    "id": self._stream_state["msg_id"],
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._stream_state["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                }

        elif event_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                return {
                    "id": self._stream_state["msg_id"],
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._stream_state["model"],
                    "choices": [{"index": 0, "delta": {"content": delta.get("text", "")}, "finish_reason": None}],
                }
            elif delta.get("type") == "thinking_delta":
                return {
                    "id": self._stream_state["msg_id"],
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._stream_state["model"],
                    "choices": [{"index": 0, "delta": {"reasoning_content": delta.get("thinking", "")}, "finish_reason": None}],
                }
            elif delta.get("type") == "input_json_delta":
                block_index = chunk.get("index", 0)
                tc_idx = self._stream_state["content_block_to_tc_index"].get(block_index, block_index)
                return {
                    "id": self._stream_state["msg_id"],
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._stream_state["model"],
                    "choices": [{"index": 0, "delta": {"tool_calls": [{"index": tc_idx, "function": {"arguments": delta.get("partial_json", "")}}]}, "finish_reason": None}],
                }

        elif event_type == "content_block_stop":
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
            }

        elif event_type == "message_delta":
            stop_reason = chunk.get("delta", {}).get("stop_reason")
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": self._map_stop_reason(stop_reason)}],
            }

        elif event_type == "message_stop":
            return None

        elif event_type == "ping":
            return None

        return None

    # --- OpenAI Response → Chat Completions ---

    def _response_tools_to_chat(self, tools: list) -> list:
        chat_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                chat_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    }
                })
        return chat_tools

    def _response_request_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        messages = []
        instructions = data.get("instructions")
        if instructions:
            messages.append({"role": "system", "content": instructions})

        for item in data.get("input", []):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                item_type = item.get("type", "")
                role = item.get("role", "user")
                if item_type == "function_call_output":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "content": item.get("output", ""),
                    })
                elif item_type == "function_call":
                    messages.append({
                        "role": "assistant",
                        "tool_calls": [{
                            "id": item.get("call_id", item.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            }
                        }],
                        "content": None,
                    })
                else:
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            c.get("text", "") if isinstance(c, dict) and c.get("type") == "input_text"
                            else (str(c) if not isinstance(c, dict) else "")
                            for c in content
                        )
                    messages.append({"role": role, "content": content})

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
        }
        if data.get("max_output_tokens"):
            result["max_tokens"] = data["max_output_tokens"]
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        if data.get("tools"):
            result["tools"] = self._response_tools_to_chat(data["tools"])
        if data.get("tool_choice"):
            tc = data["tool_choice"]
            if isinstance(tc, str):
                result["tool_choice"] = tc
            elif isinstance(tc, dict):
                if tc.get("type") == "function":
                    result["tool_choice"] = tc
                elif tc.get("type") == "auto":
                    result["tool_choice"] = "auto"
                elif tc.get("type") == "required":
                    result["tool_choice"] = "required"
        return result

    def _response_response_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        output_items = data.get("output", [])
        message_content = ""
        tool_calls = []
        for item in output_items:
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        message_content += content.get("text", "")
            elif item.get("type") == "function_call":
                tool_calls.append({
                    "id": item.get("call_id", item.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    }
                })

        message = {"role": "assistant", "content": message_content or None}
        if tool_calls:
            message["tool_calls"] = tool_calls

        finish_reason = "tool_calls" if tool_calls else "stop"
        if data.get("status") == "incomplete":
            finish_reason = "length"

        result = {
            "id": data.get("id", ""),
            "object": "chat.completion",
            "created": data.get("created_at", 0),
            "model": data.get("model", ""),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0),
            }
        }
        return result

    # --- OpenAI Response 流式 → Chat Completions 流式 ---

    def _response_stream_chunk_to_chat(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if self._stream_state is None:
            self._reset_stream_state()

        event_type = chunk.get("type", "")

        if event_type == "response.created":
            resp = chunk.get("response", {})
            self._stream_state["msg_id"] = f"chatcmpl-{resp.get('id', '')}"
            self._stream_state["model"] = resp.get("model", "")
            self._stream_state["tool_call_index"] = 0
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }

        elif event_type == "response.output_item.added":
            item = chunk.get("item", {})
            if item.get("type") == "function_call":
                tc_idx = self._stream_state["tool_call_index"]
                self._stream_state["tool_call_index"] = tc_idx + 1
                output_index = chunk.get("output_index", 0)
                self._stream_state["output_index_to_tc_index"][output_index] = tc_idx
                return {
                    "id": self._stream_state["msg_id"],
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._stream_state["model"],
                    "choices": [{"index": 0, "delta": {"tool_calls": [{"index": tc_idx, "id": item.get("call_id", ""), "type": "function", "function": {"name": item.get("name", ""), "arguments": ""}}]}, "finish_reason": None}],
                }
            return None

        elif event_type == "response.output_text.delta":
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [{"index": 0, "delta": {"content": chunk.get("delta", "")}, "finish_reason": None}],
            }

        elif event_type == "response.function_call_arguments.delta":
            output_index = chunk.get("output_index", 0)
            tc_idx = self._stream_state["output_index_to_tc_index"].get(output_index, 0)
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [{"index": 0, "delta": {"tool_calls": [{"index": tc_idx, "function": {"arguments": chunk.get("delta", "")}}]}, "finish_reason": None}],
            }

        elif event_type == "response.completed":
            resp = chunk.get("response", {})
            finish_reason = "stop"
            if resp.get("status") == "incomplete":
                finish_reason = "length"
            else:
                for item in resp.get("output", []):
                    if item.get("type") == "function_call":
                        finish_reason = "tool_calls"
                        break
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }

        return None

    # --- 公共接口 ---

    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "anthropic":
            return self._anthropic_request_to_chat(source_data)
        elif source_type == "openai-response":
            return self._response_request_to_chat(source_data)
        return source_data

    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        if source_type == "anthropic":
            return self._anthropic_response_to_chat(target_response)
        elif source_type == "openai-response":
            return self._response_response_to_chat(target_response)
        return target_response

    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> dict[str, Any] | None:
        if source_type == "anthropic":
            return self._anthropic_stream_chunk_to_chat(chunk)
        elif source_type == "openai-response":
            return self._response_stream_chunk_to_chat(chunk)
        return chunk
