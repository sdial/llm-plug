"""Caveman 提示词注入。

将固定的 Caveman 指令追加到上游请求 system 内容的头部。注入位置固定、文本固定，
因此是确定性操作，不会破坏 prompt cache；反而因为输出被压缩，可降低输出 token 成本。
"""

from __future__ import annotations

from loguru import logger

from models.api_types import APIType

# 与 PLAN.md 保持一致的 Caveman 提示词文本（确定性、位置固定）
CAVEMAN_PROMPT = (
    "Enable Caveman output style for coding tasks.\n"
    "RULES:\n"
    "1. Use concise technical statements. Remove conversational filler.\n"
    "2. Prefer dense SVO sentences. Use -> for causality and + - | for relations.\n"
    "3. Preserve user code, markdown, identifiers, paths, commands, logs, and technical terms exactly unless explicitly asked to change.\n"
    "4. Provide complete, executable code blocks. Avoid pseudo-code when implementation is expected.\n"
    "5. Example: \"Need fast query -> add index on user_id column.\"\n\n"
)

# 注入后在提示词与用户原 system 内容之间留一空行，避免粘连。
_CAVEMAN_PREFIX = CAVEMAN_PROMPT + "\n\n"


def _api_value(api_type) -> str:
    if isinstance(api_type, APIType):
        return api_type.value
    return str(api_type)


def _is_supported_upstream(api_type) -> bool:
    value = _api_value(api_type)
    return value in {APIType.OPENAI_CHAT.value, APIType.ANTHROPIC.value}


def _prepend_to_text(text: str) -> str:
    if text == CAVEMAN_PROMPT or text.startswith(_CAVEMAN_PREFIX):
        return text
    return _CAVEMAN_PREFIX + text


def _new_text_content() -> str:
    """创建全新的 system 内容时使用的 Caveman 文本（无后续分隔符）。"""
    return CAVEMAN_PROMPT


def _inject_openai_chat(payload: dict) -> bool:
    """为 OpenAI Chat Completions 格式注入 Caveman 提示词。

    在 messages 中第一个 role == system 的 content 头部追加；
    若无 system 消息，在 messages[0] 插入新 system 消息。
    content 支持 str 或 text block 列表。
    返回是否发生了注入。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False

    first_system_index = None
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "system":
            first_system_index = idx
            break

    if first_system_index is not None:
        msg = messages[first_system_index]
        content = msg.get("content")
        if isinstance(content, str):
            new_content = _prepend_to_text(content)
            if new_content != content:
                msg["content"] = new_content
                return True
            return False
        if isinstance(content, list):
            # 若列表第一个元素就是 text 块，直接在其文本头部追加；
            # 否则在列表头部插入新的 Caveman text block，确保 Caveman 始终位于 system 内容最前。
            if (
                content
                and isinstance(content[0], dict)
                and content[0].get("type") == "text"
            ):
                text = content[0].get("text", "")
                new_text = _prepend_to_text(text)
                if new_text != text:
                    content[0]["text"] = new_text
                    return True
                return False
            content.insert(0, {"type": "text", "text": _new_text_content()})
            return True
        # 其他 content 类型（如 None）不处理。
        return False

    # 没有 system 消息：在 messages 头部插入。
    messages.insert(
        0, {"role": "system", "content": _new_text_content()}
    )
    return True


def _inject_anthropic(payload: dict) -> bool:
    """为 Anthropic Messages 格式注入 Caveman 提示词。

    Anthropic 上游使用顶层 system 字段（str 或 text block 列表）。
    若不存在 system 字段，则创建为字符串。
    返回是否发生了注入。
    """
    system = payload.get("system")

    if isinstance(system, str):
        new_system = _prepend_to_text(system)
        if new_system != system:
            payload["system"] = new_system
            return True
        return False

    if isinstance(system, list):
        # 若列表第一个元素就是 text 块，直接在其文本头部追加；
        # 否则在列表头部插入新的 Caveman text block，确保 Caveman 始终位于 system 最前。
        if (
            system
            and isinstance(system[0], dict)
            and system[0].get("type") == "text"
        ):
            text = system[0].get("text", "")
            new_text = _prepend_to_text(text)
            if new_text != text:
                system[0]["text"] = new_text
                return True
            return False
        system.insert(0, {"type": "text", "text": _new_text_content()})
        return True

    # system 不存在或类型不识别：创建为字符串。
    payload["system"] = _new_text_content()
    return True


def inject_caveman_prompt(payload: dict, upstream_api_type) -> bool:
    """向上游请求 payload 注入 Caveman 提示词（原地修改）。

    仅支持 OpenAI Chat Completions 与 Anthropic Messages 上游格式；
    其他格式或已注入时返回 False。

    Args:
        payload: 上游请求体（已转换为上游格式）。
        upstream_api_type: 上游 API 类型（APIType 枚举或字符串）。

    Returns:
        是否实际发生了注入。
    """
    if not _is_supported_upstream(upstream_api_type):
        return False

    value = _api_value(upstream_api_type)
    injected = (
        _inject_openai_chat(payload)
        if value == APIType.OPENAI_CHAT.value
        else _inject_anthropic(payload)
    )

    if injected:
        logger.debug(
            f"[CTX_OPT] Caveman prompt injected for upstream={value}"
        )
    return injected
