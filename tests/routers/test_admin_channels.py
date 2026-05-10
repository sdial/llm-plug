import json

import httpx
import pytest

import config
import storage
from main import app


@pytest.fixture
def channels_file(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"

    channels_path.write_text(json.dumps({
        "channels": [{
            "id": "ch_test",
            "name": "Test",
            "api_type": "openai-chat-completions",
            "base_url": "https://api.example.com",
            "api_key": "sk-test",
            "models": ["gpt-4o"],
            "enabled": True,
            "weight": 1,
            "priority": 1,
            "socks5_proxy": None,
            "created_at": "2026-05-10T00:00:00+00:00",
        }]
    }), encoding="utf-8")
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None
    yield channels_path
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None


@pytest.mark.anyio
async def test_update_channel_revalidates_weight(channels_file):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/admin/channels/ch_test", json={"weight": 0})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_update_channel_revalidates_priority(channels_file):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/admin/channels/ch_test", json={"priority": 0})

    assert response.status_code == 422
