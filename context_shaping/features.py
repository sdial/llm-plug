"""Context Shaping 的独立、事务式非 Prompt feature。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from models.api_types import APIType

_COLLAPSE_BLANK_RE = re.compile(r"\n{3,}")


def action(feature: str, action_name: str, path: str, before: str | Any, after: str | Any) -> dict[str, Any]:
    def size(value: Any) -> int:
        return len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))

    return {
        "feature": feature,
        "action": action_name,
        "field_path": path,
        "hit_count": 1,
        "before_chars": size(before),
        "after_chars": size(after),
    }


def _text_list_leaves(items: list, path: str):
    for index, item in enumerate(items):
        item_path = f"{path}[*]"
        if isinstance(item, str):
            yield items, index, item, item_path
        elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str):
                yield item, "text", text, f"{item_path}.text"


def tool_text_leaves(payload: dict[str, Any], api_type: str):
    if api_type == APIType.OPENAI_RESPONSE.value:
        for item in payload.get("input", []):
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            output = item.get("output")
            if isinstance(output, str):
                yield item, "output", output, "input[*].output"
            elif isinstance(output, list):
                yield from _text_list_leaves(output, "input[*].output")
        return
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if api_type == APIType.OPENAI_CHAT.value and message.get("role") == "tool":
            if isinstance(content, str):
                yield message, "content", content, "messages[*].content"
            elif isinstance(content, list):
                yield from _text_list_leaves(content, "messages[*].content")
        elif api_type == APIType.ANTHROPIC.value and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                result = block.get("content")
                if isinstance(result, str):
                    yield block, "content", result, "messages[*].content[*].content"
                elif isinstance(result, list):
                    yield from _text_list_leaves(result, "messages[*].content[*].content")


def transform_tool_text(
    payload: dict[str, Any], api_type: str, feature: str, action_name: str, transform: Callable[[str], str]
) -> list[dict[str, Any]]:
    actions = []
    for container, key, text, path in tool_text_leaves(payload, api_type):
        changed = transform(text)
        if changed != text:
            container[key] = changed
            actions.append(action(feature, action_name, path, text, changed))
    return actions


def trim_trailing_whitespace(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def collapse_blank_lines(text: str) -> str:
    return _COLLAPSE_BLANK_RE.sub("\n\n", text)


def dedupe_consecutive_lines(text: str) -> str:
    lines = text.split("\n")
    result: list[str] = []
    previous: str | None = None
    for line in lines:
        if line and line == previous:
            continue
        result.append(line)
        previous = line
    return "\n".join(result)


def _collect_tool_calls(payload: dict[str, Any], api_type: str) -> set[str]:
    found: set[str] = set()
    source = payload.get("input", []) if api_type == APIType.OPENAI_RESPONSE.value else payload.get("messages", [])
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        if api_type == APIType.OPENAI_RESPONSE.value and item.get("type") in {"function_call", "custom_tool_call"}:
            value = item.get("call_id") or item.get("id")
            if isinstance(value, str):
                found.add(value)
        for call in item.get("tool_calls", []) if isinstance(item.get("tool_calls"), list) else []:
            if isinstance(call, dict) and isinstance(call.get("id"), str):
                found.add(call["id"])
        for block in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                found.add(block["id"])
    return found


def strip_unreferenced_tool_results(payload: dict[str, Any], api_type: str) -> list[dict[str, Any]]:
    calls = _collect_tool_calls(payload, api_type)
    key = "input" if api_type == APIType.OPENAI_RESPONSE.value else "messages"
    items = payload.get(key)
    if not isinstance(items, list):
        return []
    before = list(items)
    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        if api_type == APIType.OPENAI_RESPONSE.value and item.get("type") == "function_call_output":
            if item.get("call_id") not in calls:
                continue
        elif api_type == APIType.OPENAI_CHAT.value and item.get("role") == "tool":
            if item.get("tool_call_id") not in calls:
                continue
        elif api_type == APIType.ANTHROPIC.value and isinstance(item.get("content"), list):
            content = [
                block
                for block in item["content"]
                if not (isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") not in calls)
            ]
            if content != item["content"]:
                if not content:
                    continue
                item = {**item, "content": content}
        result.append(item)
    if result == before:
        return []
    payload[key] = result
    return [action("strip_unreferenced_tool_results", "remove_unreferenced_tool_result", f"{key}[*]", before, result)]


def dedupe_adjacent_users(payload: dict[str, Any], api_type: str) -> list[dict[str, Any]]:
    key = "input" if api_type == APIType.OPENAI_RESPONSE.value else "messages"
    items = payload.get(key)
    if not isinstance(items, list):
        return []
    result: list[Any] = []
    removed: list[Any] = []
    for item in items:
        is_user = isinstance(item, dict) and item.get("role") == "user"
        if is_user and result and isinstance(result[-1], dict) and result[-1].get("role") == "user" and result[-1] == item:
            removed.append(item)
            continue
        result.append(item)
    if not removed:
        return []
    payload[key] = result
    return [action("dedupe_adjacent_user_messages", "remove_duplicate_user_message", f"{key}[*]", items, result)]
