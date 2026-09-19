"""Chat Completions → OpenAI Response 流式状态机（ADR-0016 D2 二期：to_response 按方向拆分）。

从 ``converters/to_response`` 纯搬移：拍 1 处理 Chat 流 chunk（``chat_stream_chunk_to_response``），
拍 2 收尾（finish_reason 排队等 usage、``response.completed`` 终事件）。
本模块只持 Chat 方向的状态机逻辑；共享流状态 schema 与不变量转移方法 /
截断防护 / ID 伪造见 ``converters/response_stream_state``（ADR-0022 D0/D1）与
``converters/response_ids``。函数签名收 ``state: ResponseStreamState``（ADR-0022 D2：
conv 参数消亡，chunk 解析直接读写类型化字段）；两拍协议 / usage 排队排空 / 文本关闭 /
终事件组装等不变量转移全部收在 state 方法内。
"""

from typing import Any

from loguru import logger

from converters.response_ids import make_message_id, make_response_id
from converters.response_stream_state import (
    ResponseStreamState,
    append_aggregate_text,
    append_item_text,
    append_tool_arguments,
    make_response_event,
)
from converters.stream_usage import _chat_usage_to_response_acc


def chat_stream_chunk_to_response(state: ResponseStreamState, chunk: dict[str, Any]) -> dict[str, Any] | None:
    if chunk.get("id"):
        state.response_id = chunk["id"]
        if not state.message_id:
            state.message_id = chunk["id"]
    if chunk.get("model"):
        state.model = chunk["model"]
    if chunk.get("created") is not None:
        state.created_at = chunk.get("created", 0)
    if state.response_id and not state.response_id.startswith("resp_"):
        state.response_id = make_response_id(state.response_id)
    if state.message_id and not state.message_id.startswith("msg_"):
        state.message_id = make_message_id(
            state.response_id or "resp_stream",
            state.message_id,
        )

    # 提取 usage（可能在任何 chunk 中，包括 choices 为空的 usage-only chunk）；
    # 累计规则在 stream_usage 模块（ADR-0016 D3）：终值覆写 + 现值回退 + details 重建。
    # 现值回退所需的累计基线由类型化字段组装（dict 兼容访问层已删除，ADR-0022 D2）
    usage = chunk.get("usage")
    if usage:
        acc = {"input_tokens": state.input_tokens, "output_tokens": state.output_tokens, "total_tokens": state.total_tokens}
        merged = _chat_usage_to_response_acc(usage, acc)
        state.input_tokens = merged["input_tokens"]
        state.output_tokens = merged["output_tokens"]
        state.total_tokens = merged["total_tokens"]
        if "input_tokens_details" in merged:
            state.input_tokens_details = merged["input_tokens_details"]
        if "output_tokens_details" in merged:
            state.output_tokens_details = merged["output_tokens_details"]

    choices = chunk.get("choices", [])
    if not choices or choices[0] is None:
        if state.waiting_for_usage_after_finish:
            return state.release_pending()
        return []
    if len(choices) > 1:
        raise ValueError("Chat Completions stream with multiple choices is not supported for Responses conversion")
    # 部分网关的 finish chunk 会显式发 "delta": null，回退空 dict 防 AttributeError
    delta = choices[0].get("delta") or {}
    finish_reason = choices[0].get("finish_reason")

    def _ensure_text_output_item_added() -> bool:
        """确保文本消息的 output_item.added 已发送。返回 True 表示需要发送。"""
        if state.active_text_item_id is not None:
            return False
        idx = state.output_index
        state.output_index = idx + 1
        item_id = state.message_id
        if not item_id.startswith("msg_"):
            item_id = make_message_id(state.response_id or "resp_stream", item_id)
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
        return True

    # 处理第一个 chunk 带 role 的情况（两拍协议：created + in_progress 同拍，
    # 不变量住所 ResponseStreamState.emit_created，ADR-0022 D1）
    if delta.get("role") == "assistant":
        state.message_id = chunk.get("id", "")
        return state.emit_created()

    # 同一个 delta 可能同时携带 content / tool_calls / reasoning_content /
    # finish_reason（部分网关合发），必须全部处理而不是先命中先 return。
    events: list[dict[str, Any]] = []

    # 处理文本内容
    if delta.get("content") is not None:
        text = delta["content"]
        append_aggregate_text(state, "accumulated_text", text)
        item_id = state.message_id
        if not item_id.startswith("msg_"):
            item_id = make_message_id(state.response_id or "resp_stream", item_id)
        text_output_index = state.active_text_output_index

        if _ensure_text_output_item_added():
            text_output_index = state.active_text_output_index
            events.append(
                make_response_event(
                    "response.output_item.added",
                    output_index=text_output_index,
                    item={
                        "type": "message",
                        "id": item_id,
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                )
            )
        if not state.content_part_added_sent:
            state.content_part_added_sent = True
            events.append(
                make_response_event(
                    "response.content_part.added",
                    item_id=item_id,
                    output_index=text_output_index,
                    content_index=0,
                    part={"type": "output_text", "text": ""},
                )
            )
        events.append(
            make_response_event(
                "response.output_text.delta",
                item_id=item_id,
                output_index=text_output_index,
                content_index=0,
                delta=text,
            )
        )
        append_item_text(state, text_output_index, text)

    # 处理工具调用
    if delta.get("tool_calls"):
        # 切到 function_call 之前需要先关闭尚未结束的 text output（C16，
        # 不变量住所 ResponseStreamState.emit_text_done，ADR-0022 D1）
        events.extend(state.emit_text_done())
        for tc in delta["tool_calls"]:
            call_id = tc.get("id", "")
            tc_index = tc.get("index")
            if not call_id and tc_index is not None:
                call_id = state.tool_call_index_to_id.get(tc_index, "")
            function = tc.get("function", {})
            if function.get("name"):
                name = function["name"]
                if not call_id:
                    call_id = f"call_{tc_index}" if tc_index is not None else f"call_{len(state.tool_calls)}"
                if tc_index is not None:
                    state.tool_call_index_to_id[tc_index] = call_id
                idx = state.output_index
                state.output_index = idx + 1
                state.tool_calls[call_id] = {
                    "name": name,
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
                events.append(
                    make_response_event(
                        "response.output_item.added",
                        output_index=idx,
                        item={
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": "",
                            "status": "in_progress",
                        },
                    )
                )
            # 同 chunk 中可能同时携带 name 和 arguments（DeepSeek/Qwen 等高发），
            # 用独立的 if 而非 elif，避免首块 arguments 丢失。
            if function.get("arguments") is not None:
                args = function.get("arguments", "")
                if args:
                    if call_id in state.tool_calls:
                        append_tool_arguments(state, call_id, args)
                    event = make_response_event(
                        "response.function_call_arguments.delta",
                        delta=args,
                    )
                    if tc_index is not None:
                        event["output_index"] = state.tool_calls.get(call_id, {}).get("output_index", tc_index)
                    events.append(event)

    # 处理推理内容
    if delta.get("reasoning_content") is not None:
        rc = delta["reasoning_content"]
        append_aggregate_text(state, "reasoning_content", rc)
        if not state.reasoning_started:
            state.reasoning_started = True
            state.reasoning_id = f"rs_{chunk.get('id', '')}"
            idx = state.output_index
            state.output_index = idx + 1
            state.output_items.append(
                {
                    "type": "reasoning",
                    "output_index": idx,
                    "id": state.reasoning_id,
                }
            )
            events.append(
                make_response_event(
                    "response.output_item.added",
                    output_index=idx,
                    item={
                        "type": "reasoning",
                        "id": state.reasoning_id,
                        "summary": [],
                        "content": [],
                    },
                )
            )
            events.append(
                make_response_event(
                    "response.content_part.added",
                    item_id=state.reasoning_id,
                    output_index=idx,
                    content_index=0,
                    part={"type": "reasoning_text", "text": ""},
                )
            )
        output_index = next(
            (item["output_index"] for item in state.output_items if item.get("type") == "reasoning"),
            0,
        )
        events.append(
            make_response_event(
                "response.reasoning_text.delta",
                item_id=state.reasoning_id,
                output_index=output_index,
                content_index=0,
                delta=rc,
            )
        )

    # 处理结束原因
    if finish_reason is not None:
        logger.debug(f"[CHUNK] finish_reason={finish_reason} response_created_sent={state.response_created_sent}")
        done_events = state.queue_final(finish_reason)
        logger.debug(f"[CHUNK] built {len(done_events)} final events")
        events.extend(done_events)

    if not events:
        return []
    # 两拍协议：response.created 同拍紧随 response.in_progress（不变量住所
    # ResponseStreamState.emit_created，ADR-0022 D1）；已发过时返回空列表
    return state.emit_created() + events
