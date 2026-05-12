"""
将其他格式转换为 OpenAI Chat Completions 格式
"""
import json
import time
from typing import Any

from loguru import logger

from converters.base import BaseConverter


HOSTED_RESPONSE_TOOL_TYPES = {
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "computer_use",
    "image_generation",
    "mcp",
}

HOSTED_RESPONSE_INPUT_ITEM_TYPES = {
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "computer_call",
    "image_generation_call",
}

UNSUPPORTED_RESPONSE_REQUEST_FIELDS = {
    "background",
    "conversation",
    "context_management",
}


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

    @staticmethod
    def _serialize_tool_arguments(value: Any) -> str:
        if value is None:
            value = {}
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _tool_result_to_chat_message(part: dict[str, Any]) -> dict[str, Any]:
        tool_result_content = part.get("content", "")
        if isinstance(tool_result_content, list):
            text_parts = []
            for item in tool_result_content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif "text" in item:
                        text_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
            result_text = "\n".join(text_parts)
        else:
            result_text = str(tool_result_content) if tool_result_content else ""

        if part.get("is_error", False):
            result_text = f"[ERROR] {result_text}"

        return {
            "role": "tool",
            "tool_call_id": part.get("tool_use_id") or part.get("tool_call_id", ""),
            "content": result_text,
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
                reasoning_parts = []
                for part in content:
                    if part.get("type") == "text":
                        text_parts.append(part["text"])
                    elif part.get("type") == "image":
                        source = part.get("source", {})
                        if source.get("type") == "base64":
                            image_parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{source.get('media_type', 'image/png')};base64,{source['data']}"
                                }
                            })
                        elif source.get("type") == "url":
                            image_parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": source.get("url", "")
                                }
                            })
                    elif part.get("type") == "document":
                        # Anthropic document -> 转为文本标记
                        doc_source = part.get("source", {})
                        if doc_source.get("type") == "base64":
                            # 将 document 标记为文本，保留类型信息
                            text_parts.append(f"[DOCUMENT: {doc_source.get('media_type', 'application/pdf')}]")
                        elif doc_source.get("type") == "url":
                            text_parts.append(f"[DOCUMENT URL: {doc_source.get('url', '')}]")
                        elif doc_source.get("type") == "content":
                            # 直接内容
                            doc_content = doc_source.get("content", "")
                            if isinstance(doc_content, str):
                                text_parts.append(doc_content)
                    elif part.get("type") == "redacted_thinking":
                        # redacted_thinking -> 跳过或标记
                        # 已编辑的思考块无法显示内容，跳过
                        pass
                    elif part.get("type") == "search_result":
                        # search_result -> 转为文本
                        search_content = part.get("content", "")
                        if isinstance(search_content, str):
                            text_parts.append(f"[SEARCH_RESULT] {search_content}")
                        elif isinstance(search_content, list):
                            for sc in search_content:
                                if isinstance(sc, dict) and sc.get("type") == "text":
                                    text_parts.append(f"[SEARCH_RESULT] {sc.get('text', '')}")
                    elif part.get("type") == "tool_use":
                        tool_calls.append({
                            "id": part.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": part.get("name", ""),
                                "arguments": self._serialize_tool_arguments(part.get("input", {})),
                            }
                        })
                    elif part.get("type") == "tool_result":
                        tool_result_parts.append(part)
                    elif part.get("type") == "thinking":
                        reasoning_parts.append(part.get("thinking", ""))
                    else:
                        # 未知类型，尝试提取文本或标记
                        if "text" in part:
                            text_parts.append(part["text"])
                        else:
                            logger.debug("Unknown Anthropic content block type: %s", part.get("type"))

                if role == "assistant":
                    assistant_msg = {"role": "assistant"}
                    if text_parts:
                        assistant_msg["content"] = "\n".join(text_parts)
                    elif reasoning_parts and not tool_calls:
                        assistant_msg["content"] = ""
                    else:
                        assistant_msg["content"] = None
                    if reasoning_parts:
                        assistant_msg["reasoning_content"] = "\n".join(reasoning_parts)
                    if tool_calls:
                        assistant_msg["tool_calls"] = tool_calls
                    messages.append(assistant_msg)
                elif role == "user":
                    for tr in tool_result_parts:
                        messages.append(self._tool_result_to_chat_message(tr))
                    user_parts = []
                    for t in text_parts:
                        user_parts.append({"type": "text", "text": t})
                    user_parts.extend(image_parts)
                    if user_parts:
                        if len(user_parts) == 1 and user_parts[0].get("type") == "text":
                            messages.append({"role": "user", "content": user_parts[0]["text"]})
                        else:
                            messages.append({"role": "user", "content": user_parts})
                elif role == "tool" and tool_result_parts:
                    for tr in tool_result_parts:
                        messages.append(self._tool_result_to_chat_message(tr))
                else:
                    fallback_content = " ".join(text_parts) if text_parts else ""
                    messages.append({"role": role, "content": fallback_content})

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
        }
        if data.get("max_tokens") is not None:
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
                    # 将 budget_tokens 映射为语义合理的 reasoning_effort 值
                    if budget <= 0:
                        result["reasoning_effort"] = "low"
                    elif budget <= 2048:
                        result["reasoning_effort"] = "low"
                    elif budget <= 8192:
                        result["reasoning_effort"] = "medium"
                    else:
                        result["reasoning_effort"] = "high"
                    result["enable_thinking"] = True
                elif thinking.get("type") == "adaptive":
                    result["reasoning_effort"] = "medium"
                    result["enable_thinking"] = True

        # metadata/user_id 处理：Anthropic metadata.user_id -> OpenAI user
        metadata = data.get("metadata")
        if metadata:
            if isinstance(metadata, dict) and metadata.get("user_id"):
                result["user"] = metadata["user_id"]
            # 其他 metadata 字段可透传
            result["metadata"] = metadata

        # Anthropic 独有参数警告（OpenAI 不支持）
        unsupported_params = []
        if data.get("top_k") is not None and data["top_k"] != 0:
            unsupported_params.append("top_k")

        # 递归检查 cache_control（可能在 system、messages、content blocks 上）
        has_cache_control = data.get("cache_control") is not None
        if not has_cache_control:
            for part in (system if isinstance(system, list) else []):
                if isinstance(part, dict) and part.get("cache_control"):
                    has_cache_control = True
                    break
        if not has_cache_control:
            for msg in data.get("messages", []):
                content = msg.get("content", "")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("cache_control"):
                            has_cache_control = True
                            break
                if has_cache_control:
                    break
        if has_cache_control:
            unsupported_params.append("cache_control")

        if unsupported_params:
            logger.debug(
                "Anthropic parameters not supported by OpenAI, will be ignored: %s",
                ", ".join(unsupported_params)
            )

        return result

    def _anthropic_tools_to_openai(self, tools: list) -> list:
        openai_tools = []
        for tool in tools:
            tool_type = tool.get("type")
            # Anthropic tools 规范：type 可省略（默认即工具），或为 "custom"
            # 必须同时包含 name 和 input_schema 才是有效工具定义
            if (tool_type in (None, "custom") or "name" in tool) and "input_schema" in tool:
                func_def = {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                }
                # strict 字段透传（OpenAI 也支持）
                if tool.get("strict") is not None:
                    func_def["strict"] = tool["strict"]
                openai_tools.append({
                    "type": "function",
                    "function": func_def,
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
            elif part.get("type") == "redacted_thinking":
                # 已编辑的思考块，跳过（无法显示内容）
                pass
            elif part.get("type") == "tool_use":
                tool_calls.append({
                    "id": part.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": part.get("name", ""),
                        "arguments": self._serialize_tool_arguments(part.get("input", {})),
                    }
                })
            elif part.get("type") == "document":
                # document 内容块 -> 转为文本标记
                doc_source = part.get("source", {})
                if doc_source.get("type") == "content":
                    doc_content = doc_source.get("content", "")
                    if isinstance(doc_content, str):
                        message_content += doc_content
                else:
                    message_content += "[DOCUMENT]"
            elif part.get("type") == "search_result":
                # search_result -> 转为文本
                search_content = part.get("content", "")
                if isinstance(search_content, str):
                    message_content += search_content
                elif isinstance(search_content, list):
                    for sc in search_content:
                        if isinstance(sc, dict) and sc.get("type") == "text":
                            message_content += sc.get("text", "")
            else:
                # 未知类型，尝试提取文本
                if "text" in part:
                    message_content += part["text"]
                else:
                    logger.debug("Unknown Anthropic response content block type: %s", part.get("type"))

        message = {"role": "assistant", "content": message_content or None}
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        if tool_calls:
            message["tool_calls"] = tool_calls

        result = {
            "id": f"chatcmpl-{data.get('id', '')}",
            "object": "chat.completion",
            "created": data.get("created") or int(time.time()),
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
        stop_seq = data.get("stop_sequence")
        if data.get("stop_reason") == "stop_sequence" and stop_seq:
            result["choices"][0]["x_stop_sequence"] = stop_seq
        return result

    def _map_stop_reason(self, reason: str | None) -> str:
        mapping = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
            "pause_turn": "stop",
            "refusal": "content_filter",
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
            elif delta.get("type") == "signature_delta":
                # Anthropic signature_delta 无 OpenAI Chat 对应字段，显式忽略
                return None
            elif delta.get("type") == "citations_delta":
                # Anthropic citations_delta 无 OpenAI Chat 对应字段，显式忽略
                return None

        elif event_type == "content_block_stop":
            return None

        elif event_type == "message_delta":
            stop_reason = chunk.get("delta", {}).get("stop_reason")
            choice = {"index": 0, "delta": {}, "finish_reason": self._map_stop_reason(stop_reason)}
            stop_seq = chunk.get("delta", {}).get("stop_sequence") or chunk.get("stop_sequence")
            if stop_reason == "stop_sequence" and stop_seq:
                choice["x_stop_sequence"] = stop_seq
            return {
                "id": self._stream_state["msg_id"],
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._stream_state["model"],
                "choices": [choice],
            }

        elif event_type == "message_stop":
            return None

        elif event_type == "ping":
            return None

        return None

    # --- OpenAI Response → Chat Completions ---

    def _drop_unsupported_response_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(data)
        dropped_fields = [field for field in UNSUPPORTED_RESPONSE_REQUEST_FIELDS if field in sanitized]
        for field in dropped_fields:
            sanitized.pop(field, None)
        if dropped_fields:
            logger.warning(
                "[RESPONSES->CHAT] 降级: unsupported request fields dropped: %s",
                ", ".join(sorted(dropped_fields)),
            )
        return sanitized

    def _drop_hosted_response_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chat_tools = []
        dropped_types = []
        for tool in tools:
            tool_type = tool.get("type")
            if tool_type in HOSTED_RESPONSE_TOOL_TYPES:
                dropped_types.append(tool_type)
                continue
            if tool_type != "function":
                raise ValueError(f"Unsupported Responses tool type for Chat Completions upstream: {tool_type}")

            function = {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            }
            if tool.get("strict") is not None:
                function["strict"] = tool["strict"]
            chat_tools.append({
                "type": "function",
                "function": function,
            })

        if dropped_types:
            logger.warning(
                "[RESPONSES->CHAT] 降级: hosted tools dropped for Chat Completions upstream: %s",
                ", ".join(dropped_types),
            )
        return chat_tools

    def _sanitize_response_input_items(self, input_data: Any) -> Any:
        if not isinstance(input_data, list):
            return input_data

        sanitized = []
        dropped_types = []
        for item in input_data:
            if isinstance(item, dict) and item.get("type") in HOSTED_RESPONSE_INPUT_ITEM_TYPES:
                dropped_types.append(item.get("type", ""))
                continue
            sanitized.append(item)

        if dropped_types:
            logger.warning(
                "[RESPONSES->CHAT] 降级: hosted input items dropped for Chat Completions upstream: %s",
                ", ".join(dropped_types),
            )
        return sanitized

    def _response_tools_to_chat(self, tools: list) -> list:
        return self._drop_hosted_response_tools(tools)

    def _response_tool_choice_to_chat(self, tool_choice: Any) -> Any:
        if isinstance(tool_choice, str):
            if tool_choice in {"auto", "none", "required"}:
                return tool_choice
            raise ValueError(f"Unsupported Responses tool_choice for Chat Completions upstream: {tool_choice}")
        if isinstance(tool_choice, dict):
            choice_type = tool_choice.get("type")
            if choice_type == "function":
                name = tool_choice.get("name") or tool_choice.get("function", {}).get("name")
                if not name:
                    raise ValueError("Responses function tool_choice requires a function name")
                return {"type": "function", "function": {"name": name}}
            if choice_type in {"auto", "none", "required"}:
                return choice_type
        raise ValueError(f"Unsupported Responses tool_choice for Chat Completions upstream: {tool_choice}")

    def _response_text_format_to_chat(self, text_config: Any) -> dict[str, Any] | None:
        if not isinstance(text_config, dict):
            return None
        fmt = text_config.get("format")
        if not isinstance(fmt, dict):
            return None

        fmt_type = fmt.get("type")
        if fmt_type in (None, "text"):
            return None
        if fmt_type == "json_object":
            return {"type": "json_object"}
        if fmt_type == "json_schema":
            json_schema = {k: v for k, v in fmt.items() if k != "type"}
            return {"type": "json_schema", "json_schema": json_schema}
        raise ValueError(f"Unsupported Responses text.format type for Chat Completions upstream: {fmt_type}")

    def _response_content_to_chat_content(self, content: Any) -> Any:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return content or ""

        chat_parts = []
        text_parts = []

        def _flush_text_parts():
            if text_parts:
                chat_parts.append({"type": "text", "text": "\n".join(t for t in text_parts if t)})
                text_parts.clear()

        for part in content:
            if not isinstance(part, dict):
                text_parts.append(str(part))
                continue
            part_type = part.get("type")
            if part_type in ("input_text", "output_text", "text"):
                text_parts.append(part.get("text", ""))
            elif part_type == "input_image":
                _flush_text_parts()
                image_url = part.get("image_url") or part.get("url")
                if image_url:
                    image_payload = {"url": image_url}
                    if part.get("detail") is not None:
                        image_payload["detail"] = part["detail"]
                    chat_parts.append({"type": "image_url", "image_url": image_payload})
            elif part_type == "input_file":
                _flush_text_parts()
                file_payload = {}
                for src, dst in (("file_id", "file_id"), ("filename", "filename"), ("file_data", "file_data")):
                    if part.get(src) is not None:
                        file_payload[dst] = part[src]
                if not file_payload and isinstance(part.get("file"), dict):
                    file_payload = dict(part["file"])
                if not file_payload:
                    raise ValueError("Responses input_file content requires file_id, filename, file_data, or file")
                chat_parts.append({"type": "file", "file": file_payload})
            elif part_type == "input_audio":
                _flush_text_parts()
                audio = part.get("input_audio") or {k: v for k, v in part.items() if k in ("data", "format")}
                if not audio:
                    raise ValueError("Responses input_audio content requires input_audio data")
                chat_parts.append({"type": "input_audio", "input_audio": audio})
            elif part_type == "refusal":
                _flush_text_parts()
                chat_parts.append({"type": "refusal", "refusal": part.get("refusal", "")})
            elif "text" in part:
                text_parts.append(part.get("text", ""))
            else:
                raise ValueError(f"Unsupported Responses content block type for Chat Completions upstream: {part_type}")

        if chat_parts:
            _flush_text_parts()
            return chat_parts
        return "\n".join(t for t in text_parts if t)

    def _response_request_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        data = self._drop_unsupported_response_fields(data)

        messages = []
        instructions = data.get("instructions")
        if instructions:
            messages.append({"role": "system", "content": instructions})

        input_data = self._sanitize_response_input_items(data.get("input", []))
        # input 可以是字符串或列表，需要先判断类型
        if isinstance(input_data, str):
            messages.append({"role": "user", "content": input_data})
        else:
            for item in input_data:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    item_type = item.get("type", "")
                    role = item.get("role", "user")
                    if role == "developer":
                        role = "system"
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
                        content = self._response_content_to_chat_content(item.get("content", ""))
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
        if data.get("stop") is not None:
            result["stop"] = data["stop"]
        if data.get("parallel_tool_calls") is not None:
            result["parallel_tool_calls"] = data["parallel_tool_calls"]
        if data.get("reasoning") is not None:
            reasoning = data["reasoning"]
            if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
                result["reasoning_effort"] = reasoning["effort"]
        response_format = self._response_text_format_to_chat(data.get("text"))
        if response_format:
            result["response_format"] = response_format
        user = data.get("safety_identifier") or data.get("user")
        if user:
            result["user"] = user
        had_tools = bool(data.get("tools"))
        compatible_tools_remaining = False
        if data.get("tools"):
            chat_tools = self._response_tools_to_chat(data["tools"])
            if chat_tools:
                result["tools"] = chat_tools
                compatible_tools_remaining = True
            elif data.get("tool_choice") is not None:
                logger.warning(
                    "[RESPONSES->CHAT] 降级: tool_choice dropped because no compatible tools remain"
                )
        if data.get("tool_choice") and (not had_tools or compatible_tools_remaining):
            result["tool_choice"] = self._response_tool_choice_to_chat(data["tool_choice"])
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
