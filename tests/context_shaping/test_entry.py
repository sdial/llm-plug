from copy import deepcopy

import pytest

from context_shaping import shape_request
from models.api_types import APIType


class _Profile:
    def __init__(self, api_type: APIType):
        self.api_type = api_type


@pytest.mark.parametrize(
    ("api_type", "payload", "expected"),
    [
        (
            APIType.OPENAI_CHAT,
            {"messages": [{"role": "tool", "content": "a\x1b[31mb\x1b[0m"}]},
            {"messages": [{"role": "tool", "content": "ab"}]},
        ),
        (
            APIType.ANTHROPIC,
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "a\x1b[31mb"}]}]},
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ab"}]}]},
        ),
        (
            APIType.OPENAI_RESPONSE,
            {"input": [{"type": "function_call_output", "call_id": "c", "output": "a\x1b[31mb"}]},
            {"input": [{"type": "function_call_output", "call_id": "c", "output": "ab"}]},
        ),
    ],
)
def test_shapes_by_actual_upstream_format_without_mutating_input(api_type, payload, expected):
    original = deepcopy(payload)

    result = shape_request(
        payload,
        resolved_profile=_Profile(api_type),
        settings={"context_shaping_strip_ansi": True},
    )

    assert payload == original
    assert result.payload == expected
    assert result.payload is not payload
    assert result.receipt["upstream_api_format"] == api_type.value
    assert result.receipt["actions"][0]["feature"] == "strip_ansi"
    assert result.receipt["actions"][0]["action"] == "remove_ansi"


def test_default_is_enabled_and_repeated_execution_is_idempotent():
    payload = {"messages": [{"role": "tool", "content": "a\x1b[31mb"}]}
    profile = _Profile(APIType.OPENAI_CHAT)
    first = shape_request(payload, resolved_profile=profile, settings={})
    second = shape_request(first.payload, resolved_profile=profile, settings={})

    assert first.payload["messages"][0]["content"] == "ab"
    assert second.payload == first.payload
    assert second.receipt["actions"] == []


def test_disabled_feature_returns_original_reference_and_no_receipt():
    payload = {"messages": [{"role": "tool", "content": "a\x1b[31mb"}]}

    result = shape_request(
        payload,
        resolved_profile=_Profile(APIType.OPENAI_CHAT),
        settings={"context_shaping_strip_ansi": False, "ctx_optimize_enabled": True, "ctx_optimize_strip_ansi": True},
    )

    assert result.payload is payload
    assert result.receipt is None


def test_receipt_aggregates_array_indices_into_stable_path():
    payload = {
        "messages": [
            {"role": "tool", "content": "a\x1b[31mb"},
            {"role": "tool", "content": "c\x1b[32md"},
        ]
    }

    result = shape_request(payload, resolved_profile=_Profile(APIType.OPENAI_CHAT), settings={})

    assert result.receipt["actions"] == [
        {
            "feature": "strip_ansi",
            "action": "remove_ansi",
            "field_path": "messages[*].content",
            "hit_count": 2,
            "before_chars": 14,
            "after_chars": 4,
        }
    ]
