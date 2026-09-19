"""
Think 块过滤包装：响应级 / 流式 chunk 级。

底层状态机复用根级 think_filter.ThinkFilter（跨 chunk 保留 💭...💭 块边界），
本模块只承载包装函数（真实实现，供 proxy.routing 聚合）。
"""

from typing import Any

from think_filter import ThinkFilter, filter_think_content_static


def _filter_think_in_response(response_data: dict[str, Any]) -> dict[str, Any]:
    """过滤响应中的 💭 内容。

    支持两种响应格式：
    - Chat Completions: choices[].message.content
    - Responses: output[].content[].text

    Args:
        response_data: 原始响应数据

    Returns:
        过滤后的响应数据
    """
    if not isinstance(response_data, dict):
        return response_data

    result = dict(response_data)

    # Chat Completions 格式
    choices = result.get("choices", [])
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message", {})
        if "content" in msg and isinstance(msg["content"], str):
            msg = dict(msg)
            msg["content"] = filter_think_content_static(msg["content"])
            result["choices"] = [dict(choices[0], message=msg)]
        return result

    # Responses 格式
    output = result.get("output", [])
    if output and isinstance(output, list):
        new_output = []
        for item in output:
            if not isinstance(item, dict):
                new_output.append(item)
                continue
            if item.get("type") == "message":
                content = item.get("content", [])
                if isinstance(content, list):
                    new_content = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in (
                            "output_text",
                            "input_text",
                        ):
                            text = part.get("text", "")
                            part = dict(part, text=filter_think_content_static(text))
                        new_content.append(part)
                    item = dict(item, content=new_content)
            new_output.append(item)
        result["output"] = new_output

    # 更新 output_text
    if "output_text" in result:
        result["output_text"] = filter_think_content_static(result["output_text"])

    return result


def _filter_think_in_stream_chunk(chunk: dict[str, Any], think_filter: ThinkFilter) -> dict[str, Any] | None:
    """过滤流式 chunk 中的 💭 内容。

    处理三种格式：
    - Chat Completions: choices[].delta.content
    - Responses: response.output_text.delta 事件
    - Anthropic: content_block_delta 的 text_delta（M2：Anthropic 目标格式此前零过滤）

    Args:
        chunk: 流式 chunk 数据
        think_filter: ThinkFilter 实例

    Returns:
        过滤后的 chunk，如果整个 chunk 都被过滤则返回 None
    """
    if not isinstance(chunk, dict):
        return chunk

    event_type = chunk.get("type", "")

    # Anthropic 格式：content_block_delta 的 text_delta（💭 思考块可能内嵌在正文里）
    delta_obj = chunk.get("delta")
    if event_type == "content_block_delta" and isinstance(delta_obj, dict) and delta_obj.get("type") == "text_delta":
        text = delta_obj.get("text")
        if text and isinstance(text, str):
            filtered = think_filter.feed(text)
            if not filtered:
                return None
            return dict(chunk, delta=dict(delta_obj, text=filtered))
        return chunk

    # Responses 格式：response.output_text.delta
    if event_type == "response.output_text.delta":
        delta = chunk.get("delta", "")
        if delta:
            filtered = think_filter.feed(delta)
            if not filtered:
                return None
            return dict(chunk, delta=filtered)

    # Chat Completions 格式：choices[].delta.content
    choices = chunk.get("choices", [])
    if choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta", {})
        content = delta.get("content")
        if content and isinstance(content, str):
            filtered = think_filter.feed(content)
            if not filtered:
                return None
            new_choice = dict(choices[0])
            new_choice["delta"] = dict(delta, content=filtered)
            return dict(chunk, choices=[new_choice])

    return chunk


__all__ = [
    "_filter_think_in_response",
    "_filter_think_in_stream_chunk",
]
