import json
import secrets
import time
from typing import Any


def build_stream_response_body(
    chunks: list[Any],
    is_upstream_anthropic: bool,
    model: str,
) -> dict | None:
    """从流式 chunks 构建完整的响应体用于存储。

    Args:
        chunks: 流式响应的 chunk 列表
        is_upstream_anthropic: 上游是否为 Anthropic API
        model: 模型名称

    Returns:
        拼装后的响应体字典，如果无法构建则返回 None
    """
    if not chunks:
        return None

    if is_upstream_anthropic:
        return build_anthropic_stream_response(chunks, model)
    else:
        return build_openai_stream_response(chunks, model)


def build_anthropic_stream_response(chunks: list[Any], model: str) -> dict | None:
    """构建 Anthropic 格式的流式响应体。"""
    message_id = None
    role = "assistant"
    stop_reason = None
    stop_sequence = None
    usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    blocks: dict[int, dict[str, Any]] = {}
    tool_json_buffers: dict[int, str] = {}

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        chunk_type = chunk.get("type")

        if chunk_type == "message_start":
            msg = chunk.get("message", {})
            message_id = msg.get("id")
            role = msg.get("role", "assistant")
            if isinstance(msg.get("usage"), dict):
                usage.update(msg["usage"])

        elif chunk_type == "content_block_start":
            content_block = chunk.get("content_block", {})
            block_idx = chunk.get("index", 0)
            if isinstance(block_idx, int) and isinstance(content_block, dict):
                blocks[block_idx] = dict(content_block)
                if content_block.get("type") == "tool_use":
                    tool_json_buffers[block_idx] = ""

        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            block_idx = chunk.get("index", 0)
            delta_type = delta.get("type")
            block = blocks.setdefault(block_idx, {"type": "text", "text": ""})
            if delta_type == "text_delta":
                block["type"] = block.get("type") or "text"
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta_type == "thinking_delta":
                block["type"] = block.get("type") or "thinking"
                block["thinking"] = block.get("thinking", "") + delta.get(
                    "thinking", ""
                )
            elif delta_type == "signature_delta":
                block["signature"] = delta.get("signature", "")
            elif delta_type == "input_json_delta":
                tool_json_buffers[block_idx] = tool_json_buffers.get(
                    block_idx, ""
                ) + delta.get("partial_json", "")

        elif chunk_type == "content_block_stop":
            block_idx = chunk.get("index", 0)
            if block_idx in tool_json_buffers and block_idx in blocks:
                buffer = tool_json_buffers[block_idx]
                try:
                    blocks[block_idx]["input"] = json.loads(buffer) if buffer else {}
                except json.JSONDecodeError:
                    # 上游 partial_json 拼装异常：保留空 input，避免把内部 buffer 写入日志/响应记录。
                    blocks[block_idx]["input"] = {}

        elif chunk_type == "message_delta":
            delta = chunk.get("delta", {})
            stop_reason = delta.get("stop_reason")
            stop_sequence = delta.get("stop_sequence")
            if isinstance(chunk.get("usage"), dict):
                usage.update(chunk["usage"])

    if not message_id:
        # 尝试从其他 chunk 中获取 id
        for chunk in chunks:
            if isinstance(chunk, dict) and chunk.get("id"):
                message_id = chunk["id"]
                break

    if not message_id:
        return None

    content_blocks = [blocks[idx] for idx in sorted(blocks)]

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    return {
        "id": message_id,
        "type": "message",
        "role": role,
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": usage,
    }


def build_openai_stream_response(chunks: list[Any], model: str) -> dict | None:
    """构建 OpenAI 格式的流式响应体。"""
    response_id = None
    input_tokens = 0
    output_tokens = 0
    total_tokens: int | None = None
    prompt_details: dict | None = None
    completion_details: dict | None = None
    choice_states: dict[int, dict[str, Any]] = {}

    def get_choice_state(index: int) -> dict[str, Any]:
        if index not in choice_states:
            choice_states[index] = {
                "role": "assistant",
                "content_text": "",
                "reasoning_text": "",
                "finish_reason": None,
                "tool_calls_map": {},
            }
        return choice_states[index]

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        # 获取 id（通常在第一个 chunk）
        if not response_id and chunk.get("id"):
            response_id = chunk["id"]

        # 拼接每个 choice 的内容；OpenAI stream 支持 n > 1。
        choices = chunk.get("choices", [])
        if isinstance(choices, list):
            for position, choice in enumerate(choices):
                if not isinstance(choice, dict):
                    continue
                index = choice.get("index", position)
                if not isinstance(index, int):
                    index = position
                state = get_choice_state(index)
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    delta = {}

                if delta.get("role"):
                    state["role"] = delta["role"]
                if delta.get("content"):
                    state["content_text"] += delta["content"]
                # 拼接 reasoning_content（如 DeepSeek 的思考内容）
                if delta.get("reasoning_content"):
                    state["reasoning_text"] += delta["reasoning_content"]

                # 拼接 tool_calls
                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    tool_calls_map = state["tool_calls_map"]
                    for tc in tool_calls:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc.get("id", ""),
                                "type": tc.get("type", "function"),
                                "function": {"name": "", "arguments": ""},
                            }
                        tool_call = tool_calls_map[idx]
                        # 更新 id（第一个 chunk 可能有）
                        if tc.get("id"):
                            tool_call["id"] = tc["id"]
                        # 更新 type
                        if tc.get("type"):
                            tool_call["type"] = tc["type"]
                        # 拼接 function 字段
                        func = tc.get("function", {})
                        if func.get("name"):
                            tool_call["function"]["name"] = func["name"]
                        if func.get("arguments"):
                            tool_call["function"]["arguments"] += func["arguments"]

                # 获取 finish_reason
                fr = choice.get("finish_reason")
                if fr:
                    state["finish_reason"] = fr

        # 获取 usage（可能在最后一个 chunk）
        usage = chunk.get("usage")
        if usage:
            input_tokens = usage.get("prompt_tokens", input_tokens)
            output_tokens = usage.get("completion_tokens", output_tokens)
            # 优先使用上游的 total_tokens
            if usage.get("total_tokens") is not None:
                total_tokens = usage["total_tokens"]
            # 透传 prompt_tokens_details
            pd = usage.get("prompt_tokens_details")
            if isinstance(pd, dict):
                prompt_details = pd
            # 透传 completion_tokens_details
            cd = usage.get("completion_tokens_details")
            if isinstance(cd, dict):
                completion_details = cd

    if not response_id:
        response_id = f"chatcmpl-{secrets.token_hex(12)}"

    if not choice_states:
        get_choice_state(0)

    response_choices: list[dict[str, Any]] = []
    for index in sorted(choice_states):
        state = choice_states[index]
        message: dict[str, Any] = {
            "role": state["role"],
            "content": state["content_text"] if state["content_text"] else None,
        }
        # 添加 reasoning_content（如有）
        if state["reasoning_text"]:
            message["reasoning_content"] = state["reasoning_text"]

        # 如果有 tool_calls，按 index 顺序添加到 message
        tool_calls_map = state["tool_calls_map"]
        if tool_calls_map:
            tool_calls_list = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())]
            message["tool_calls"] = tool_calls_list

        response_choices.append(
            {
                "index": index,
                "message": message,
                "finish_reason": state["finish_reason"],
            }
        )

    # 构建 usage 字段
    final_usage: dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens
        if total_tokens is not None
        else input_tokens + output_tokens,
    }
    if prompt_details is not None:
        final_usage["prompt_tokens_details"] = prompt_details
    if completion_details is not None:
        final_usage["completion_tokens_details"] = completion_details

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": response_choices,
        "usage": final_usage,
    }


_build_stream_response_body = build_stream_response_body
_build_anthropic_stream_response = build_anthropic_stream_response
_build_openai_stream_response = build_openai_stream_response
