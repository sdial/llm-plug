"""
将其他格式转换为 OpenAI Response 格式

请求体解析走源语法解析模块（ADR-0016 D2 二期）：Chat 源见
``converters/parsing_chat``，Anthropic 源见 ``converters/parsing_anthropic``；
本模块只做渲染（中间条目 → Responses 语法）与非流式请求/响应转换 + source_type
组装分发。tools / tool_choice / finish_reason 映射为"源语法 → 中间条目"（解析模块）
与"中间条目 → 目标语法"（本模块渲染函数）两段。

流式状态机已按方向拆分（ADR-0016 D2 二期）：chat 源见 ``converters/stream_chat_to_response``，
Anthropic 源见 ``converters/stream_anthropic_to_response``；两机共享的流状态模板 /
聚合截断防护见 ``converters/response_stream_state``，ID 伪造策略见 ``converters/response_ids``。
"""

import json
import time
from typing import Any

from loguru import logger

from converters.base import BaseConverter, thinking_budget_to_effort
from converters.parsing_anthropic import (
    parse_anthropic_messages,
    parse_anthropic_stop_reason,
    parse_anthropic_system,
    parse_anthropic_tool_choice,
    parse_anthropic_tools,
)
from converters.parsing_chat import parse_chat_finish_reason, parse_chat_messages, parse_chat_tool_choice, parse_chat_tools
from converters.response_ids import make_function_call_id, make_message_id, make_response_id
from converters.response_stream_state import ResponseStreamState, new_response_stream_state
from converters.stream_anthropic_to_response import anthropic_stream_chunk_to_response, finalize_anthropic_stream
from converters.stream_chat_to_response import chat_stream_chunk_to_response
from converters.usage import anthropic_to_openai_response


def render_tools_response(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """中间工具条目 → Responses tools（扁平形态；无 schema 时补空对象）。"""
    return [
        {
            "type": "function",
            "name": entry["name"],
            "description": entry["description"],
            "parameters": entry["parameters"] if entry["parameters"] is not None else {},
        }
        for entry in entries
    ]


def render_tool_choice_response(tool_choice: str | dict[str, Any] | None) -> str | dict[str, Any] | None:
    """中间 tool_choice 条目 → Responses tool_choice；无法渲染返回 None。"""
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in ("auto", "none", "required") else None
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {"type": "function", "name": tool_choice.get("name", "")}
    return None


class ToResponseConverter(BaseConverter):
    """任意格式 → OpenAI Response"""

    def __init__(self):
        # 必须在 __init__ 初始化：空流场景（首 chunk 即 [DONE]）下
        # finalize_stream 会先于 _reset_stream_state 被调用。
        # 两拍协议标志已字段化为 ResponseStreamState.need_in_progress（ADR-0022 D0），
        # converter 实例上不再有游离属性。
        self._stream_state: ResponseStreamState | None = None

    def _reset_stream_state(self):
        self._stream_state = new_response_stream_state()

    # --- Chat Completions → Response ---

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

    def _chat_entry_content_to_response(self, entry: dict[str, Any], text_type: str) -> Any:
        """消息中间条目 content → Responses content（原 _chat_content_to_response_content 渲染语义）。

        Responses 要求 input_text / input_image / input_file / input_audio；
        纯文本 part 列表折叠为字符串，保持旧输出形态。
        """
        if entry["text"] is not None:
            return entry["text"]
        parts: list[dict[str, Any]] = []
        for part in entry["parts"]:
            kind = part["kind"]
            if kind == "text":
                parts.append({"type": text_type, "text": part["text"]})
            elif kind == "image":
                image_part: dict[str, Any] = {"type": "input_image", "image_url": part["url"]}
                if part.get("detail"):
                    image_part["detail"] = part["detail"]
                parts.append(image_part)
            elif kind == "audio":
                parts.append({"type": "input_audio", "input_audio": part["audio"]})
            elif kind == "file":
                file_part: dict[str, Any] = {"type": "input_file"}
                file_part.update(part["file"])
                parts.append(file_part)
            elif kind == "refusal":
                parts.append({"type": "refusal", "refusal": part["text"]})
            elif kind == "unknown":
                block = part["block"]
                if "text" in block:
                    parts.append({"type": text_type, "text": block.get("text", "")})
                else:
                    logger.warning("Unsupported chat content type %r, converting to text", block.get("type"))
                    parts.append({"type": text_type, "text": f"[Unsupported content type: {block.get('type')}]"})
        if parts and all(p.get("type") in ("input_text", "output_text") for p in parts):
            return "\n".join(p.get("text", "") for p in parts)
        return parts

    def _chat_request_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        system_contents, entries = parse_chat_messages(data)
        # Responses 只有单个 instructions 字段；按原顺序拼接，避免静默丢弃较早的 system 消息。
        instructions = "\n\n".join(system_contents) if system_contents else None
        input_items = []
        for entry in entries:
            role = entry["role"]
            if role == "tool":
                for tool_result in entry["tool_results"]:
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": tool_result["tool_use_id"],
                            "output": tool_result["content"],
                        }
                    )
            elif role == "assistant":
                content = self._chat_entry_content_to_response(entry, "output_text")
                if entry["tool_calls"]:
                    if content:
                        input_items.append({"role": "assistant", "content": content})
                    for tc in entry["tool_calls"]:
                        input_items.append(
                            {
                                "type": "function_call",
                                "call_id": tc["id"],
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            }
                        )
                else:
                    input_items.append({"role": "assistant", "content": content or ""})
            else:
                input_items.append(
                    {
                        "role": role,
                        "content": self._chat_entry_content_to_response(entry, "input_text"),
                    }
                )

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
            result["tools"] = render_tools_response(parse_chat_tools(data["tools"]))
        if data.get("tool_choice"):
            rendered = render_tool_choice_response(parse_chat_tool_choice(data["tool_choice"]))
            if rendered is not None:
                result["tool_choice"] = rendered
        if data.get("reasoning_effort") is not None:
            result["reasoning"] = {"effort": data["reasoning_effort"]}
        return result

    def _chat_response_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices", [])
        upstream_id = data.get("id", "")
        response_id = make_response_id(upstream_id)
        output = []
        output_text_parts = []
        finish_reasons = []

        for choice_pos, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message", {})
            if not isinstance(msg, dict):
                msg = {}
            text = msg.get("content", "") or ""
            refusal = msg.get("refusal")
            tool_calls = msg.get("tool_calls")
            reasoning_content = msg.get("reasoning_content")
            finish_reasons.append(parse_chat_finish_reason(choice.get("finish_reason", "stop")))
            choice_index = choice.get("index", choice_pos)
            id_suffix = "" if len(choices) <= 1 else f"_{choice_index}"

            if reasoning_content:
                output.append(
                    {
                        "type": "reasoning",
                        "id": f"rs_{response_id.removeprefix('resp_')}{id_suffix}",
                        "summary": [],
                        "content": [{"type": "reasoning_text", "text": reasoning_content}],
                    }
                )
            if text or refusal:
                content = []
                if text:
                    content.append({"type": "output_text", "text": text})
                    output_text_parts.append(text)
                if refusal:
                    content.append({"type": "refusal", "refusal": refusal})
                message_id = make_message_id(response_id, upstream_id)
                if id_suffix:
                    message_id = f"{message_id}{id_suffix}"
                output.append(
                    {
                        "type": "message",
                        "id": message_id,
                        "status": "completed",
                        "role": "assistant",
                        "content": content,
                    }
                )
            if tool_calls:
                for tc in tool_calls:
                    call_id = tc.get("id", "")
                    output.append(
                        {
                            "type": "function_call",
                            "id": make_function_call_id(call_id),
                            "call_id": call_id,
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", "{}"),
                            "status": "completed",
                        }
                    )

        status = "completed"
        incomplete_details = None
        if "length" in finish_reasons:
            status = "incomplete"
            incomplete_details = {"reason": "max_output_tokens"}
        elif "content_filter" in finish_reasons:
            status = "incomplete"
            incomplete_details = {"reason": "content_filter"}

        if not output:
            output.append(
                {
                    "type": "message",
                    "id": make_message_id(response_id, upstream_id),
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                }
            )

        result = {
            "id": response_id,
            "object": "response",
            "created_at": data.get("created", 0),
            "model": data.get("model", ""),
            "status": status,
            "output": output,
            "output_text": "\n".join(output_text_parts),
            "usage": self._chat_usage_to_response_usage(data.get("usage", {})),
        }
        if upstream_id and upstream_id != response_id:
            result["_upstream_id"] = upstream_id
        if incomplete_details:
            result["incomplete_details"] = incomplete_details
        return result

    # --- Anthropic → Response ---

    def _anthropic_request_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        instructions_list = parse_anthropic_system(data.get("system"))
        instructions = "\n".join(instructions_list) if instructions_list else None
        entries = parse_anthropic_messages(data)
        input_items = []
        for entry in entries:
            role = entry["role"]
            if entry["text"] is not None:
                input_items.append({"role": role, "content": entry["text"]})
                continue
            # tool_use / tool_result 按原始块顺序就地产出 function_call /
            # function_call_output item；文本段收集后合并（原解析语义）
            text_parts: list[str] = []
            content_parts: list[dict[str, Any]] = []
            for part in entry["parts"]:
                kind = part["kind"]
                if kind == "text":
                    text_parts.append(part["text"])
                elif kind == "image":
                    if part.get("media_type") is not None:
                        content_parts.append({"type": "input_image", "image_url": f"data:{part['media_type']};base64,{part['data']}"})
                    else:
                        content_parts.append({"type": "input_image", "image_url": part.get("url", "")})
                elif kind == "document":
                    if "media_type" in part and "data" in part:
                        content_parts.append({"type": "input_file", "file_data": f"data:{part['media_type']};base64,{part['data']}"})
                    elif "url" in part:
                        content_parts.append({"type": "input_file", "file_url": part["url"]})
                    else:
                        raise ValueError("Anthropic document requires portable base64 data or URL")
                elif kind == "tool_use":
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": part["id"],
                            "name": part["name"],
                            "arguments": json.dumps(part["input"]),
                        }
                    )
                elif kind == "tool_result":
                    tr_content = part["content"]
                    if isinstance(tr_content, list):
                        result_text = "\n".join(c.get("text", "") for c in tr_content if isinstance(c, dict) and c.get("type") == "text")
                    else:
                        result_text = str(tr_content) if tr_content else ""
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": part["tool_use_id"],
                            "output": result_text,
                        }
                    )
                else:
                    # image/document 之外的多模态/未识别块（thinking /
                    # search_result / unknown）：映射不了，降级为类型标记文本
                    block_type = part["block"].get("type") if kind == "unknown" else kind
                    text_parts.append(f"[Unsupported content type: {block_type}]")
            if text_parts or content_parts:
                parts: list[dict[str, Any]] = [{"type": "input_text", "text": "\n".join(text_parts)}] if text_parts else []
                parts.extend(content_parts)
                if content_parts:
                    input_items.append({"role": role, "content": parts})
                else:
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
            result["tools"] = render_tools_response(parse_anthropic_tools(data["tools"]))
        if data.get("tool_choice"):
            rendered = render_tool_choice_response(parse_anthropic_tool_choice(data["tool_choice"]))
            if rendered is not None:
                result["tool_choice"] = rendered
        thinking = data.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") in {"enabled", "adaptive"}:
            budget = thinking.get("budget_tokens", 0)
            result["reasoning"] = {"effort": thinking_budget_to_effort(budget)}
        return result

    def _anthropic_response_to_response(self, data: dict[str, Any]) -> dict[str, Any]:
        output: list[dict[str, Any]] = []
        stop_reason = parse_anthropic_stop_reason(data.get("stop_reason", "end_turn"))
        msg_id = data.get("id", "")

        text_buffer = ""
        reasoning_text = ""

        def flush_reasoning() -> None:
            nonlocal reasoning_text
            if reasoning_text:
                output.append(
                    {
                        "type": "reasoning",
                        "id": f"rs_{msg_id}",
                        "summary": [],
                        "content": [{"type": "reasoning_text", "text": reasoning_text}],
                    }
                )
                reasoning_text = ""

        def flush_text() -> None:
            nonlocal text_buffer
            if text_buffer:
                output.append(
                    {
                        "type": "message",
                        "id": f"msg_{msg_id}",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text_buffer}],
                    }
                )
                text_buffer = ""

        for part in data.get("content", []):
            ptype = part.get("type")
            if ptype == "thinking":
                reasoning_text += part.get("thinking", "")
            elif ptype == "text":
                flush_reasoning()
                text_buffer += part.get("text", "")
            elif ptype == "tool_use":
                flush_reasoning()
                flush_text()
                call_id = part.get("id", "")
                output.append(
                    {
                        "type": "function_call",
                        "id": make_function_call_id(call_id),
                        "call_id": call_id,
                        "name": part.get("name", ""),
                        "arguments": json.dumps(part.get("input", {})),
                        "status": "completed",
                    }
                )

        flush_reasoning()
        flush_text()

        if not output:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{msg_id}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                }
            )

        status = "completed"
        if stop_reason == "length":
            status = "incomplete"

        return {
            "id": f"resp_{msg_id}",
            "object": "response",
            "created_at": int(time.time()),
            "model": data.get("model", ""),
            "status": status,
            "output": output,
            "usage": anthropic_to_openai_response(data.get("usage")),
        }

    # --- 公共接口（source_type 分发为类级映射表）---
    # 两套流式状态机分别独立成件（ADR-0016 D2 二期拆分）：
    # chat → converters/stream_chat_to_response，anthropic → converters/stream_anthropic_to_response
    # 流式 handler 首参为 ResponseStreamState（ADR-0022 D2，经 convert_stream_chunk 传入）

    _REQUEST_HANDLERS = {
        "openai-chat-completions": _chat_request_to_response,
        "anthropic": _anthropic_request_to_response,
    }
    _RESPONSE_HANDLERS = {
        "openai-chat-completions": _chat_response_to_response,
        "anthropic": _anthropic_response_to_response,
    }
    _STREAM_HANDLERS = {
        "openai-chat-completions": chat_stream_chunk_to_response,
        "anthropic": anthropic_stream_chunk_to_response,
    }

    def convert_request(self, source_data: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        return self._dispatch_source(self._REQUEST_HANDLERS, source_type, source_data)

    def convert_response(self, target_response: dict[str, Any], source_type: str = "") -> dict[str, Any]:
        return self._dispatch_source(self._RESPONSE_HANDLERS, source_type, target_response)

    def convert_stream_chunk(self, chunk: dict[str, Any], source_type: str = "") -> list[dict[str, Any]]:
        """流式拍 1：返回该 chunk 产生的全部 Responses 事件（空列表 = 本拍无产出）。

        分发表传 ``self._stream_state``（ADR-0022 D2）：方向函数只依赖流状态，
        不再收 converter 实例；首 chunk 前状态为 None，懒初始化上移到此处
        （原方向文件内的 reset 段）。
        """
        if self._stream_state is None:
            self._reset_stream_state()
            logger.debug("[CHUNK] reset stream_state for first chunk")
        handler = self._STREAM_HANDLERS.get(source_type)
        if handler is None:
            raise ValueError(f"{type(self).__name__} 不支持 source_type={source_type!r}")
        return self._assign_sequence_numbers(handler(self._stream_state, chunk))

    def finalize_stream(self, source_type: str = "") -> list[dict[str, Any]]:
        logger.debug(f"[FINALIZE] source_type={source_type} stream_state={self._stream_state is not None}")
        if source_type == "anthropic":
            if self._stream_state is None:
                return []
            return self._assign_sequence_numbers(finalize_anthropic_stream(self._stream_state))
        if source_type != "openai-chat-completions" or self._stream_state is None:
            return []
        # chat 方向收尾补偿（pending 排空 / 已完成跳过 / 空流伪造 ID）已收编为
        # state 方法（ADR-0022 D1）；对外接缝签名不变
        return self._assign_sequence_numbers(self._stream_state.finalize())

    def _assign_sequence_numbers(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Responses 流的每个事件按实际发出顺序拥有单调 sequence_number。"""
        if self._stream_state is None:
            return events
        for event in events:
            event["sequence_number"] = self._stream_state.next_seq()
        return events
