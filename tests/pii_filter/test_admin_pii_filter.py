import json

import httpx
import pytest
import pytest_asyncio

import config
import storage
from main import app
from tests.admin_auth_utils import login_admin

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"
    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await login_admin(c)
        yield c


async def test_pii_filter_test_masks_phone(client):
    resp = await client.post(
        "/admin/pii-filter/test",
        json={
            "text": "我的手机是13800138000",
            "settings": {
                "pii_filter_enabled": True,
                "pii_preset_phone": True,
                "pii_preset_id_card": False,
                "pii_preset_email": False,
                "pii_preset_bank_card": False,
                "pii_custom_rules": "[]",
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "我的手机是138****8000"
    assert data["action"] == "mask"
    assert any(t["entity"] == "CN_PHONE_NUMBER" for t in data["triggered"])


async def test_pii_filter_test_block_rule(client):
    resp = await client.post(
        "/admin/pii-filter/test",
        json={
            "text": "机密 alpha123",
            "settings": {
                "pii_filter_enabled": True,
                "pii_preset_phone": False,
                "pii_preset_id_card": False,
                "pii_preset_email": False,
                "pii_preset_bank_card": False,
                "pii_custom_rules": json.dumps(
                    [
                        {
                            "name": "secret",
                            "entity": "SECRET",
                            "pattern": r"alpha\d+",
                            "action": "block",
                        }
                    ]
                ),
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "block"
    assert "SECRET" in [t["entity"] for t in data["triggered"]]
