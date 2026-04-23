"""
将其他格式转换为 OpenAI Response 格式
"""
import time
from typing import Any

from converters.base import BaseConverter


class ToResponseConverter(BaseConverter):
    """任意格式 → OpenAI Response"""

    # --- Chat Completions → Response ---

    def _chat_request_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        input_items = []
        instructions = None
        for msg in data.get("messages", []):
            if msg["role"] == "system":
                instructions = msg.get("content", "")
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
        return result

    def _chat_response_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices", [])
        text = ""
        if choices:
            text = choices[0].get("message", {}).get("content", "")

        return {
            "id": data.get("id", ""),
            "object": "response",
            "created_at": data.get("created", 0),
            "model": data.get("model", ""),
            "status": "completed",
            "output": [{
                "type": "message",
                "id": f"msg_{data.get('id', '')}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
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
            if isinstance(content, str):
                input_items.append({"role": msg["role"], "content": content})
            elif isinstance(content, list):
                input_items.append({"role": msg["role"], "content": content})

        result = {
            "model": data.get("model", ""),
            "input": input_items,
            "stream": data.get("stream", False),
        }
        if instructions:
            result["instructions"] = instructions
        if data.get("max_tokens"):
            result["max_output_tokens"] = data["max_tokens"]
        return result

    def _anthropic_response_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        text = ""
        for part in data.get("content", []):
            if part.get("type") == "text":
                text += part.get("text", "")

        return {
            "id": f"resp_{data.get('id', '')}",
            "object": "response",
            "created_at": int(time.time()),
            "model": data.get("model", ""),
            "status": "completed",
            "output": [{
                "type": "message",
                "id": f"msg_{data.get('id', '')}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
            "usage": {
                "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0),
            }
        }

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
        # Response格式的流式转换较复杂，暂返回原始数据
        return chunk
