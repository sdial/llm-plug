"""上游请求的确定性结构辅助。

Provider/模型能力、URL 身份与请求兼容性已经由 Upstream Catalog、
``upstream_profile_resolver`` 和 ``conversion_plan`` 统一拥有。这里不再包含
URL 关键词推断或静默字段过滤，只保留档案动作所需的纯结构变换。
"""

from __future__ import annotations


def merge_system_messages(messages: list[dict]) -> list[dict]:
    """把多条 system 消息合并到首条，保留非 system 消息的原顺序。"""
    system_parts: list[str] = []
    other_messages: list[dict] = []

    for message in messages:
        if message.get("role") != "system":
            other_messages.append(message)
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            if content:
                system_parts.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str) and part:
                    system_parts.append(part)
                elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"]:
                    system_parts.append(part["text"])

    if not system_parts:
        return other_messages
    return [{"role": "system", "content": "\n\n".join(system_parts)}, *other_messages]
