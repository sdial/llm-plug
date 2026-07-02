import json
import secrets
import time
from typing import Any


def yield_anthropic_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def convert_anthropic_response_to_events(
    converted: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    message_for_start = {
        k: v for k, v in converted.items() if k not in ("stop_reason", "stop_sequence")
    }
    usage = converted.get("usage", {})
    start_usage = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0}
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if key in usage:
            start_usage[key] = usage[key]
    message_for_start["usage"] = start_usage
    events.append(("message_start", {"message": message_for_start}))

    for i, block in enumerate(converted.get("content", [])):
        block_type = block.get("type", "text")
        if block_type == "thinking":
            events.append(
                (
                    "content_block_start",
                    {"index": i, "content_block": {"type": "thinking", "thinking": ""}},
                )
            )
            events.append(
                (
                    "content_block_delta",
                    {
                        "index": i,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": block.get("thinking", ""),
                        },
                    },
                )
            )
        elif block_type == "tool_use":
            events.append(
                (
                    "content_block_start",
                    {
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": {},
                        },
                    },
                )
            )
            events.append(
                (
                    "content_block_delta",
                    {
                        "index": i,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(
                                block.get("input", {}), ensure_ascii=False
                            ),
                        },
                    },
                )
            )
        else:
            events.append(
                (
                    "content_block_start",
                    {"index": i, "content_block": {"type": "text", "text": ""}},
                )
            )
            events.append(
                (
                    "content_block_delta",
                    {
                        "index": i,
                        "delta": {"type": "text_delta", "text": block.get("text", "")},
                    },
                )
            )
        events.append(("content_block_stop", {"index": i}))

    usage = converted.get("usage", {})
    events.append(
        (
            "message_delta",
            {
                "delta": {"stop_reason": converted.get("stop_reason", "end_turn")},
                "usage": {"output_tokens": usage.get("output_tokens", 0)},
            },
        )
    )
    events.append(("message_stop", {}))
    return events


def convert_non_stream_to_stream_events(
    full_response: dict[str, Any],
    response_converter,
    source_type: str,
    output_responses_sse: bool,
) -> list[str]:
    if not output_responses_sse:
        return []
    if response_converter is not None:
        converted = response_converter.convert_response(full_response, source_type)
    else:
        converted = full_response
    return build_responses_stream_events_from_object(converted)


def build_responses_stream_events_from_object(converted: dict[str, Any]) -> list[str]:
    """把一个 Response 形态的完整对象拆成 Responses SSE 事件序列。"""
    events: list[str] = []
    events.append(
        format_sse_for_list({"type": "response.created", "response": converted})
    )
    for idx, item in enumerate(converted.get("output", [])):
        events.append(
            format_sse_for_list(
                {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": item,
                }
            )
        )
        if item.get("type") == "message":
            for part_idx, part in enumerate(item.get("content", [])):
                if part.get("type") == "output_text":
                    events.append(
                        format_sse_for_list(
                            {
                                "type": "response.content_part.added",
                                "output_index": idx,
                                "content_index": part_idx,
                                "part": {"type": "output_text", "text": ""},
                            }
                        )
                    )
                    text = part.get("text", "")
                    if text:
                        events.append(
                            format_sse_for_list(
                                {
                                    "type": "response.output_text.delta",
                                    "output_index": idx,
                                    "content_index": part_idx,
                                    "delta": text,
                                }
                            )
                        )
                    events.append(
                        format_sse_for_list(
                            {
                                "type": "response.content_part.done",
                                "output_index": idx,
                                "content_index": part_idx,
                                "part": part,
                            }
                        )
                    )
        events.append(
            format_sse_for_list(
                {"type": "response.output_item.done", "output_index": idx, "item": item}
            )
        )
    status = converted.get("status", "completed")
    events.append(
        format_sse_for_list(
            {"type": "response.completed", "response": {**converted, "status": status}}
        )
    )
    return events


def build_chat_stream_chunks_from_object(
    full_response: dict[str, Any], model: str
) -> list[dict[str, Any]]:
    """把一个 Chat Completion 完整对象拆成 chat.completion.chunk 列表（不含 [DONE]）。

    用于上游对 stream=true 仍返回整块 JSON 的兜底场景，避免直接吐整块对象破坏流式协议。
    """
    response_id = full_response.get("id") or f"chatcmpl-{secrets.token_hex(12)}"
    created = full_response.get("created") or int(time.time())
    resp_model = full_response.get("model") or model
    chunks: list[dict[str, Any]] = []
    choices = full_response.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return chunks

    for ch_idx, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        message = (
            choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
        )
        role = message.get("role", "assistant")
        content = message.get("content")
        reasoning_content = message.get("reasoning_content")
        tool_calls = message.get("tool_calls")
        finish_reason = choice.get("finish_reason")

        # 首帧：role 头
        chunks.append(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": resp_model,
                "choices": [
                    {"index": ch_idx, "delta": {"role": role}, "finish_reason": None}
                ],
            }
        )

        if isinstance(reasoning_content, str) and reasoning_content:
            chunks.append(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resp_model,
                    "choices": [
                        {
                            "index": ch_idx,
                            "delta": {"reasoning_content": reasoning_content},
                            "finish_reason": None,
                        }
                    ],
                }
            )

        if isinstance(content, str) and content:
            chunks.append(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resp_model,
                    "choices": [
                        {
                            "index": ch_idx,
                            "delta": {"content": content},
                            "finish_reason": None,
                        }
                    ],
                }
            )

        if isinstance(tool_calls, list) and tool_calls:
            tc_delta = []
            for tc_idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                func = (
                    tc.get("function", {})
                    if isinstance(tc.get("function"), dict)
                    else {}
                )
                tc_delta.append(
                    {
                        "index": tc_idx,
                        "id": tc.get("id", ""),
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": func.get("name", ""),
                            "arguments": func.get("arguments", ""),
                        },
                    }
                )
            if tc_delta:
                chunks.append(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": resp_model,
                        "choices": [
                            {
                                "index": ch_idx,
                                "delta": {"tool_calls": tc_delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                )

        # 末帧：finish_reason
        chunks.append(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": resp_model,
                "choices": [
                    {"index": ch_idx, "delta": {}, "finish_reason": finish_reason}
                ],
            }
        )

    usage = full_response.get("usage")
    if isinstance(usage, dict):
        chunks.append(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": resp_model,
                "choices": [],
                "usage": usage,
            }
        )

    return chunks


def format_sse_for_list(data: dict[str, Any]) -> str:
    event_type = data.get("type") if isinstance(data, dict) else None
    if event_type:
        return yield_anthropic_event(event_type, data)
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def iter_sse_blocks(lines, coalesce_data_lines: bool = True):
    event_type = None
    data_lines = []
    passthrough_lines = []

    async for line in lines:
        if not line.strip():
            if event_type or data_lines or passthrough_lines:
                yield event_type, data_lines, passthrough_lines
            event_type = None
            data_lines = []
            passthrough_lines = []
            continue

        if line.startswith("event:") and (event_type or data_lines):
            yield event_type, data_lines, passthrough_lines
            event_type = None
            data_lines = []
            passthrough_lines = []

        if line.startswith(":"):
            passthrough_lines.append(line)
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        else:
            passthrough_lines.append(line)

    if event_type or data_lines or passthrough_lines:
        yield event_type, data_lines, passthrough_lines


def format_passthrough_sse_block(
    event_type: str | None,
    data_lines: list[str],
    passthrough_lines: list[str],
) -> str:
    lines = list(passthrough_lines)
    if event_type:
        lines.append(f"event: {event_type}")
    for data_line in data_lines:
        lines.append(f"data: {data_line}")
    return "\n".join(lines) + "\n\n"


def format_raw_sse(event_type: str | None, data: str) -> str:
    lines = []
    if event_type:
        lines.append(f"event: {event_type}")
    for data_line in data.splitlines() or [""]:
        lines.append(f"data: {data_line}")
    return "\n".join(lines) + "\n\n"


_yield_anthropic_event = yield_anthropic_event
_convert_anthropic_response_to_events = convert_anthropic_response_to_events
_convert_non_stream_to_stream_events = convert_non_stream_to_stream_events
_build_responses_stream_events_from_object = build_responses_stream_events_from_object
_build_chat_stream_chunks_from_object = build_chat_stream_chunks_from_object
_format_sse_for_list = format_sse_for_list
_iter_sse_blocks = iter_sse_blocks
_format_passthrough_sse_block = format_passthrough_sse_block
_format_raw_sse = format_raw_sse
