"""Prompt Extension 组合、三格式落位与短生命周期完整性证明。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from models.api_types import APIType

CAVEMAN_EXTENSION_ID = "caveman"
CAVEMAN_VERSION = 1
CAVEMAN_PROMPT = (
    "Enable Caveman output style for coding tasks.\n"
    "RULES:\n"
    "1. Use concise technical statements. Remove conversational filler.\n"
    "2. Prefer dense SVO sentences. Use -> for causality and + - | for relations.\n"
    "3. Preserve user code, markdown, identifiers, paths, commands, logs, and technical terms exactly unless explicitly asked to change.\n"
    "4. Provide complete, executable code blocks. Avoid pseudo-code when implementation is expected.\n"
    '5. Example: "Need fast query -> add index on user_id column."'
)
CUSTOM_EXTENSION_ID = "custom"
MAX_CUSTOM_PROMPT_BYTES = 32768


class PromptInjectionError(ValueError):
    """已启用的 Prompt Extension 无法按上游格式落位。"""


class PromptIntegrityError(ValueError):
    """PII 处理改变了管理员配置的 Prompt Extension。"""


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_custom_prompt(text: str, *, enabled: bool) -> None:
    if len(text.encode("utf-8")) > MAX_CUSTOM_PROMPT_BYTES:
        raise ValueError("custom prompt exceeds 32768 UTF-8 bytes")
    if enabled and not text.strip():
        raise ValueError("enabled custom prompt must not be blank")


def compose_extensions(*, caveman_enabled: bool, custom_enabled: bool, custom_text: str) -> tuple[str, list[dict[str, Any]]]:
    validate_custom_prompt(custom_text, enabled=custom_enabled)
    segments: list[str] = []
    metadata: list[dict[str, Any]] = []
    if caveman_enabled:
        segments.append(CAVEMAN_PROMPT)
        metadata.append(
            {
                "extension_id": CAVEMAN_EXTENSION_ID,
                "version": CAVEMAN_VERSION,
                "utf8_bytes": len(CAVEMAN_PROMPT.encode("utf-8")),
                "sha256": prompt_hash(CAVEMAN_PROMPT),
            }
        )
    if custom_enabled:
        segments.append(custom_text)
        metadata.append(
            {
                "extension_id": CUSTOM_EXTENSION_ID,
                "version": None,
                "utf8_bytes": len(custom_text.encode("utf-8")),
                "sha256": prompt_hash(custom_text),
            }
        )
    return "\n\n".join(segments), metadata


def _prepend(existing: str, prefix: str) -> str:
    return prefix if not existing else f"{prefix}\n\n{existing}"


def inject_prompt(payload: dict[str, Any], api_type: str, prefix: str) -> str:
    """把组合前缀放到实际格式的权威 system 载体，返回落位说明。"""
    if not prefix:
        return "none"
    if api_type == APIType.OPENAI_CHAT.value:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise PromptInjectionError("prompt_injection_invalid_messages")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = _prepend(content, prefix)
                return "messages[*].content"
            if isinstance(content, list):
                content.insert(0, {"type": "text", "text": prefix})
                return "messages[*].content[*].text"
            raise PromptInjectionError("prompt_injection_invalid_system")
        messages.insert(0, {"role": "system", "content": prefix})
        return "messages[0].content"
    if api_type == APIType.ANTHROPIC.value:
        system = payload.get("system")
        if system is None:
            payload["system"] = prefix
            return "system"
        if isinstance(system, str):
            payload["system"] = _prepend(system, prefix)
            return "system"
        if isinstance(system, list):
            system.insert(0, {"type": "text", "text": prefix})
            return "system[*].text"
        raise PromptInjectionError("prompt_injection_invalid_system")
    if api_type == APIType.OPENAI_RESPONSE.value:
        instructions = payload.get("instructions")
        if instructions is None:
            payload["instructions"] = prefix
            return "instructions"
        if isinstance(instructions, str):
            payload["instructions"] = _prepend(instructions, prefix)
            return "instructions"
        raise PromptInjectionError("prompt_injection_invalid_instructions")
    raise PromptInjectionError("prompt_injection_unsupported_format")


def _first_prompt_text(payload: dict[str, Any], api_type: str) -> str | None:
    if api_type == APIType.OPENAI_RESPONSE.value:
        value = payload.get("instructions")
        return value if isinstance(value, str) else None
    if api_type == APIType.ANTHROPIC.value:
        value = payload.get("system")
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            text = value[0].get("text")
            return text if isinstance(text, str) else None
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        value = message.get("content")
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            text = value[0].get("text")
            return text if isinstance(text, str) else None
    return None


@dataclass(frozen=True, slots=True)
class PromptIntegrity:
    api_type: str
    prefix: str

    def verify(self, payload: dict[str, Any]) -> None:
        current = _first_prompt_text(payload, self.api_type)
        if current is None or not current.startswith(self.prefix):
            raise PromptIntegrityError("prompt_extension_modified_by_pii")


def preview_prompt(*, api_type: str, caveman_enabled: bool, custom_enabled: bool, custom_text: str) -> dict[str, Any]:
    prefix, extensions = compose_extensions(
        caveman_enabled=caveman_enabled,
        custom_enabled=custom_enabled,
        custom_text=custom_text,
    )
    draft = _preview_payload(api_type)
    placement = inject_prompt(draft, api_type, prefix) if prefix else "none"
    return {
        "draft": True,
        "api_type": api_type,
        "order": [item["extension_id"] for item in extensions],
        "extensions": extensions,
        "combined_text": prefix,
        "combined_utf8_bytes": len(prefix.encode("utf-8")),
        "combined_sha256": prompt_hash(prefix),
        "placement": placement,
    }


def _preview_payload(api_type: str) -> dict[str, Any]:
    if api_type == APIType.OPENAI_CHAT.value:
        return {"messages": []}
    if api_type == APIType.ANTHROPIC.value:
        return {"messages": []}
    if api_type == APIType.OPENAI_RESPONSE.value:
        return {"input": []}
    raise PromptInjectionError("prompt_injection_unsupported_format")
