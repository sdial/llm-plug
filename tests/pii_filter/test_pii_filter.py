import copy
import json

import pytest

from pii_filter import apply_pii_filter
from proxy.errors import SensitiveBlockError

BASE_SETTINGS = {
    "pii_filter_enabled": True,
    "pii_preset_phone": True,
    "pii_preset_id_card": True,
    "pii_preset_email": True,
    "pii_preset_bank_card": True,
    "pii_custom_rules": "[]",
    "pii_exempt_channels": "[]",
}


def test_disabled_returns_original():
    data = {"messages": [{"role": "user", "content": "13800138000"}]}
    settings = {**BASE_SETTINGS, "pii_filter_enabled": False}
    result = apply_pii_filter(data, "openai-chat", settings)
    assert result is data


def test_exempt_channel_returns_original():
    data = {"messages": [{"role": "user", "content": "13800138000"}]}
    settings = {**BASE_SETTINGS, "pii_exempt_channels": '["ch_1"]'}
    result = apply_pii_filter(data, "openai-chat", settings, channel_id="ch_1")
    assert result is data


def test_phone_mask():
    data = {"messages": [{"role": "user", "content": "我的手机是13800138000"}]}
    result = apply_pii_filter(data, "openai-chat", BASE_SETTINGS)
    assert result["messages"][0]["content"] == "我的手机是138****8000"


def test_email_mask():
    data = {"messages": [{"role": "user", "content": "联系 foo@example.com"}]}
    result = apply_pii_filter(data, "openai-chat", BASE_SETTINGS)
    assert "****" in result["messages"][0]["content"]


def test_block_custom_rule():
    data = {"messages": [{"role": "user", "content": "机密词 alpha123"}]}
    settings = {
        **BASE_SETTINGS,
        "pii_custom_rules": json.dumps(
            [
                {
                    "name": "secret_word",
                    "entity": "SECRET_WORD",
                    "pattern": r"alpha\d+",
                    "action": "block",
                    "context": [],
                }
            ]
        ),
    }
    with pytest.raises(SensitiveBlockError) as exc_info:
        apply_pii_filter(data, "openai-chat", settings)
    assert "SECRET_WORD" in exc_info.value.triggered


def test_replace_custom_rule():
    data = {"messages": [{"role": "user", "content": "机密词 alpha123"}]}
    settings = {
        **BASE_SETTINGS,
        "pii_custom_rules": json.dumps(
            [
                {
                    "name": "secret_word",
                    "entity": "SECRET_WORD",
                    "pattern": r"alpha\d+",
                    "action": "replace",
                    "context": [],
                }
            ]
        ),
    }
    result = apply_pii_filter(data, "openai-chat", settings)
    assert result["messages"][0]["content"] == "机密词 [REDACTED]"


def test_field_whitelist_skip_tool_args():
    data = {
        "messages": [{"role": "user", "content": "ok"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "arguments": '{"x": "13800138000"}',
                },
            }
        ],
    }
    original = copy.deepcopy(data)
    result = apply_pii_filter(data, "openai-chat", BASE_SETTINGS)
    assert result["tools"][0]["function"]["arguments"] == original["tools"][0]["function"]["arguments"]


def test_returns_sensitivity_info():
    data = {"messages": [{"role": "user", "content": "我的手机是13800138000"}]}
    _, info = apply_pii_filter(data, "openai-chat", BASE_SETTINGS, return_info=True)
    assert info["enabled"] is True
    assert info["action"] == "mask"
    assert "CN_PHONE_NUMBER" in info["rules_triggered"]


def test_encrypt_action_falls_back_to_mask():
    data = {"messages": [{"role": "user", "content": "机密词 alpha123"}]}
    settings = {
        **BASE_SETTINGS,
        "pii_custom_rules": json.dumps(
            [
                {
                    "name": "secret_word",
                    "entity": "SECRET_WORD",
                    "pattern": r"alpha\d+",
                    "action": "encrypt",
                    "context": [],
                }
            ]
        ),
    }
    result = apply_pii_filter(data, "openai-chat", settings)
    assert result["messages"][0]["content"] == "机密词 [REDACTED]"


def test_preset_phone_action_replace():
    data = {"messages": [{"role": "user", "content": "我的手机是13800138000"}]}
    settings = {**BASE_SETTINGS, "pii_preset_phone_action": "replace"}
    result = apply_pii_filter(data, "openai-chat", settings)
    assert result["messages"][0]["content"] == "我的手机是[REDACTED]"


def test_preset_phone_action_block():
    data = {"messages": [{"role": "user", "content": "我的手机是13800138000"}]}
    settings = {**BASE_SETTINGS, "pii_preset_phone_action": "block"}
    with pytest.raises(SensitiveBlockError) as exc_info:
        apply_pii_filter(data, "openai-chat", settings)
    assert "CN_PHONE_NUMBER" in exc_info.value.triggered


def test_preset_action_defaults_to_mask_when_absent():
    data = {"messages": [{"role": "user", "content": "我的手机是13800138000"}]}
    result = apply_pii_filter(data, "openai-chat", BASE_SETTINGS)
    assert result["messages"][0]["content"] == "我的手机是138****8000"
