"""
将其他格式转换为 OpenAI Chat Completions 格式

请求体解析走源语法解析模块（ADR-0016 D2 二期）：Anthropic 源见
``converters/parsing_anthropic``，Responses 源见 ``converters/parsing_responses``；
本模块只做渲染（中间条目 → Chat 语法）。finish_reason / tools / tool_choice
映射为"源语法 → 中间条目"（解析模块）与"中间条目 → 目标语法"（本模块渲染函数）
两段。
"""

import json
import time
from typing import Any

from loguru import logger

from converters.base import BaseConverter, thinking_budget_to_effort
from converters.parsing_anthropic import (
    flatten_anthropic_tool_result,
    parse_anthropic_messages,
    parse_anthropic_stop_reason,
    parse_anthropic_system,
    parse_anthropic_tool_choice,
    parse_anthropic_tools,
)
from converters.parsing_responses import parse_responses_finish, parse_responses_input, parse_responses_tool_choice, parse_responses_tools
from converters.stream_events import build_chat_completion_chunk
from converters.stream_usage import _anthropic_usage_raw_merge
from converters.usage import anthropic_to_openai_chat, openai_response_to_chat

HOSTED_RESPONSE_INPUT_ITEM_TYPES = {
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "computer_call",
    "image_generation_call",
}

UNSUPPORTED_RESPONSE_INPUT_ITEM_TYPES = HOSTED_RESPONSE_INPUT_ITEM_TYPES | {"reasoning"}

UNSUPPORTED_RESPONSE_REQUEST_FIELDS = {
    "background",
    "conversation",
    "context_management",
}


def render_finish_chat(finish_reason: str | None) -> str | None:
    """中间 finish 条目 → Chat finish_reason（Chat 词表即中间词表，恒等渲染）。"""
    return finish_reason


def render_tool_choice_chat(tool_choice: str | dict[str, Any] | None) -> str | dict[str, Any] | None:
    """中间 tool_choice 条目 → Chat tool_choice；无法渲染返回 None。"""
    if tool_choice in ("auto", "none", "required"):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
    return None


def render_tools_chat(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """中间工具条目 → Chat tools；无 schema 的工具不收录（Chat 目标要求 parameters）。"""
    tools = []
    for entry in entries:
        if entry["parameters"] is None:
            continue
        func = {
            "name": entry["name"],
            "description": entry["description"],
            "parameters": entry["parameters"] or {},
        }
        if "strict" in entry:
            func["strict"] = entry["strict"]
        tools.append({"type": "function", "function": func})
    return tools


class ToChatCompletionsConverter(BaseConverter):
    """任意格式 → OpenAI Chat Completions"""

    def __init__(self):
        self._stream_state: dict[str, Any] | None = None
        self._stream_include_usage: bool = False

    def set_stream_include_usage(self, flag: bool) -> None:
        """供编排层（proxy.routing）在创建 converter 后透传客户端的 stream_options.include_usage。
        当 flag=True 时，Anthropic→Chat 流式在 message_stop 处、Responses→Chat 流式在
        response.completed 处 emit 末帧 usage chunk。
        """
        self._stream_include_usage = bool(flag)

    def _reset_stream_state(self):
        self._stream_state = {
            "msg_id": "chatcmpl",
            "model": "",
            "tool_call_index": 0,
            "content_block_to_tc_index": {},  # Anthropic content block index → OpenAI tool_call index
            "output_index_to_tc_index": {},  # Response output_index → OpenAI tool_call index
            "item_id_to_tc_index": {},  # Response item_id → OpenAI tool_call index
            "anthropic_usage": {},  # 累积 Anthropic 侧 usage
        }

    @staticmethod
    def _serialize_tool_arguments(value: Any) -> str:
        if value is None:
            value = {}
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    # --- 中间条目 → Chat 消息渲染（Anthropic 源） ---

    @classmethod
    def _tool_call_entry_to_chat(cls, tc: dict[str, Any]) -> dict[str, Any]:
        arguments = tc["arguments"]
        if not isinstance(arguments, str):
            arguments = cls._serialize_tool_arguments(arguments)
        return {
            "id": tc["id"],
            "type": "function",
            "function": {"name": tc["name"], "arguments": arguments},
        }

    @staticmethod
    def _image_kind_to_chat_part(part: dict[str, Any]) -> dict[str, Any]:
        if "url" in part:
            return {"type": "image_url", "image_url": {"url": part["url"]}}
        return {"type": "image_url", "image_url": {"url": f"data:{part.get('media_type', 'image/png')};base64,{part['data']}"}}

    @staticmethod
    def _tool_result_text(tool_result: dict[str, Any]) -> str:
        text = flatten_anthropic_tool_result(tool_result["content"])
        if tool_result["is_error"]:
            text = f"[ERROR] {text}"
        return text

    @classmethod
    def _split_anthropic_parts_for_chat(cls, entry: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], str]:
        """条目 parts → (文本片段, 图片 part, reasoning 文本)；tool_use/tool_result 在条目级。"""
        texts: list[str] = []
        images: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        reasoning_parts: list[str] = []
        for part in entry["parts"]:
            kind = part["kind"]
            if kind == "text":
                texts.append(part["text"])
            elif kind == "image":
                images.append(cls._image_kind_to_chat_part(part))
            elif kind == "thinking":
                reasoning_parts.append(part["text"])
            elif kind == "document":
                if "media_type" in part and "data" in part:
                    files.append(
                        {
                            "type": "file",
                            "file": {"file_data": f"data:{part['media_type']};base64,{part['data']}"},
                        }
                    )
                else:
                    raise ValueError("Anthropic document cannot be represented by Chat file_data")
            elif kind == "search_result":
                search_content = part["content"]
                if isinstance(search_content, str):
                    texts.append(f"[SEARCH_RESULT] {search_content}")
                elif isinstance(search_content, list):
                    for sc in search_content:
                        if isinstance(sc, dict) and sc.get("type") == "text":
                            texts.append(f"[SEARCH_RESULT] {sc.get('text', '')}")
            elif kind == "redacted_thinking":
                # redacted_thinking -> 跳过（已编辑的思考块无法显示内容）
                pass
            elif kind == "unknown":
                block = part["block"]
                if "text" in block:
                    texts.append(block["text"])
                else:
                    logger.debug("Unknown Anthropic content block type: %s", block.get("type"))
        return texts, images, files, "\n".join(reasoning_parts)

    def _anthropic_entry_to_chat_messages(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        role = entry["role"]
        texts, images, files, reasoning = self._split_anthropic_parts_for_chat(entry)
        tool_messages = [
            {"role": "tool", "tool_call_id": tr["tool_use_id"] or "", "content": self._tool_result_text(tr)} for tr in entry["tool_results"]
        ]

        if role == "assistant":
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if images or files:
                assistant_content = []
                if texts:
                    assistant_content.append({"type": "text", "text": "\n".join(texts)})
                assistant_content.extend(images)
                assistant_content.extend(files)
                assistant_msg["content"] = assistant_content
            elif texts:
                assistant_msg["content"] = "\n".join(texts)
            elif reasoning and not entry["tool_calls"]:
                assistant_msg["content"] = ""
            else:
                assistant_msg["content"] = None
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            if entry["tool_calls"]:
                assistant_msg["tool_calls"] = [self._tool_call_entry_to_chat(tc) for tc in entry["tool_calls"]]
            return [assistant_msg]
        if role == "user":
            messages: list[dict[str, Any]] = list(tool_messages)
            user_parts = [{"type": "text", "text": t} for t in texts]
            user_parts.extend(images)
            user_parts.extend(files)
            if user_parts:
                if len(user_parts) == 1 and user_parts[0].get("type") == "text":
                    messages.append({"role": "user", "content": user_parts[0]["text"]})
                else:
                    messages.append({"role": "user", "content": user_parts})
            return messages
        if role == "tool" and entry["tool_results"]:
            return tool_messages
        fallback_content = " ".join(texts) if texts else ""
        return [{"role": role, "content": fallback_content}]

    # --- Anthropic → Chat Completions ---

    def _anthropic_request_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        system_texts = parse_anthropic_system(data.get("system"))
        if system_texts:
            messages.append({"role": "system", "content": "\n".join(system_texts)})
        for entry in parse_anthropic_messages(data):
            messages.extend(self._anthropic_entry_to_chat_messages(entry))

        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
        }
        if result["stream"]:
            result["stream_options"] = {"include_usage": True}
        if data.get("max_tokens") is not None:
            result["max_tokens"] = data["max_tokens"]
        if data.get("temperature") is not None:
            result["temperature"] = data["temperature"]
        if data.get("top_p") is not None:
            result["top_p"] = data["top_p"]
        if data.get("stop_sequences"):
            result["stop"] = data["stop_sequences"]
        if data.get("tools"):
            result["tools"] = render_tools_chat(parse_anthropic_tools(data["tools"]))
        if data.get("tool_choice"):
            rendered = render_tool_choice_chat(parse_anthropic_tool_choice(data["tool_choice"]))
            if rendered is not None:
                result["tool_choice"] = rendered
        thinking = data.get("thinking")
        if thinking and isinstance(thinking, dict):
            if thinking.get("type") == "enabled":
                budget = thinking.get("budget_tokens", 0)
                result["reasoning_effort"] = thinking_budget_to_effort(budget)
                result["enable_thinking"] = True
            elif thinking.get("type") == "adaptive":
                result["reasoning_effort"] = "medium"
                result["enable_thinking"] = True

        # metadata/user_id 处理：Anthropic metadata.user_id -> OpenAI user。
        # 其余 metadata 字段不透传（Claude Code 每请求携带含布尔/嵌套值的 metadata，
        # 官方 OpenAI 仅接受字符串值 ≤512 字符，透传有 400 风险；非官方渠道也基本忽略）
        metadata = data.get("metadata")
        if isinstance(metadata, dict) and metadata.get("user_id"):
            result["user"] = metadata["user_id"]

        # Anthropic 独有参数警告（OpenAI 不支持）
        unsupported_params = []
        if data.get("top_k") is not None and data["top_k"] != 0:
            unsupported_params.append("top_k")

        # 递归检查 cache_control（可能在 system、messages、content blocks、tools 上）
        has_cache_control = data.get("cache_control") is not None
        if not has_cache_control:
            system = data.get("system")
            for part in system if isinstance(system, list) else []:
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
        if not has_cache_control:
            # Anthropic 允许在 tool 定义上挂 cache_control 缓存工具集
            for tool in data.get("tools", []) or []:
                if isinstance(tool, dict) and tool.get("cache_control"):
                    has_cache_control = True
                    break
        if has_cache_control:
            unsupported_params.append("cache_control")

        if unsupported_params:
            logger.debug(
                "Anthropic parameters not supported by OpenAI, will be ignored: %s",
                ", ".join(unsupported_params),
            )

        return result

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
                tool_calls.append(
                    {
                        "id": part.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": part.get("name", ""),
                            "arguments": self._serialize_tool_arguments(part.get("input", {})),
                        },
                    }
                )
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
                    logger.debug(
                        "Unknown Anthropic response content block type: %s",
                        part.get("type"),
                    )

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
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": render_finish_chat(parse_anthropic_stop_reason(data.get("stop_reason"))),
                }
            ],
            "usage": anthropic_to_openai_chat(data.get("usage")),
        }
        stop_seq = data.get("stop_sequence")
        if data.get("stop_reason") == "stop_sequence" and stop_seq:
            result["choices"][0]["x_stop_sequence"] = stop_seq
        return result

    def _anthropic_stream_chunk_to_chat(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        if self._stream_state is None:
            self._reset_stream_state()

        event_type = chunk.get("type") or chunk.get("_event_type", "")

        if event_type == "message_start":
            msg = chunk.get("message", {})
            self._stream_state["msg_id"] = f"chatcmpl-{msg.get('id', '')}"
            self._stream_state["model"] = msg.get("model", "")
            self._stream_state["tool_call_index"] = 0
            # 累积 message_start 中的 usage（原样覆写合并，规则在 stream_usage 模块 ADR-0016 D3）
            anthropic_usage = msg.get("usage")
            if isinstance(anthropic_usage, dict):
                self._stream_state["anthropic_usage"] = _anthropic_usage_raw_merge(self._stream_state["anthropic_usage"], anthropic_usage)
            return [
                build_chat_completion_chunk(
                    self._stream_state["msg_id"],
                    self._stream_state["model"],
                    {"role": "assistant", "content": ""},
                )
            ]

        elif event_type == "content_block_start":
            content_block = chunk.get("content_block", {})
            if content_block.get("type") == "tool_use":
                tc_idx = self._stream_state["tool_call_index"]
                self._stream_state["tool_call_index"] = tc_idx + 1
                self._stream_state["content_block_to_tc_index"][chunk.get("index", 0)] = tc_idx
                return [
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        {
                            "tool_calls": [
                                {
                                    "index": tc_idx,
                                    "id": content_block.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": content_block.get("name", ""),
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                    )
                ]
            elif content_block.get("type") == "thinking":
                return []
            else:
                return [build_chat_completion_chunk(self._stream_state["msg_id"], self._stream_state["model"])]

        elif event_type == "content_block_delta":
            delta = chunk.get("delta") or {}
            if delta.get("type") == "text_delta":
                return [
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        {"content": delta.get("text", "")},
                    )
                ]
            elif delta.get("type") == "thinking_delta":
                return [
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        {"reasoning_content": delta.get("thinking", "")},
                    )
                ]
            elif delta.get("type") == "input_json_delta":
                block_index = chunk.get("index", 0)
                if block_index in self._stream_state["content_block_to_tc_index"]:
                    tc_idx = self._stream_state["content_block_to_tc_index"][block_index]
                else:
                    # fallback：缺少前置 content_block_start 时，退到最近一次分配的 tc_idx；
                    # 直接用 Anthropic 的 block_index 作 OpenAI 的 tool_calls index 会错位
                    # （Anthropic block 编号常 ≥1，OpenAI tool_calls 从 0 起）
                    tc_idx = max(0, self._stream_state["tool_call_index"] - 1)
                    logger.warning(
                        "input_json_delta without matching content_block_start (block_index=%d), fallback tc_idx=%d",
                        block_index,
                        tc_idx,
                    )
                return [
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        {
                            "tool_calls": [
                                {
                                    "index": tc_idx,
                                    "function": {"arguments": delta.get("partial_json", "")},
                                }
                            ]
                        },
                    )
                ]
            elif delta.get("type") == "signature_delta":
                # Anthropic signature_delta 无 OpenAI Chat 对应字段，显式忽略
                return []
            elif delta.get("type") == "citations_delta":
                # Anthropic citations_delta 无 OpenAI Chat 对应字段，显式忽略
                return []

        elif event_type == "content_block_stop":
            return []

        elif event_type == "message_delta":
            delta = chunk.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            delta_usage = chunk.get("usage")
            if isinstance(delta_usage, dict):
                self._stream_state["anthropic_usage"] = _anthropic_usage_raw_merge(self._stream_state["anthropic_usage"], delta_usage)
            if stop_reason is None or self._stream_state.get("finish_sent"):
                return []
            self._stream_state["finish_sent"] = True
            choice = {
                "index": 0,
                "delta": {},
                "finish_reason": render_finish_chat(parse_anthropic_stop_reason(stop_reason)),
            }
            stop_seq = delta.get("stop_sequence") or chunk.get("stop_sequence")
            if stop_reason == "stop_sequence" and stop_seq:
                choice["x_stop_sequence"] = stop_seq
            return [
                build_chat_completion_chunk(
                    self._stream_state["msg_id"],
                    self._stream_state["model"],
                    choices=[choice],
                )
            ]

        elif event_type == "message_stop":
            if self._stream_include_usage:
                usage_payload = anthropic_to_openai_chat(self._stream_state.get("anthropic_usage"))
                return [
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        choices=[],
                        usage=usage_payload,
                    )
                ]
            return []

        elif event_type == "ping":
            return []

        return []

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

    # --- 中间条目 → Chat 消息渲染（Responses 源） ---

    def _response_entry_content_to_chat(self, entry: dict[str, Any]) -> Any:
        """条目 content → Chat content（字符串保持；列表按 kind 渲染并合并文本段）。"""
        if entry["text"] is not None:
            return entry["text"]
        chat_parts: list[dict[str, Any]] = []
        text_parts: list[str] = []

        def _flush_text_parts():
            if text_parts:
                chat_parts.append({"type": "text", "text": "\n".join(t for t in text_parts if t)})
                text_parts.clear()

        for part in entry["parts"]:
            kind = part["kind"]
            if kind == "text":
                text_parts.append(part["text"])
            elif kind == "image":
                if not part["url"]:
                    # 原 _response_content_to_chat_content 对空 URL 图片静默跳过
                    continue
                _flush_text_parts()
                image_payload: dict[str, Any] = {"url": part["url"]}
                if part.get("detail") is not None:
                    image_payload["detail"] = part["detail"]
                chat_parts.append({"type": "image_url", "image_url": image_payload})
            elif kind == "file":
                _flush_text_parts()
                if not part["file"]:
                    raise ValueError("Responses input_file content requires file_id, filename, file_data, or file")
                chat_parts.append({"type": "file", "file": dict(part["file"])})
            elif kind == "audio":
                _flush_text_parts()
                if not part["audio"]:
                    raise ValueError("Responses input_audio content requires input_audio data")
                chat_parts.append({"type": "input_audio", "input_audio": part["audio"]})
            elif kind == "refusal":
                _flush_text_parts()
                chat_parts.append({"type": "refusal", "refusal": part["text"]})
            elif kind == "unknown":
                block = part["block"]
                if "text" in block:
                    text_parts.append(block.get("text", ""))
                else:
                    logger.debug(
                        "Unsupported Responses content block type %r, degrading to text",
                        block.get("type"),
                    )
                    text_parts.append(f"[Unsupported content type: {block.get('type')}]")

        if chat_parts:
            _flush_text_parts()
            return chat_parts
        return "\n".join(t for t in text_parts if t)

    def _response_entry_to_chat_messages(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        if entry["role"] == "tool":
            tool_result = entry["tool_results"][0]
            return [{"role": "tool", "tool_call_id": tool_result["tool_use_id"], "content": tool_result["content"]}]
        return [{"role": entry["role"], "content": self._response_entry_content_to_chat(entry)}]

    def _response_request_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        data = self._drop_unsupported_response_fields(data)

        instructions, entries = parse_responses_input(data)
        # Chat 目标策略：丢弃未支持的 input item（托管调用 + reasoning 历史）
        kept_entries = []
        dropped_types = []
        for entry in entries:
            if entry["item_type"] in UNSUPPORTED_RESPONSE_INPUT_ITEM_TYPES:
                dropped_types.append(entry["item_type"])
                continue
            kept_entries.append(entry)
        if dropped_types:
            logger.warning(
                "[RESPONSES->CHAT] 降级: unsupported input items dropped for Chat Completions upstream: %s",
                ", ".join(dropped_types),
            )

        messages: list[dict[str, Any]] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        pending_tool_calls: list[dict[str, Any]] = []
        for entry in kept_entries:
            if entry["role"] == "assistant" and entry["tool_calls"] and entry["text"] is None and not entry["parts"]:
                # 连续 function_call item 合并为一条 assistant 消息（Chat 格式要求）
                pending_tool_calls.extend(self._tool_call_entry_to_chat(tc) for tc in entry["tool_calls"])
                continue
            if pending_tool_calls:
                messages.append({"role": "assistant", "tool_calls": pending_tool_calls, "content": None})
                pending_tool_calls = []
            messages.extend(self._response_entry_to_chat_messages(entry))
        if pending_tool_calls:
            messages.append({"role": "assistant", "tool_calls": pending_tool_calls, "content": None})

        system_contents = [msg.get("content", "") for msg in messages if msg.get("role") == "system" and msg.get("content")]
        if system_contents:
            messages = [{"role": "system", "content": "\n\n".join(map(str, system_contents))}] + [
                msg for msg in messages if msg.get("role") != "system"
            ]
        result = {
            "model": data.get("model", ""),
            "messages": messages,
            "stream": data.get("stream", False),
        }
        if result["stream"]:
            result["stream_options"] = {"include_usage": True}
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
        if had_tools:
            tool_entries, hosted_types, unsupported_types = parse_responses_tools(data["tools"])
            if hosted_types:
                logger.warning(
                    "[RESPONSES->CHAT] 降级: hosted tools dropped for Chat Completions upstream: %s",
                    ", ".join(hosted_types),
                )
            if unsupported_types:
                raise ValueError(f"Unsupported Responses tool type for Chat Completions upstream: {unsupported_types[0]}")
            chat_tools = render_tools_chat(tool_entries)
            if chat_tools:
                result["tools"] = chat_tools
                compatible_tools_remaining = True
            elif data.get("tool_choice") is not None:
                logger.warning("[RESPONSES->CHAT] 降级: tool_choice dropped because no compatible tools remain")
        if data.get("tool_choice") and (not had_tools or compatible_tools_remaining):
            result["tool_choice"] = render_tool_choice_chat(parse_responses_tool_choice(data["tool_choice"]))
        return result

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

    def _response_response_to_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        output_items = data.get("output", [])
        message_content = ""
        reasoning_content = ""
        tool_calls = []
        for item in output_items:
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        message_content += content.get("text", "")
            elif item.get("type") == "reasoning":
                for content in item.get("content", []):
                    if content.get("type") in ("reasoning_text", "summary_text"):
                        reasoning_content += content.get("text", "")
                for summary in item.get("summary", []):
                    if isinstance(summary, dict):
                        reasoning_content += summary.get("text", "")
            elif item.get("type") == "function_call":
                tool_calls.append(
                    {
                        "id": item.get("call_id", item.get("id", "")),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }
                )

        message = {"role": "assistant", "content": message_content or None}
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        if tool_calls:
            message["tool_calls"] = tool_calls

        finish_reason = parse_responses_finish(data.get("status"), output_items)

        result = {
            "id": data.get("id", ""),
            "object": "chat.completion",
            "created": data.get("created_at", 0),
            "model": data.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": render_finish_chat(finish_reason),
                }
            ],
            "usage": openai_response_to_chat(data.get("usage")),
        }
        return result

    # --- OpenAI Response 流式 → Chat Completions 流式 ---

    def _response_stream_chunk_to_chat(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        if self._stream_state is None:
            self._reset_stream_state()

        event_type = chunk.get("type", "")

        if event_type == "response.created":
            resp = chunk.get("response") or {}
            self._stream_state["msg_id"] = f"chatcmpl-{resp.get('id', '')}"
            self._stream_state["model"] = resp.get("model", "")
            self._stream_state["tool_call_index"] = 0
            return [
                build_chat_completion_chunk(
                    self._stream_state["msg_id"],
                    self._stream_state["model"],
                    {"role": "assistant", "content": ""},
                )
            ]

        elif event_type == "response.output_item.added":
            item = chunk.get("item") or {}
            if item.get("type") == "function_call":
                tc_idx = self._stream_state["tool_call_index"]
                self._stream_state["tool_call_index"] = tc_idx + 1
                output_index = chunk.get("output_index", 0)
                self._stream_state["output_index_to_tc_index"][output_index] = tc_idx
                item_id = chunk.get("item_id", "") or item.get("id", "")
                if item_id:
                    self._stream_state["item_id_to_tc_index"][item_id] = tc_idx
                return [
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        {
                            "tool_calls": [
                                {
                                    "index": tc_idx,
                                    "id": item.get("call_id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name", ""),
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                    )
                ]
            return []

        elif event_type == "response.output_text.delta":
            return [
                build_chat_completion_chunk(
                    self._stream_state["msg_id"],
                    self._stream_state["model"],
                    {"content": chunk.get("delta", "")},
                )
            ]

        elif event_type in (
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        ):
            # M1: 流式 reasoning 必须与非流式一致输出（此前整个静默丢失），
            # 按生态惯例映射为 DeepSeek 风格 reasoning_content 增量
            return [
                build_chat_completion_chunk(
                    self._stream_state["msg_id"],
                    self._stream_state["model"],
                    {"reasoning_content": chunk.get("delta", "")},
                )
            ]

        elif event_type == "response.function_call_arguments.delta":
            output_index = chunk.get("output_index", 0)
            tc_idx = self._stream_state["output_index_to_tc_index"].get(output_index)
            if tc_idx is None:
                tc_idx = self._stream_state["item_id_to_tc_index"].get(chunk.get("item_id", ""), None)
            if tc_idx is None:
                # fallback：缺少前置 response.output_item.added 或 output_index 漂移时，
                # 退到最近一次分配的 tc_idx，避免多个 tool_call 的 arguments
                # 全部串到 index 0
                tc_idx = max(0, self._stream_state["tool_call_index"] - 1)
                logger.warning(
                    "function_call_arguments.delta without matching output_item.added (output_index=%s), fallback tc_idx=%d",
                    output_index,
                    tc_idx,
                )
            return [
                build_chat_completion_chunk(
                    self._stream_state["msg_id"],
                    self._stream_state["model"],
                    {
                        "tool_calls": [
                            {
                                "index": tc_idx,
                                "function": {"arguments": chunk.get("delta", "")},
                            }
                        ]
                    },
                )
            ]

        elif event_type == "response.completed":
            resp = chunk.get("response") or {}
            finish_reason = render_finish_chat(parse_responses_finish(resp.get("status"), resp.get("output", [])))
            if self._stream_include_usage:
                # include_usage 时 usage chunk 与 finish chunk 同拍产出
                # （原 pending stash 排空，与 OpenAI 官方流式结尾一致）
                return [
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        finish_reason=finish_reason,
                    ),
                    build_chat_completion_chunk(
                        self._stream_state["msg_id"],
                        self._stream_state["model"],
                        choices=[],
                        usage=openai_response_to_chat(resp.get("usage")),
                    ),
                ]
            return [
                build_chat_completion_chunk(
                    self._stream_state["msg_id"],
                    self._stream_state["model"],
                    finish_reason=finish_reason,
                )
            ]

        return []

    # --- 公共接口（source_type 分发为类级映射表）---

    _REQUEST_HANDLERS = {
        "anthropic": _anthropic_request_to_chat,
        "openai-response": _response_request_to_chat,
    }
    _RESPONSE_HANDLERS = {
        "anthropic": _anthropic_response_to_chat,
        "openai-response": _response_response_to_chat,
    }
    _STREAM_HANDLERS = {
        "anthropic": _anthropic_stream_chunk_to_chat,
        "openai-response": _response_stream_chunk_to_chat,
    }

    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        return self._dispatch_source(self._REQUEST_HANDLERS, source_type, source_data)

    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        return self._dispatch_source(self._RESPONSE_HANDLERS, source_type, target_response)

    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> list[dict[str, Any]]:
        """流式拍 1：返回该 chunk 产生的全部 Chat Completions 事件（空列表 = 本拍无产出）。"""
        return self._dispatch_source(self._STREAM_HANDLERS, source_type, chunk)
