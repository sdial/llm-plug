"""Context Shaping 请求级深模块。"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from logging_config import logger
from models.api_types import APIType

from .features import (
    collapse_blank_lines,
    dedupe_adjacent_users,
    dedupe_consecutive_lines,
    strip_unreferenced_tool_results,
    transform_tool_text,
    trim_trailing_whitespace,
)
from .prompts import CAVEMAN_PROMPT, PromptIntegrity, compose_extensions, inject_prompt

_ANSI_RE = re.compile(
    r"(?:\x1b\[[\x30-\x3f]*[ -/]*[@-~]|\x9b[\x30-\x3f]*[ -/]*[@-~]"
    r"|\x1b[PX^_][^\x1b\x07]*(?:\x07|\x1b\\)|\x1b\][^\x1b\x07]*(?:\x07|\x1b\\)"
    r"|\x1b[\x30-\x4f\x51-\x5a\x60-\x7e])"
)
_MAX_ACTION_ROWS = 64


@dataclass(frozen=True, slots=True)
class ShapingResult:
    payload: dict[str, Any]
    receipt: dict[str, Any] | None
    prompt_integrity: PromptIntegrity | None = None


def _api_type_value(resolved_profile: Any) -> str:
    api_type = resolved_profile.get("api_type") if isinstance(resolved_profile, Mapping) else resolved_profile.api_type
    return api_type.value if isinstance(api_type, APIType) else str(api_type)


def _enabled(settings: Mapping[str, Any]) -> list[str]:
    feature_keys = (
        ("strip_ansi", "context_shaping_strip_ansi", True),
        ("trim_trailing_whitespace", "context_shaping_trim_trailing_whitespace", False),
        ("collapse_blank_lines", "context_shaping_collapse_blank_lines", False),
        ("dedupe_consecutive_lines", "context_shaping_dedupe_consecutive_lines", False),
        ("strip_unreferenced_tool_results", "context_shaping_strip_unreferenced_tool_results", False),
        ("dedupe_adjacent_user_messages", "context_shaping_dedupe_adjacent_user_messages", False),
        ("caveman_prompt_extension", "context_shaping_caveman_enabled", False),
        ("custom_prompt_extension", "context_shaping_custom_prompt_enabled", False),
    )
    return [name for name, key, default in feature_keys if bool(settings.get(key, default))]


def _aggregate_actions(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in actions:
        key = (item["feature"], item["action"], item["field_path"])
        row = grouped.setdefault(
            key,
            {
                "feature": item["feature"],
                "action": item["action"],
                "field_path": item["field_path"],
                "hit_count": 0,
                "before_chars": 0,
                "after_chars": 0,
            },
        )
        for field in ("hit_count", "before_chars", "after_chars"):
            row[field] += int(item.get(field) or 0)
    rows = list(grouped.values())
    overflow = rows[_MAX_ACTION_ROWS:]
    return rows[:_MAX_ACTION_ROWS], len(overflow), sum(row["hit_count"] for row in overflow)


def _transaction(
    current: dict[str, Any], feature: str, operation: Callable[[dict[str, Any]], list[dict[str, Any]]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate = copy.deepcopy(current)
    try:
        actions = operation(candidate)
    except Exception as exc:
        logger.warning(f"Context Shaping feature={feature} error=feature_failed type={type(exc).__name__}")
        return current, []
    return candidate, actions


def shape_request(
    payload: dict[str, Any],
    *,
    resolved_profile: Any,
    settings: Mapping[str, Any],
) -> ShapingResult:
    """按实际上游格式事务式整形，绝不原地修改调用方 payload。"""
    enabled = _enabled(settings)
    if not enabled:
        return ShapingResult(payload=payload, receipt=None)

    api_type = _api_type_value(resolved_profile)
    current = payload
    all_actions: list[dict[str, Any]] = []

    text_features: tuple[tuple[str, str, Callable[[str], str]], ...] = (
        ("strip_ansi", "remove_ansi", lambda text: _ANSI_RE.sub("", text)),
        ("trim_trailing_whitespace", "trim_trailing_whitespace", trim_trailing_whitespace),
        ("collapse_blank_lines", "collapse_blank_lines", collapse_blank_lines),
        ("dedupe_consecutive_lines", "dedupe_consecutive_lines", dedupe_consecutive_lines),
    )
    for feature, action_name, transform in text_features:
        if feature not in enabled:
            continue
        current, actions = _transaction(
            current,
            feature,
            lambda candidate, feature=feature, action_name=action_name, transform=transform: transform_tool_text(
                candidate, api_type, feature, action_name, transform
            ),
        )
        all_actions.extend(actions)

    if "strip_unreferenced_tool_results" in enabled:
        current, actions = _transaction(
            current,
            "strip_unreferenced_tool_results",
            lambda candidate: strip_unreferenced_tool_results(candidate, api_type),
        )
        all_actions.extend(actions)
    if "dedupe_adjacent_user_messages" in enabled:
        current, actions = _transaction(
            current,
            "dedupe_adjacent_user_messages",
            lambda candidate: dedupe_adjacent_users(candidate, api_type),
        )
        all_actions.extend(actions)
    prompt_integrity = None
    prompt_extensions: list[dict[str, Any]] = []
    if {"caveman_prompt_extension", "custom_prompt_extension"} & set(enabled):
        prefix, prompt_extensions = compose_extensions(
            caveman_enabled="caveman_prompt_extension" in enabled,
            custom_enabled="custom_prompt_extension" in enabled,
            custom_text=str(settings.get("context_shaping_custom_prompt_text", "")),
        )
        for item in prompt_extensions:
            if item["extension_id"] == "custom":
                item["version"] = int(settings.get("context_shaping_custom_prompt_version", 0))
        candidate = copy.deepcopy(current)
        placement = inject_prompt(candidate, api_type, prefix)
        current = candidate
        prompt_integrity = PromptIntegrity(api_type=api_type, prefix=prefix)
        extension_chars = {"caveman": len(CAVEMAN_PROMPT), "custom": len(str(settings.get("context_shaping_custom_prompt_text", "")))}
        for item in prompt_extensions:
            all_actions.append(
                {
                    "feature": f"{item['extension_id']}_prompt_extension",
                    "action": "prepend_prompt_extension",
                    "field_path": placement,
                    "hit_count": 1,
                    "before_chars": 0,
                    "after_chars": extension_chars[item["extension_id"]],
                }
            )

    rows, overflow_rows, overflow_hits = _aggregate_actions(all_actions)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "upstream_api_format": api_type,
        "enabled_features": enabled,
        "actions": rows,
        "overflow_action_rows": overflow_rows,
        "overflow_hit_count": overflow_hits,
    }
    if prompt_extensions:
        receipt["prompt_extensions"] = prompt_extensions
    return ShapingResult(payload=current, receipt=receipt, prompt_integrity=prompt_integrity)


__all__ = ["ShapingResult", "shape_request"]
