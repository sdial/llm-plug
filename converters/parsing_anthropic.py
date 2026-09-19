"""Anthropic Messages 源语法解析（ADR-0016 D2 二期）。

把 Anthropic 请求体解析为最小化中间条目（形态见 ``converters/intermediate.py``），
供 to_chat / to_response 两个方向共用——原先 to_chat 与 to_response 各自解析一遍
Anthropic 语法的成对复制段随本模块归零。渲染段（中间条目 → 目标语法）留在各
目标转换器。
"""

from typing import Any

from converters.intermediate import message_entry

# Anthropic stop_reason → 中间 finish 条目（Chat finish_reason 词表）。
# 单一住所：非流式响应转换与流式 message_delta 共用（原 to_chat._map_stop_reason）。
ANTHROPIC_STOP_REASONS_TO_FINISH: dict[str | None, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def parse_anthropic_stop_reason(reason: str | None) -> str:
    """Anthropic stop_reason → 中间 finish 条目；未知值兜底 ``stop``。"""
    return ANTHROPIC_STOP_REASONS_TO_FINISH.get(reason, "stop")


def parse_anthropic_system(system: Any) -> list[str]:
    """Anthropic system（str 或块列表）→ 文本片段列表。

    仅提取 text 块与字符串片段（与原 to_chat 行为一致）；空字符串 system
    返回空列表（调用方 ``if`` 判空后不产出 system 字段）。
    """
    if not system:
        return []
    if isinstance(system, str):
        return [system]
    if isinstance(system, list):
        text_parts = []
        for part in system:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part["text"])
            elif isinstance(part, str):
                text_parts.append(part)
        return text_parts
    return []


def parse_anthropic_tools(tools: list) -> list[dict[str, Any]]:
    """Anthropic tools → 中间工具条目。

    Anthropic 规范：type 可省略（默认即工具）或为 "custom"；携带 ``name`` 即
    收录。``parameters`` 为 ``None`` 表示源未提供 input_schema（是否接受无 schema
    工具由渲染端决定：Chat 目标要求 schema，Responses 目标默认空 schema）。
    """
    entries = []
    for tool in tools:
        if "name" not in tool:
            continue
        entry = {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema"),
        }
        if tool.get("strict") is not None:
            entry["strict"] = tool["strict"]
        entries.append(entry)
    return entries


def parse_anthropic_tool_choice(value: Any) -> str | dict[str, Any] | None:
    """Anthropic tool_choice → 中间条目；无法识别的形态返回 None。"""
    if not isinstance(value, dict):
        return None
    tc_type = value.get("type")
    if tc_type == "auto":
        return "auto"
    if tc_type == "any":
        return "required"
    if tc_type == "none":
        return "none"
    if tc_type == "tool":
        return {"type": "function", "name": value.get("name", "")}
    return None


def parse_anthropic_content_parts(content: list) -> list[dict[str, Any]]:
    """Anthropic content 块列表 → kind part 条目（tool_use/tool_result 提升到条目级）。"""
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append({"kind": "text", "text": str(part)})
            continue
        part_type = part.get("type")
        if part_type == "text":
            parts.append({"kind": "text", "text": part.get("text", "")})
        elif part_type == "image":
            source = part.get("source", {}) or {}
            if source.get("type") == "base64":
                parts.append({"kind": "image", "media_type": source.get("media_type", "image/png"), "data": source.get("data", "")})
            elif source.get("type") == "url":
                parts.append({"kind": "image", "url": source.get("url", "")})
            # 其余 source 形态两个目标方向均静默丢弃，保持原行为
        elif part_type == "document":
            doc_source = part.get("source", {}) or {}
            block: dict[str, Any] = {"kind": "document"}
            if doc_source.get("type") == "base64":
                block["media_type"] = doc_source.get("media_type", "application/pdf")
                block["data"] = doc_source.get("data", "")
            elif doc_source.get("type") == "url":
                block["url"] = doc_source.get("url", "")
            elif doc_source.get("type") == "content":
                block["content"] = doc_source.get("content", "")
            parts.append(block)
        elif part_type == "search_result":
            parts.append({"kind": "search_result", "content": part.get("content", "")})
        elif part_type == "thinking":
            parts.append({"kind": "thinking", "text": part.get("thinking", "")})
        elif part_type == "redacted_thinking":
            parts.append({"kind": "redacted_thinking"})
        elif part_type == "tool_use":
            parts.append(
                {
                    "kind": "tool_use",
                    "id": part.get("id", ""),
                    "name": part.get("name", ""),
                    "input": part.get("input", {}),
                }
            )
        elif part_type == "tool_result":
            parts.append(
                {
                    "kind": "tool_result",
                    "tool_use_id": part.get("tool_use_id", ""),
                    "content": part.get("content", ""),
                    "is_error": part.get("is_error", False),
                }
            )
        else:
            # 未识别块原样保留；带 "text" 键的降级策略由渲染端决定
            # （Chat 目标取 text 兜底，Responses 目标输出类型标记）
            parts.append({"kind": "unknown", "block": part})
    return parts


def parse_anthropic_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic 请求体 messages → 消息中间条目列表（system 走 parse_anthropic_system）。"""
    entries: list[dict[str, Any]] = []
    for msg in data.get("messages", []):
        role = msg["role"]
        content = msg.get("content", "")
        entry = message_entry(role)
        if isinstance(content, str):
            entry["text"] = content
        elif isinstance(content, list):
            for part in parse_anthropic_content_parts(content):
                entry["parts"].append(part)
                if part["kind"] == "tool_use":
                    # tool_use/tool_result 双写：条目级供 Chat 渲染按消息组装，
                    # parts 内保留原始顺序供 Responses 渲染按位置展开
                    entry["tool_calls"].append({"id": part["id"], "name": part["name"], "arguments": part["input"]})
                elif part["kind"] == "tool_result":
                    entry["tool_results"].append(
                        {
                            "tool_use_id": part["tool_use_id"],
                            "content": part["content"],
                            "is_error": part["is_error"],
                            "parts": None,
                        }
                    )
        entries.append(entry)
    return entries


def flatten_anthropic_tool_result(content: Any) -> str:
    """Anthropic tool_result content（str 或块列表）→ 纯文本。

    图片块转为可读占位符避免静默丢失；供 Chat / Responses 两个目标方向共用。
    """
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    src = item.get("source", {}) or {}
                    src_type = src.get("type", "")
                    if src_type == "base64":
                        media_type = src.get("media_type", "image/*")
                        text_parts.append(f"[Image: {media_type} (base64, omitted in tool message)]")
                    elif src_type == "url":
                        text_parts.append(f"[Image: {src.get('url', '')}]")
                    else:
                        text_parts.append("[Image (unsupported in tool message)]")
                elif "text" in item:
                    text_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                text_parts.append(item)
        return "\n".join(text_parts)
    return str(content) if content else ""
