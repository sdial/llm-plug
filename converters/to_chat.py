"""
将其他格式转换为 OpenAI Chat Completions 格式
"""
import copy
from typing import Any

from converters.base import BaseConverter


class ToChatCompletionsConverter(BaseConverter):
    """任意格式 → OpenAI Chat Completions"""

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
                # 多模态内容
                parts = []
                for part in content:
                    if part.get("type") == "text":
                        parts.append({"type": "text", "text": part["text"]})
                    elif part.get("type") == "image" and part.get("source", {}).get("type") == "base64":
                        parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{part['source'].get('media_type', 'image/png')};base64,{part['source']['data']}"
                            }
                        })
                    elif part.get("type") == "tool_use":
                        parts.append(part)
                    elif part.get("type") == "tool_result":
                        parts.append(part)
                messages.append({"role": role, "content": parts})

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
        for part in content:
            if part.get("type") == "text":
                message_content += part.get("text", "")
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
        if tool_calls:
            # 确保arguments是字符串
            for tc in tool_calls:
                if isinstance(tc["function"]["arguments"], dict):
                    import json
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
        event_type = chunk.get("type", "")
        if event_type == "message_start":
            return {
                "id": f"chatcmpl-{chunk.get('message', {}).get('id', '')}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": chunk.get("message", {}).get("model", ""),
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        elif event_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                return {
                    "id": "chatcmpl",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "",
                    "choices": [{"index": 0, "delta": {"content": delta.get("text", "")}, "finish_reason": None}],
                }
            elif delta.get("type") == "input_json_delta":
                return {
                    "id": "chatcmpl",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "",
                    "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": delta.get("partial_json", "")}}]}, "finish_reason": None}],
                }
        elif event_type == "message_delta":
            stop_reason = chunk.get("delta", {}).get("stop_reason")
            return {
                "id": "chatcmpl",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "",
                "choices": [{"index": 0, "delta": {}, "finish_reason": self._map_stop_reason(stop_reason)}],
            }
        elif event_type == "content_block_start":
            content_block = chunk.get("content_block", {})
            if content_block.get("type") == "tool_use":
                return {
                    "id": "chatcmpl",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "",
                    "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": content_block.get("id", ""), "type": "function", "function": {"name": content_block.get("name", ""), "arguments": ""}}]}, "finish_reason": None}],
                }
        return None

    # --- OpenAI Response → Chat Completions ---

    def _response_request_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        messages = []
        instructions = data.get("instructions")
        if instructions:
            messages.append({"role": "system", "content": instructions})

        for item in data.get("input", []):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content", "")
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
        return result

    def _response_response_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        output_items = data.get("output", [])
        message_content = ""
        for item in output_items:
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        message_content += content.get("text", "")

        result = {
            "id": data.get("id", ""),
            "object": "chat.completion",
            "created": 0,
            "model": data.get("model", ""),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": message_content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0),
            }
        }
        return result

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
        return chunk
