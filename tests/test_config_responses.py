# tests/test_config_responses.py
from config import get_setting, _CONFIG_SCHEMA


def test_response_state_config_schema():
    assert "response_state_max_entries" in _CONFIG_SCHEMA
    assert "response_state_ttl_minutes" in _CONFIG_SCHEMA
    assert "response_state_cleanup_interval_minutes" in _CONFIG_SCHEMA


def test_response_state_defaults():
    assert get_setting("response_state_max_entries") == 1000
    assert get_setting("response_state_ttl_minutes") == 60
    assert get_setting("response_state_cleanup_interval_minutes") == 30