import json
import secrets
import time
from typing import Any

from loguru import logger


def yield_anthropic_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def convert_anthropic_response_to_events(
    converted: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    message_for_start = {k: v for k, v in converted.items() if k not in ("stop_reason", "stop_sequence")}
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
                            "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False),
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
    converted = response_converter.convert_response(full_response, source_type) if response_converter is not None else full_response
    return build_responses_stream_events_from_object(converted)


def build_responses_stream_events_from_object(converted: dict[str, Any]) -> list[str]:
    """把一个 Response 形态的完整对象拆成 Responses SSE 事件序列。"""
    events: list[str] = []
    events.append(format_sse_for_list({"type": "response.created", "response": converted}))
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
        events.append(format_sse_for_list({"type": "response.output_item.done", "output_index": idx, "item": item}))
    status = converted.get("status", "completed")
    events.append(format_sse_for_list({"type": "response.completed", "response": {**converted, "status": status}}))
    return events


def build_chat_stream_chunks_from_object(full_response: dict[str, Any], model: str) -> list[dict[str, Any]]:
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
        message = choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
        role = message.get("role", "assistant")
        content = message.get("content")
        reasoning_content = message.get("reasoning_content")
        tool_calls = message.get("tool_calls")
        finish_reason = choice.get("finish_reason")

        # 首帧：role 头
        chunks.append(_build_chat_completion_chunk(response_id, resp_model, {"role": role}, index=ch_idx, created=created))

        if isinstance(reasoning_content, str) and reasoning_content:
            chunks.append(
                _build_chat_completion_chunk(response_id, resp_model, {"reasoning_content": reasoning_content}, index=ch_idx, created=created)
            )

        if isinstance(content, str) and content:
            chunks.append(_build_chat_completion_chunk(response_id, resp_model, {"content": content}, index=ch_idx, created=created))

        if isinstance(tool_calls, list) and tool_calls:
            tc_delta = []
            for tc_idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
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
                chunks.append(_build_chat_completion_chunk(response_id, resp_model, {"tool_calls": tc_delta}, index=ch_idx, created=created))

        # 末帧：finish_reason
        chunks.append(_build_chat_completion_chunk(response_id, resp_model, finish_reason=finish_reason, index=ch_idx, created=created))

    usage = full_response.get("usage")
    if isinstance(usage, dict):
        chunks.append(_build_chat_completion_chunk(response_id, resp_model, choices=[], usage=usage, created=created))

    return chunks


def build_chat_completion_chunk(
    chunk_id: str,
    model: str,
    delta: dict[str, Any] | None = None,
    *,
    finish_reason: str | None = None,
    index: int = 0,
    choices: list[dict[str, Any]] | None = None,
    created: int | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """chat.completion.chunk 信封唯一合成住所（ADR-0016 D1 工厂 1）。

    五字段信封（id / object / created / model / choices）只在此手写一次，
    新增信封字段只改这里。默认按单 choice 合成（index + delta + finish_reason）；
    多 choice / 带 x_stop_sequence 等扩展形态经 ``choices`` 显式传入；
    usage-only 末帧传 ``choices=[]`` + ``usage``（usage 恒在 choices 之后，wire 顺序不变）。
    """
    if choices is None:
        choices = [{"index": index, "delta": {} if delta is None else delta, "finish_reason": finish_reason}]
    if created is None:
        created = int(time.time())
    chunk: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": choices,
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def build_response_item_done_events(
    item_type: str,
    *,
    item_id: str,
    output_index: int,
    content_index: int = 0,
    text: str = "",
    call_id: str = "",
    name: str = "",
    arguments: str = "",
    item: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Responses ``*.done`` 收尾序列唯一合成住所（ADR-0016 D1 工厂 3）。

    按 item 类型合成：
    - ``message``：output_text.done + content_part.done（text 非空时）+ output_item.done；
    - ``function_call``：function_call_arguments.done + output_item.done（无 content_index）；
    - ``reasoning``：reasoning_text.done + content_part.done（text 非空时）+ output_item.done。

    ``item`` 为调用方已构建的 completed output item（如带归一化 id 的形态），
    传入则原样作为 output_item.done 的 item；缺省由工厂按类型合成标准 completed 形态。
    纯事件合成：累计文本读取、状态复位等副作用一律留在转换器侧。
    """
    events: list[dict[str, Any]] = []
    if item_type == "message":
        if text:
            events.append(
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": content_index,
                    "text": text,
                }
            )
            events.append(
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": content_index,
                    "part": {"type": "output_text", "text": text},
                }
            )
        done_item = {
            "type": "message",
            "id": item_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    elif item_type == "function_call":
        events.append(
            {
                "type": "response.function_call_arguments.done",
                "item_id": item_id,
                "output_index": output_index,
                "name": name,
                "arguments": arguments,
            }
        )
        done_item = {
            "type": "function_call",
            "id": item_id,
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        }
    elif item_type == "reasoning":
        if text:
            events.append(
                {
                    "type": "response.reasoning_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": content_index,
                    "text": text,
                }
            )
            events.append(
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": content_index,
                    "part": {"type": "reasoning_text", "text": text},
                }
            )
        done_item = {
            "type": "reasoning",
            "id": item_id,
            "summary": [],
            "content": [{"type": "reasoning_text", "text": text}],
        }
    else:
        raise ValueError(f"unsupported item_type for done sequence: {item_type!r}")
    events.append({"type": "response.output_item.done", "output_index": output_index, "item": done_item if item is None else item})
    return events


def format_sse_for_list(
    data: dict[str, Any],
    event_type: str | None = None,
    infer_event_type: bool = True,
) -> str:
    # Ticket 06：此处的 [FORMAT SSE ERROR] 日志 + re-raise 从流式生成器的内联
    # _format_sse 闭包迁来，使全项目唯一 SSE 格式化入口统一兜底（wire 不变，仅日志面）。
    try:
        inferred_type = data.get("type") if infer_event_type and isinstance(data, dict) else None
        resolved_type = event_type or inferred_type
        if resolved_type:
            return yield_anthropic_event(resolved_type, data)
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    except Exception as sse_err:
        logger.error(f"[FORMAT SSE ERROR] {type(sse_err).__name__}: {sse_err} data={data}")
        logger.exception("[FORMAT SSE ERROR TRACEBACK]")
        raise


def build_anthropic_message_stop_event() -> list[str]:
    return [_yield_anthropic_event("message_stop", {"type": "message_stop"})]


def build_responses_failed_event(model: str) -> list[str]:
    failed_data = {
        "type": "response.failed",
        "response": {
            "id": "",
            "object": "response",
            "status": "failed",
            "model": model,
            "output": [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        },
    }
    return [_yield_anthropic_event("response.failed", failed_data)]


def build_responses_completed_event(model: str, input_tokens: int = 0, output_tokens: int = 0) -> list[str]:
    """response.completed 终端事件工厂（ADR-0015 D0 接缝 2）。

    与 failed 工厂同构；usage 取流内累计值（EOF 兜底补发时避免客户端挂起），
    缺省 0 与 failed 形态一致。协议终止事件自此只有 stream_sse 一个合成住所。
    """
    completed_data = {
        "type": "response.completed",
        "response": {
            "id": "",
            "object": "response",
            "status": "completed",
            "model": model,
            "output": [],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        },
    }
    return [_yield_anthropic_event("response.completed", completed_data)]


def build_chat_done_event() -> list[str]:
    return ["data: [DONE]\n\n"]


def build_anthropic_error_events(message: str) -> list[str]:
    error_data = {"type": "error", "error": {"type": "api_error", "message": message}}
    return [
        _yield_anthropic_event("error", error_data),
        *_build_anthropic_message_stop_event(),
    ]


def build_responses_error_events(message: str, model: str) -> list[str]:
    error_data = {"type": "error", "error": {"message": message, "type": "api_error"}}
    return [
        _yield_anthropic_event("error", error_data),
        *_build_responses_failed_event(model),
    ]


def build_chat_error_chunk(message: str) -> list[str]:
    error_payload = json.dumps({"error": {"message": message, "type": "api_error"}}, ensure_ascii=False)
    return [f"data: {error_payload}\n\n"]


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
_build_anthropic_message_stop_event = build_anthropic_message_stop_event
_build_responses_failed_event = build_responses_failed_event
_build_responses_completed_event = build_responses_completed_event
_build_chat_done_event = build_chat_done_event
_build_anthropic_error_events = build_anthropic_error_events
_build_responses_error_events = build_responses_error_events
_build_chat_error_chunk = build_chat_error_chunk
_build_chat_completion_chunk = build_chat_completion_chunk
_build_response_item_done_events = build_response_item_done_events
