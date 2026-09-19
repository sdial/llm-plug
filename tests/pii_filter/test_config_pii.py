import pytest

import config


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    monkeypatch.setattr(config, "_settings", {})


def test_pii_config_schema_has_all_keys():
    keys = [
        "pii_filter_enabled",
        "pii_preset_phone",
        "pii_preset_id_card",
        "pii_preset_email",
        "pii_preset_bank_card",
        "pii_custom_rules",
        "pii_exempt_channels",
    ]
    for key in keys:
        assert key in config._CONFIG_SCHEMA, key


PRESET_ACTION_KEYS = [
    "pii_preset_phone_action",
    "pii_preset_id_card_action",
    "pii_preset_email_action",
    "pii_preset_bank_card_action",
]


def test_pii_preset_action_schema():
    for key in PRESET_ACTION_KEYS:
        assert key in config._CONFIG_SCHEMA, key
        assert config._CONFIG_SCHEMA[key]["type"] == "str"
        assert config._CONFIG_SCHEMA[key]["default"] == "mask"


def test_pii_preset_action_constraints():
    for key in PRESET_ACTION_KEYS:
        assert config._CONFIG_CONSTRAINTS[key]["choices"] == ("mask", "replace", "block")


def test_preset_email_action_default_mask():
    assert config._CONFIG_SCHEMA["pii_preset_email_action"]["default"] == "mask"


@pytest.mark.anyio
async def test_update_settings_rejects_invalid_preset_action(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(config, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "_save_settings_to_disk", AsyncMock())
    monkeypatch.setattr(config, "_apply_lb_settings", AsyncMock())
    with pytest.raises(ValueError, match="pii_preset_phone_action"):
        await config.update_settings({"pii_preset_phone_action": "encrypt"})


def test_encryption_key_removed_from_schema():
    assert "pii_encryption_key" not in config._CONFIG_SCHEMA


def test_pii_config_defaults():
    assert config._CONFIG_SCHEMA["pii_filter_enabled"]["default"] is False
    assert config._CONFIG_SCHEMA["pii_preset_phone"]["default"] is True
    assert config._CONFIG_SCHEMA["pii_custom_rules"]["default"] == "[]"
    assert config._CONFIG_SCHEMA["pii_exempt_channels"]["default"] == "[]"


@pytest.mark.anyio
async def test_update_settings_rejects_invalid_custom_rule_json(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(config, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "_save_settings_to_disk", AsyncMock())
    monkeypatch.setattr(config, "_apply_lb_settings", AsyncMock())
    with pytest.raises(ValueError, match="pii_custom_rules"):
        await config.update_settings({"pii_custom_rules": "not-json"})
