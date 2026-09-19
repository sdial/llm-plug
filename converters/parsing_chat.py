"""Chat Completions 源语法解析（ADR-0016 D2 二期）。

把 Chat Completions 请求体解析为最小化中间条目（形态见
``converters/intermediate.py``），供 to_anthropic / to_response 两个方向共用——
原先 to_anthropic 与 to_response 各自解析一遍 Chat 语法的成对复制段随本模块归零。
渲染段（中间条目 → 目标语法）留在各目标转换器。
"""

from typing import Any

from loguru import logger

from converters.intermediate import message_entry


def parse_chat_tools(tools: list) -> list[dict[str, Any]]:
    """Chat tools → 中间工具条目（仅 ``type == "function"`` 的工具）。"""
    entries = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        entry = {
            "type": "function",
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "parameters": func.get("parameters"),
        }
        if func.get("strict") is not None:
            entry["strict"] = func["strict"]
        entries.append(entry)
    return entries


def parse_chat_tool_choice(value: Any) -> str | dict[str, Any] | None:
    """Chat tool_choice → 中间条目；无法识别的形态返回 None（由渲染端决定丢弃）。

    兼容官方嵌套形态 ``{"type":"function","function":{"name":...}}`` 与扁平形态
    ``{"type":"function","name":...}}``；``disable_parallel_tool_use`` 为 OpenAI
    扩展字段，此处仅记录日志（下游均无对应）。
    """
    if isinstance(value, str):
        return value if value in ("auto", "none", "required") else None
    if isinstance(value, dict):
        tc_type = value.get("type")
        if tc_type in ("auto", "required", "none"):
            return tc_type
        if tc_type == "function":
            func_info = value.get("function")
            name = func_info.get("name", "") if isinstance(func_info, dict) else ""
            if not name:
                name = value.get("name", "")
            return {"type": "function", "name": name}
        if value.get("disable_parallel_tool_use"):
            logger.debug("OpenAI tool_choice.disable_parallel_tool_use not supported, ignored")
    return None


def parse_chat_finish_reason(finish_reason: str | None) -> str | None:
    """Chat finish_reason → 中间 finish 条目。

    Chat 词表即中间词表（identity）；``function_call`` 为废弃别名，归一为
    ``tool_calls``。
    """
    if finish_reason == "function_call":
        return "tool_calls"
    return finish_reason


def parse_chat_content_parts(content: list) -> list[dict[str, Any]]:
    """Chat 多模态 content 列表 → kind part 条目（形态见 intermediate.py）。"""
    parts: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            parts.append({"kind": "text", "text": item})
            continue
        if not isinstance(item, dict):
            parts.append({"kind": "text", "text": str(item)})
            continue
        item_type = item.get("type")
        if item_type in ("text", "input_text", "output_text"):
            parts.append({"kind": "text", "text": item.get("text", "")})
        elif item_type == "image_url":
            image_url = item.get("image_url")
            url = image_url.get("url", "") if isinstance(image_url, dict) else ""
            part: dict[str, Any] = {"kind": "image", "url": url}
            if isinstance(image_url, dict) and image_url.get("detail"):
                part["detail"] = image_url["detail"]
            parts.append(part)
        elif item_type == "input_audio":
            parts.append({"kind": "audio", "audio": item.get("input_audio") or {}})
        elif item_type == "file":
            parts.append({"kind": "file", "file": _file_fields(item.get("file") or {})})
        elif item_type == "refusal":
            parts.append({"kind": "refusal", "text": item.get("refusal", "")})
        else:
            # 未识别块原样保留；带 "text" 键的降级策略由渲染端决定
            # （Chat/Responses 目标取 text 兜底，Anthropic 目标输出类型标记）
            parts.append({"kind": "unknown", "block": item})
    return parts


def _file_fields(file_info: dict) -> dict[str, Any]:
    """提取 file 块的 file_id / filename / file_data 字段（保留原值）。"""
    return {key: file_info[key] for key in ("file_id", "filename", "file_data") if file_info.get(key) is not None}


def parse_chat_messages(data: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Chat 请求体 → (system 原始内容列表, 消息中间条目列表)。

    - system 内容原样保留（str 或块列表）：to_anthropic 渲染时对 text dict
      原样透传（保留 cache_control 等扩展字段）；to_response 取最后一条为
      instructions。
    - ``developer`` 角色按源语法保留为消息条目（to_anthropic 归入 system，
      to_response 原样透传 developer 消息）。
    """
    system_contents: list[Any] = []
    entries: list[dict[str, Any]] = []
    for msg in data.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_contents.append(content)
            continue
        entry = message_entry(role)
        if role == "developer":
            # developer 消息按源语法保留原样 content：to_anthropic 渲染进 system
            # 时对 text dict 原样透传（与 system 消息同一处理）
            entry["raw_content"] = content
        if isinstance(content, str):
            entry["text"] = content
        elif isinstance(content, list):
            entry["parts"] = parse_chat_content_parts(content)
        if role == "assistant":
            reasoning = msg.get("reasoning_content")
            if reasoning:
                entry["reasoning"] = reasoning
            for tc in msg.get("tool_calls") or []:
                func = tc.get("function", {})
                entry["tool_calls"].append(
                    {
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", "{}"),
                    }
                )
        elif role == "tool":
            entry["tool_results"].append(
                {
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content,
                    "is_error": False,
                    "parts": parse_chat_content_parts(content) if isinstance(content, list) else None,
                }
            )
        entries.append(entry)
    return system_contents, entries
