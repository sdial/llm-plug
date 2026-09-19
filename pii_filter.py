"""PII 过滤入口：规则构建与缓存、调度与 block 异常。"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from pii_engine import PiiEngine, PiiRule
from pii_errors import SensitiveBlockError
from pii_operators import build_operators_map
from pii_recognizers import (
    build_bank_card_rule,
    build_email_rule,
    build_id_card_rule,
    build_phone_rule,
)
from pii_walker import walk_text_leaves

_engine = PiiEngine()
_cache_key: tuple[bool, bool, bool, bool, str] | None = None
_cached_rules: tuple[PiiRule, ...] = ()


def _build_rules(settings: dict) -> tuple[PiiRule, ...]:
    """构建识别规则并以设置指纹做单条目缓存（避免每请求重建）。"""
    global _cache_key, _cached_rules
    key = (
        bool(settings.get("pii_preset_phone", True)),
        bool(settings.get("pii_preset_id_card", True)),
        bool(settings.get("pii_preset_bank_card", True)),
        bool(settings.get("pii_preset_email", True)),
        str(settings.get("pii_custom_rules", "") or ""),
    )
    if _cache_key == key:
        return _cached_rules

    rules: list[PiiRule] = []
    if key[0]:
        rules.append(build_phone_rule())
    if key[1]:
        rules.append(build_id_card_rule())
    if key[2]:
        rules.append(build_bank_card_rule())
    if key[3]:
        rules.append(build_email_rule())

    for rule_def in _load_custom_rules(settings):
        try:
            rules.append(
                PiiRule(
                    name=str(rule_def["name"]),
                    entity_type=str(rule_def["entity"]),
                    pattern=re.compile(str(rule_def["pattern"])),
                )
            )
        except (KeyError, TypeError, ValueError, re.error) as exc:
            logger.warning(f"跳过无法编译的自定义 PII 规则 {rule_def.get('name', '?')}: {exc}")

    _cache_key = key
    _cached_rules = tuple(rules)
    return _cached_rules


def _load_custom_rules(settings: dict) -> list[dict]:
    raw = settings.get("pii_custom_rules", "[]")
    if not raw:
        return []
    try:
        rules = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("pii_custom_rules 不是合法 JSON，忽略自定义规则")
        return []
    if not isinstance(rules, list):
        logger.warning("pii_custom_rules 不是数组，忽略自定义规则")
        return []
    return [r for r in rules if isinstance(r, dict)]


def _exempt_channel(settings: dict, channel_id: str | None) -> bool:
    if not channel_id:
        return False
    raw = settings.get("pii_exempt_channels", "[]")
    try:
        exempt = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return False
    return isinstance(exempt, list) and channel_id in exempt


_PRESET_ENTITY_TO_SETTING = {
    "CN_PHONE_NUMBER": "pii_preset_phone_action",
    "CN_ID_CARD": "pii_preset_id_card_action",
    "CN_BANK_CARD": "pii_preset_bank_card_action",
    "EMAIL_ADDRESS": "pii_preset_email_action",
}


def _action_for_entity(entity_type: str, custom_rules: list[dict], settings: dict) -> str:
    for rule in custom_rules:
        if rule.get("entity") == entity_type:
            action = rule.get("action", "mask")
            # encrypt 已随 presidio 移除，存量配置平滑回退为 mask
            return "mask" if action == "encrypt" else str(action)
    preset_key = _PRESET_ENTITY_TO_SETTING.get(entity_type)
    if preset_key:
        action = str(settings.get(preset_key, "mask"))
        return action if action in ("mask", "replace", "block") else "mask"
    return "replace"


def apply_pii_filter(
    upstream_data: dict,
    target_api_type: str,
    settings: dict,
    channel_id: str | None = None,
    *,
    return_info: bool = False,
) -> dict | tuple[dict, dict[str, Any]]:
    """在 upstream_data 上完成敏感信息脱敏/拦截，原地修改并返回。

    命中 block 规则时抛出 SensitiveBlockError。
    """
    info: dict[str, Any] = {
        "enabled": False,
        "action": None,
        "rules_triggered": [],
        "target_api_type": target_api_type,
    }

    if not settings.get("pii_filter_enabled", False) or _exempt_channel(settings, channel_id):
        return (upstream_data, info) if return_info else upstream_data

    info["enabled"] = True
    rules = _build_rules(settings)
    custom_rules = _load_custom_rules(settings)
    operators_map = build_operators_map()

    triggered_rules: set[str] = set()
    any_block = False

    for container, key, text in walk_text_leaves(upstream_data, target_api_type):
        if not isinstance(text, str):
            continue

        results = _engine.analyze(text, rules)
        if not results:
            continue

        block_results = [r for r in results if _action_for_entity(r.entity_type, custom_rules, settings) == "block"]
        if block_results:
            any_block = True
            triggered_rules.update(r.entity_type for r in block_results)
            continue

        new_text = text
        current_action = None
        # 识别偏移量均基于原文。按位置从右向左逐段应用，左侧索引不会因右侧脱敏
        # 字符数变化而漂移；不能按实体类型分组后在已修改文本上复用原始偏移。
        for result in sorted(results, key=lambda item: (item.start, item.end), reverse=True):
            action = _action_for_entity(result.entity_type, custom_rules, settings)
            triggered_rules.add(result.entity_type)
            op = operators_map["DEFAULT"] if action == "replace" else (operators_map.get(result.entity_type) or operators_map["DEFAULT"])
            new_text = op.apply(new_text, [result])
            current_action = action

        if new_text != text:
            container[key] = new_text
            info["action"] = current_action

    info["rules_triggered"] = sorted(triggered_rules)

    if any_block:
        raise SensitiveBlockError(
            "Sensitive data blocked by PII filter",
            triggered=sorted(triggered_rules),
        )

    return (upstream_data, info) if return_info else upstream_data
