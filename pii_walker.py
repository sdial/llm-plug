"""字段感知遍历：只把业务文本字段交给 PII 识别器。

使用迭代栈避免深层嵌套导致 RecursionError；按 target_api_type 白名单定位 leaf。
"""

from __future__ import annotations

from collections import deque
from typing import Any

_FIELD_WHITELIST: dict[str, list[str]] = {
    "openai-chat-completions": [
        "messages[*].content",
        "messages[*].content[*].text",
        "messages[*].tool_result_content[*].text",
    ],
    # 管理端旧测试/调用兼容；生产路径使用 openai-chat-completions。
    "openai-chat": [
        "messages[*].content",
        "messages[*].content[*].text",
        "messages[*].tool_result_content[*].text",
    ],
    "anthropic": [
        "system",
        "system[*].text",
        "messages[*].content[*].text",
    ],
    "openai-response": [
        "input[*].content[*].text",
        "input[*].content[*].value",
        "instructions",
    ],
}


def _looks_like_base64_data_url(text: str) -> bool:
    return isinstance(text, str) and text.startswith("data:")


def _parse_pattern(pattern: str) -> list[str]:
    parts: list[str] = []
    for seg in pattern.split("."):
        if seg.endswith("[*]"):
            field = seg[:-3]
            if field:
                parts.append(field)
            parts.append("*")
        elif seg == "[*]":
            parts.append("*")
        else:
            parts.append(seg)
    return parts


def _iter_wildcard_items(obj: Any) -> list[tuple[str | int, Any]]:
    if isinstance(obj, dict):
        return list(obj.items())
    if isinstance(obj, list):
        return list(enumerate(obj))
    return []


def _collect_leaves_by_pattern(root: Any, pattern_parts: list[str], out: list[tuple[Any, str | int, str]]) -> None:
    """按 pattern_parts 迭代定位文本 leaf，返回 (container, key, text) 三元组。"""
    queue: deque[tuple[Any, list[str], Any | None, str | int | None]] = deque([(root, pattern_parts, None, None)])
    while queue:
        obj, parts, parent, key = queue.popleft()
        if not parts:
            continue

        head = parts[0]
        rest = parts[1:]

        if head == "*":
            for child_key, child_value in _iter_wildcard_items(obj):
                if rest:
                    queue.append((child_value, rest, obj, child_key))
                elif isinstance(child_value, str) and not _looks_like_base64_data_url(child_value):
                    out.append((obj, child_key, child_value))
        elif isinstance(obj, dict) and head in obj:
            value = obj[head]
            if rest:
                queue.append((value, rest, obj, head))
            elif isinstance(value, str) and not _looks_like_base64_data_url(value):
                out.append((obj, head, value))


def walk_text_leaves(upstream_data: dict, target_api_type: str) -> list[tuple[Any, str | int, str]]:
    """返回 (container, key, text) 三元组列表，便于调用方原地修改。"""
    out: list[tuple[Any, str | int, str]] = []
    if not isinstance(upstream_data, dict):
        return out
    for pattern in _FIELD_WHITELIST.get(target_api_type, []):
        _collect_leaves_by_pattern(upstream_data, _parse_pattern(pattern), out)
    return out
