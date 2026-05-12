"""Tests for startup lifespan behavior (cache pre-warming and diagnostic log)."""

import json

import pytest
from unittest.mock import patch

import config
import storage


@pytest.fixture(autouse=True)
def setup_data(tmp_path, monkeypatch):
    """Set up test data directory with channels and API keys."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    channels_data = {
        "channels": [
            {
                "id": "ch_1",
                "name": "Test",
                "api_type": "openai-chat-completions",
                "base_url": "https://api.example.com",
                "api_key": "key",
                "models": ["gpt-4o", "gpt-4"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
            {
                "id": "ch_2",
                "name": "Test2",
                "api_type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "key",
                "models": ["claude-sonnet-4-20250514"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
        ]
    }
    api_keys_data = {
        "api_keys": [{"id": "key_1", "name": "test-key", "key": "sk-test"}]
    }
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)
    with open(api_keys_file, "w") as f:
        json.dump(api_keys_data, f)

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None

    yield

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None


class TestLifespanPreWarming:
    def test_lifespan_pre_warms_cache(self):
        """lifespan should call load_data() and load_api_keys() before yielding."""
        import asyncio
        from main import app

        with (
            patch("main.load_data") as mock_load_data,
            patch("main.load_api_keys") as mock_load_api_keys,
            patch("main.close_all_clients") as mock_close,
        ):
            mock_load_data.return_value = {"channels": []}
            mock_load_api_keys.return_value = {"api_keys": []}

            async def run_lifespan():
                async with app.router.lifespan_context(app):
                    pass

            asyncio.run(run_lifespan())

            mock_close.assert_called_once()

            mock_load_data.assert_called_once()
            mock_load_api_keys.assert_called_once()

    def test_lifespan_logs_startup_info(self):
        """lifespan should print a startup summary with channel/model/key counts."""
        import asyncio
        from loguru import logger
        from main import app

        # Reset caches so load_data/load_api_keys actually run
        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None

        # Capture loguru output
        messages = []
        handler_id = logger.add(messages.append)

        async def run_lifespan():
            async with app.router.lifespan_context(app):
                pass

        try:
            with patch("main.close_all_clients"):
                asyncio.run(run_lifespan())

            # Check that the startup log message was emitted
            full_message = "".join(str(m) for m in messages)
            assert "个渠道" in full_message
            assert "个模型" in full_message
        finally:
            logger.remove(handler_id)
