import json

import httpx
import pytest

import config
import storage
from main import app
from tests.admin_auth_utils import login_admin


@pytest.fixture
def channels_file(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"

    channels_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
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
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()
    import main
    monkeypatch.setattr(main, "_whitelist_cache", main._whitelist.WhitelistCache(str(data_dir / "whitelist.csv")))
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login_admin(client)
        response = await client.put("/admin/channels/ch_test", json={"weight": 0})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_update_channel_revalidates_priority(channels_file):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login_admin(client)
        response = await client.put("/admin/channels/ch_test", json={"priority": 0})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_update_channel_accepts_anthropic_header_policy_fields(channels_file):
    payload = {
        "api_type": "anthropic",
        "anthropic_version": "2024-10-22",
        "anthropic_version_policy": "channel_if_missing",
        "anthropic_beta": "prompt-caching-2024-07-31",
        "anthropic_beta_policy": "merge",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login_admin(client)
        response = await client.put("/admin/channels/ch_test", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["api_type"] == "anthropic"
    assert body["anthropic_version"] == "2024-10-22"
    assert body["anthropic_version_policy"] == "channel_if_missing"
    assert body["anthropic_beta"] == "prompt-caching-2024-07-31"
    assert body["anthropic_beta_policy"] == "merge"


@pytest.mark.anyio
async def test_create_model_group_uses_storage_helper(channels_file, monkeypatch):
    import routers.admin

    calls = []

    async def fake_add_model_group(group):
        calls.append(group)
        return group

    monkeypatch.setattr(routers.admin, "add_model_group", fake_add_model_group)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login_admin(client)
        response = await client.post(
            "/admin/model-groups",
            json={"name": "fallback", "models": ["gpt-4o"]},
        )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].name == "fallback"
