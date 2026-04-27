"""
将其他格式转换为 Anthropic Messages 格式
"""
import json
from typing import Any

from converters.base import BaseConverter


class ToAnthropicConverter(BaseConverter):
    """任意格式 → Anthropic Messages"""

    # --- Chat Completions → Anthropic ---

    def _chat_request_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        system = None
        messages = []
        for msg in data.get("messages", []):
            if msg["role"] == "system":
                system = msg.get("content", "")
            else:
                messages.append({
                    "role": msg["role"],
                    "content": msg.get("content", ""),
                })

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

        # 处理 thinking 参数
        if data.get("thinking") is not None:
            result["thinking"] = data["thinking"]
        elif data.get("enable_thinking"):
            result["thinking"] = {"type": "enabled", "budget_tokens": 4096}

        # 处理 reasoning_effort 参数 (OpenAI low/medium/high 或数字字符串)
        reasoning_effort = data.get("reasoning_effort")
        if reasoning_effort is not None:
            if isinstance(reasoning_effort, int) or (isinstance(reasoning_effort, str) and reasoning_effort.isdigit()):
                budget = int(reasoning_effort)
            else:
                budget = {"low": 1024, "medium": 4096, "high": 16384}.get(reasoning_effort, 4096)
            result["thinking"] = {"type": "enabled", "budget_tokens": budget}

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
            }
        }

    def _chat_stream_chunk_to_anthropic(self, chunk: dict[str, Any]) -> dict[str, Any] | None:
        choices = chunk.get("choices", [])
        if not choices:
            return None
        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")

        if delta.get("role") == "assistant":
            return {
                "type": "message_start",
                "message": {
                    "id": chunk.get("id", "").replace("chatcmpl-", "msg_"),
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": chunk.get("model", ""),
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            }

        if delta.get("content") is not None:
            return {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": delta["content"]},
            }

        if delta.get("tool_calls"):
            tc = delta["tool_calls"][0]
            if tc.get("function", {}).get("name"):
                return {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc["function"]["name"],
                    }
                }
            elif tc.get("function", {}).get("arguments"):
                return {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": tc["function"]["arguments"]},
                }

        if finish_reason is not None:
            stop_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
            return {
                "type": "message_delta",
                "delta": {"stop_reason": stop_map.get(finish_reason, "end_turn"), "stop_sequence": None},
                "usage": {"output_tokens": 0},
            }

        return None

    # --- OpenAI Response → Anthropic ---

    def _response_request_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        system = data.get("instructions")
        messages = []
        for item in data.get("input", []):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})

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
        return result

    def _response_response_to_anthropic(self, data: dict[str, Any]) -> dict[str, Any]:
        text = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text += content.get("text", "")

        return {
            "id": f"msg_{data.get('id', '')}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": data.get("model", ""),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("usage", {}).get("output_tokens", 0),
            }
        }

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
            return self._chat_stream_chunk_to_anthropic(chunk)
        return chunk
