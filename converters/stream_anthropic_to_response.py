"""Anthropic → OpenAI Response 流式状态机（ADR-0016 D2 二期：to_response 按方向拆分）。

从 ``converters/to_response`` 纯搬移：拍 1 处理 Anthropic SSE 事件
（``anthropic_stream_chunk_to_response``：message_start / content_block_* / message_delta），
拍 2 收尾（``finalize_anthropic_stream``：message_delta 缺失时的 done + completed 安全网）。
块收尾 done 序列经 ``build_block_done_events`` 单份合成。
函数签名收 ``state: ResponseStreamState``（ADR-0022 D2：conv 参数消亡，chunk 解析
直接读写类型化字段）；两拍协议段已收编为 ``ResponseStreamState.emit_created``
（ADR-0022 D1）。
"""

import secrets
import time
from typing import Any

from converters.parsing_anthropic import parse_anthropic_stop_reason
from converters.response_ids import make_function_call_id, make_message_id
from converters.response_stream_state import (
    ResponseStreamState,
    append_aggregate_text,
    append_item_text,
    append_tool_arguments,
    build_output_items,
    item_text,
    make_response_event,
)
from converters.stream_events import build_response_item_done_events
from converters.stream_usage import _anthropic_usage_delta_overwrite, _anthropic_usage_start_accumulate
from converters.usage import anthropic_to_openai_response


def build_block_done_events(state: ResponseStreamState, block: dict[str, Any]) -> list[dict[str, Any]]:
    """为 Anthropic content block 生成 Responses 的 *.done 收尾事件并标记已关闭。

    严格协议客户端（Codex CLI / Agents SDK）依赖 output_item.done /
    output_text.done / function_call_arguments.done 判断流完成；
    `_save_response_state` 也从随后的 response.completed.output 提取历史。
    """
    block_type = block.get("type")
    item_id = block.get("item_id", "")
    output_index = block.get("output_index", 0)
    content_index = block.get("content_index", 0)
    if block_type == "text":
        events = build_response_item_done_events(
            "message", item_id=item_id, output_index=output_index, content_index=content_index, text=item_text(state, output_index)
        )
        if state.active_text_item_id == item_id:
            state.active_text_item_id = None
            state.active_text_output_index = None
            state.content_part_added_sent = False
    elif block_type == "tool_use":
        call_id = block.get("call_id", "")
        tc_data = state.tool_calls.get(call_id, {})
        fc_item_id = item_id or make_function_call_id(call_id)
        events = build_response_item_done_events(
            "function_call",
            item_id=fc_item_id,
            output_index=output_index,
            call_id=call_id,
            name=tc_data.get("name", ""),
            arguments=tc_data.get("arguments", ""),
        )
    elif block_type == "thinking":
        events = build_response_item_done_events(
            "reasoning",
            item_id=item_id,
            output_index=output_index,
            content_index=content_index,
            text=state.reasoning_content,
        )
    else:
        events = []
    block["closed"] = True
    return events


def anthropic_stream_chunk_to_response(state: ResponseStreamState, chunk: dict[str, Any]) -> dict[str, Any] | None:
    event_type = chunk.get("type") or chunk.get("_event_type", "")

    if event_type == "message_start":
        msg = chunk.get("message", {})
        state.message_id = msg.get("id", "")
        state.response_id = f"resp_{msg.get('id', '')}"
        state.model = msg.get("model", "")
        state.created_at = int(time.time())
        msg_usage = msg.get("usage")
        if isinstance(msg_usage, dict):
            # message_start 加法播种规则在 stream_usage 模块（ADR-0016 D3）
            state.anthropic_usage = _anthropic_usage_start_accumulate(state.anthropic_usage, msg_usage)
        # 两拍协议：created + in_progress 同拍（不变量住所
        # ResponseStreamState.emit_created，ADR-0022 D1）
        return state.emit_created()

    elif event_type == "content_block_start":
        content_block = chunk.get("content_block", {})
        block_index = chunk.get("index", 0)
        if content_block.get("type") == "text":
            item_id = state.message_id
            if not item_id.startswith("msg_"):
                item_id = make_message_id(state.response_id or "resp_stream", item_id)
            idx = state.output_index
            state.output_index = idx + 1
            state.anthropic_content_blocks[block_index] = {
                "type": "text",
                "item_id": item_id,
                "output_index": idx,
                "content_index": 0,
            }
            state.active_text_item_id = item_id
            state.active_text_output_index = idx
            state.item_texts[idx] = ""
            state.output_items.append(
                {
                    "type": "message",
                    "output_index": idx,
                    "item_id": item_id,
                }
            )
            # 严格协议：text delta 之前必须先发 output_item.added + content_part.added
            added_event = make_response_event(
                "response.output_item.added",
                output_index=idx,
                item={
                    "type": "message",
                    "id": item_id,
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            )
            content_added_event = make_response_event(
                "response.content_part.added",
                item_id=item_id,
                output_index=idx,
                content_index=0,
                part={"type": "output_text", "text": ""},
            )
            return [added_event, content_added_event]
        if content_block.get("type") == "tool_use":
            idx = state.output_index
            state.output_index = idx + 1
            call_id = content_block.get("id", "")
            item_id = make_function_call_id(call_id)
            state.anthropic_content_blocks[block_index] = {
                "type": "tool_use",
                "item_id": item_id,
                "call_id": call_id,
                "output_index": idx,
            }
            state.tool_calls[call_id] = {
                "name": content_block.get("name", ""),
                "arguments": "",
                "output_index": idx,
            }
            state.output_items.append(
                {
                    "type": "function_call",
                    "output_index": idx,
                    "call_id": call_id,
                }
            )
            return [
                {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": content_block.get("id", ""),
                        "name": content_block.get("name", ""),
                        "arguments": "",
                        "status": "in_progress",
                    },
                }
            ]
        if content_block.get("type") == "thinking":
            idx = state.output_index
            state.output_index = idx + 1
            reasoning_id = f"rs_{state.message_id}"
            state.reasoning_started = True
            state.reasoning_id = reasoning_id
            state.anthropic_content_blocks[block_index] = {
                "type": "thinking",
                "item_id": reasoning_id,
                "output_index": idx,
                "content_index": 0,
            }
            state.output_items.append(
                {
                    "type": "reasoning",
                    "output_index": idx,
                    "id": reasoning_id,
                }
            )
            added_event = make_response_event(
                "response.output_item.added",
                output_index=idx,
                item={
                    "type": "reasoning",
                    "id": reasoning_id,
                    "summary": [],
                    "content": [],
                },
            )
            # 严格协议：reasoning_text.delta 之前必须先发 content_part.added
            content_added_event = make_response_event(
                "response.content_part.added",
                item_id=reasoning_id,
                output_index=idx,
                content_index=0,
                part={"type": "reasoning_text", "text": ""},
            )
            return [added_event, content_added_event]
        return []

    elif event_type == "content_block_delta":
        delta = chunk.get("delta") or {}
        block = state.anthropic_content_blocks.get(chunk.get("index", 0), {})
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            append_aggregate_text(state, "accumulated_text", text)
            append_item_text(state, block.get("output_index"), text)
            return [
                {
                    "type": "response.output_text.delta",
                    "item_id": block.get("item_id", state.message_id),
                    "output_index": block.get("output_index", chunk.get("index", 0)),
                    "content_index": block.get("content_index", 0),
                    "delta": text,
                }
            ]
        elif delta.get("type") == "input_json_delta":
            partial_json = delta.get("partial_json", "")
            call_id = block.get("call_id", "")
            if call_id in state.tool_calls:
                append_tool_arguments(state, call_id, partial_json)
            return [
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": block.get("item_id", make_function_call_id(call_id)),
                    "output_index": block.get("output_index", chunk.get("index", 0)),
                    "delta": partial_json,
                }
            ]
        elif delta.get("type") == "thinking_delta":
            thinking = delta.get("thinking", "")
            append_aggregate_text(state, "reasoning_content", thinking)
            if not state.reasoning_started:
                state.reasoning_started = True
                state.reasoning_id = f"rs_{state.message_id}"
                idx = state.output_index
                state.output_index = idx + 1
                block = {
                    "type": "thinking",
                    "item_id": state.reasoning_id,
                    "output_index": idx,
                    "content_index": 0,
                }
                state.anthropic_content_blocks[chunk.get("index", 0)] = block
                result = {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {
                        "type": "reasoning",
                        "id": state.reasoning_id,
                        "summary": [],
                        "content": [],
                    },
                }
                # 严格协议：reasoning_text.delta 之前必须先发 content_part.added
                content_added_event = make_response_event(
                    "response.content_part.added",
                    item_id=block["item_id"],
                    output_index=block["output_index"],
                    content_index=block["content_index"],
                    part={"type": "reasoning_text", "text": ""},
                )
                delta_event = {
                    "type": "response.reasoning_text.delta",
                    "item_id": block["item_id"],
                    "output_index": block["output_index"],
                    "content_index": block["content_index"],
                    "delta": thinking,
                }
                # 严格协议：output_item.added → content_part.added → delta 同拍产出
                return [result, content_added_event, delta_event]
            return [
                {
                    "type": "response.reasoning_text.delta",
                    "item_id": block.get("item_id", state.reasoning_id),
                    "output_index": block.get("output_index", 0),
                    "content_index": block.get("content_index", 0),
                    "delta": thinking,
                }
            ]
        return []

    elif event_type == "content_block_stop":
        # Anthropic 块结束对应 Responses 的 *.done 事件序列；严格协议客户端
        # 依赖这些事件判断输出项完成，缺失会判流未完成
        block = state.anthropic_content_blocks.get(chunk.get("index", 0))
        if not block or block.get("closed"):
            return []
        done_events = build_block_done_events(state, block)
        if not done_events:
            return []
        return done_events

    elif event_type == "message_delta":
        # Anthropic message_delta 的 usage 是累计终值而非增量（output_tokens
        # 尤其如此）；覆写规则在 stream_usage 模块（ADR-0016 D3），否则会把
        # message_start 的初始值重复累加
        delta_usage = chunk.get("usage")
        if isinstance(delta_usage, dict):
            state.anthropic_usage = _anthropic_usage_delta_overwrite(state.anthropic_usage, delta_usage)
        if state.completed_sent:
            return []
        stop_reason = parse_anthropic_stop_reason((chunk.get("delta") or {}).get("stop_reason"))
        status = "completed"
        if stop_reason == "length":
            status = "incomplete"
        # completed 必须携带完整 output：Codex CLI / Agents SDK 从
        # completed.response.output 取内容，_save_response_state 也从
        # 这里提取 previous_response_id 会话历史
        final_usage = anthropic_to_openai_response(state.anthropic_usage)
        state.completed_sent = True
        completed_response = {
            "id": state.response_id,
            "object": "response",
            "created_at": state.created_at,
            "model": state.model,
            "status": status,
            "output": build_output_items(state),
            "output_text": state.accumulated_text,
            "usage": final_usage,
        }
        if status == "incomplete":
            completed_response["incomplete_details"] = {"reason": "max_output_tokens"}
        return [
            {
                "type": "response.completed",
                "response": completed_response,
            }
        ]

    elif event_type == "message_stop" or event_type == "ping":
        return []

    return []


def finalize_anthropic_stream(state: ResponseStreamState) -> list[dict[str, Any]]:
    """Anthropic 上游流结束的安全网：message_delta 缺失时补 done + completed。

    正常协议下 message_delta → message_stop 结束流；部分网关异常断流时
    只走到 content_block_delta，客户端等不到 response.completed 会挂起。
    """
    if state.completed_sent:
        return []
    if not state.response_id:
        state.response_id = f"resp_{secrets.token_hex(12)}"
    events: list[dict[str, Any]] = []
    for block in state.anthropic_content_blocks.values():
        if not block.get("closed"):
            events.extend(build_block_done_events(state, block))
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": state.response_id,
                "object": "response",
                "created_at": state.created_at,
                "model": state.model,
                "status": "completed",
                "output": build_output_items(state),
                "output_text": state.accumulated_text,
                "usage": anthropic_to_openai_response(state.anthropic_usage),
            },
        }
    )
    state.completed_sent = True
    return events
