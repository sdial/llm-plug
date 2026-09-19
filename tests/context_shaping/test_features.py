import copy

import pytest

from context_shaping import shape_request
from context_shaping.prompts import PromptIntegrityError
from models.api_types import APIType


def _shape(payload, api_type=APIType.OPENAI_CHAT, settings=None):
    return shape_request(
        payload,
        resolved_profile={"api_type": api_type},
        settings={"context_shaping_strip_ansi": False, **(settings or {})},
    )


@pytest.mark.parametrize(
    ("setting", "source", "expected"),
    [
        ("context_shaping_trim_trailing_whitespace", "a  \nb", "a\nb"),
        ("context_shaping_collapse_blank_lines", "a\n\n\n\nb", "a\n\nb"),
        ("context_shaping_dedupe_consecutive_lines", "a\na\nb", "a\nb"),
    ],
)
def test_text_compaction_features_are_independent_and_idempotent(setting, source, expected):
    payload = {"messages": [{"role": "tool", "tool_call_id": "x", "content": source}]}
    original = copy.deepcopy(payload)
    first = _shape(payload, settings={setting: True})
    second = _shape(first.payload, settings={setting: True})

    assert first.payload["messages"][0]["content"] == expected
    assert second.payload == first.payload
    assert payload == original
    assert first.receipt["actions"][0]["feature"] == setting.removeprefix("context_shaping_")


def test_message_compaction_features_are_independent():
    payload = {
        "messages": [
            {"role": "user", "content": "same"},
            {"role": "user", "content": "same"},
            {"role": "tool", "tool_call_id": "missing", "content": "orphan"},
        ]
    }
    result = _shape(
        payload,
        settings={
            "context_shaping_dedupe_adjacent_user_messages": True,
            "context_shaping_strip_unreferenced_tool_results": True,
        },
    )
    assert result.payload["messages"] == [{"role": "user", "content": "same"}]
    assert {row["feature"] for row in result.receipt["actions"]} == {
        "dedupe_adjacent_user_messages",
        "strip_unreferenced_tool_results",
    }


def test_removed_claude_code_settings_leave_prompt_unchanged():
    payload = {"system": "Date: 2026-09-11\nOS Version: Windows"}

    result = _shape(
        payload,
        APIType.ANTHROPIC,
        settings={
            "context_shaping_claude_code_enabled": True,
            "context_shaping_claude_code_core_metadata": True,
        },
    )

    assert result.payload is payload
    assert result.receipt is None


@pytest.mark.parametrize(
    ("api_type", "payload", "read_text"),
    [
        (APIType.OPENAI_CHAT, {"messages": [{"role": "system", "content": "client"}]}, lambda p: p["messages"][0]["content"]),
        (APIType.ANTHROPIC, {"system": "client", "messages": []}, lambda p: p["system"]),
        (APIType.OPENAI_RESPONSE, {"instructions": "client", "input": []}, lambda p: p["instructions"]),
    ],
)
def test_prompt_extensions_land_in_each_actual_format(api_type, payload, read_text):
    result = _shape(
        payload,
        api_type,
        settings={
            "context_shaping_caveman_enabled": True,
            "context_shaping_custom_prompt_enabled": True,
            "context_shaping_custom_prompt_text": "custom",
            "context_shaping_custom_prompt_version": 3,
        },
    )
    text = read_text(result.payload)
    assert text.index("Enable Caveman") < text.index("custom") < text.index("client")
    assert "sha256" not in text and "version" not in text
    assert result.receipt["prompt_extensions"][1]["version"] == 3
    result.prompt_integrity.verify(result.payload)

    tampered = copy.deepcopy(result.payload)
    if api_type == APIType.OPENAI_CHAT:
        tampered["messages"][0]["content"] = tampered["messages"][0]["content"].replace("custom", "<PII>")
    elif api_type == APIType.ANTHROPIC:
        tampered["system"] = tampered["system"].replace("custom", "<PII>")
    else:
        tampered["instructions"] = tampered["instructions"].replace("custom", "<PII>")
    with pytest.raises(PromptIntegrityError):
        result.prompt_integrity.verify(tampered)


def test_blank_or_oversized_enabled_custom_prompt_is_rejected():
    with pytest.raises(ValueError, match="blank"):
        _shape({}, settings={"context_shaping_custom_prompt_enabled": True, "context_shaping_custom_prompt_text": " "})
    with pytest.raises(ValueError, match="32768"):
        _shape({}, settings={"context_shaping_custom_prompt_enabled": True, "context_shaping_custom_prompt_text": "你" * 11000})
