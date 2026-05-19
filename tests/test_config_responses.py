# tests/test_config_responses.py
from unittest.mock import AsyncMock

import pytest

from config import get_setting, _CONFIG_SCHEMA


def test_response_state_config_schema():
    assert "response_state_max_entries" in _CONFIG_SCHEMA
    assert "response_state_ttl_minutes" in _CONFIG_SCHEMA
    assert "response_state_cleanup_interval_minutes" in _CONFIG_SCHEMA


def test_response_state_defaults():
    assert get_setting("response_state_max_entries") == 1000
    assert get_setting("response_state_ttl_minutes") == 60
    assert get_setting("response_state_cleanup_interval_minutes") == 30


def test_response_state_store_is_shared_across_modules():
    import main
    import proxy_core
    from routers import proxy_response

    assert main._responses_store is proxy_core._responses_store
    assert proxy_response._store is proxy_core._responses_store


def test_response_state_store_can_reload_runtime_settings(monkeypatch):
    import response_state

    monkeypatch.setattr(response_state, "get_setting", lambda key: {
        "response_state_max_entries": 17,
        "response_state_ttl_minutes": 3,
    }.get(key))

    response_state.reload_responses_store()

    store = response_state.get_responses_store()
    assert store.max_entries == 17
    assert store.ttl_seconds == 180


@pytest.mark.anyio
async def test_response_state_settings_update_reloads_store(monkeypatch):
    import config

    called = False

    def fake_reload():
        nonlocal called
        called = True

    monkeypatch.setattr(config, "_save_settings_to_disk", AsyncMock())
    monkeypatch.setattr(config, "_settings", {})
    monkeypatch.setattr("response_state.reload_responses_store", fake_reload)

    await config.update_settings({"response_state_max_entries": 23})

    assert called
