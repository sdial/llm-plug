import json

import httpx
import pytest
import pytest_asyncio

import config
import storage
from main import app
from tests.admin_auth_utils import login_admin


@pytest.fixture
def admin_ui_data_dir(tmp_path, monkeypatch):
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

    import main
    monkeypatch.setattr(main, "_whitelist_cache", main._whitelist.WhitelistCache(str(data_dir / "whitelist.csv")))


@pytest_asyncio.fixture
async def client(admin_ui_data_dir):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await login_admin(c)
        yield c


@pytest.mark.anyio
async def test_admin_index_is_a_shell(client):
    resp = await client.get("/")

    assert resp.status_code == 200
    html = resp.text
    assert "/static/js/htmx.min.js" in html
    assert 'hx-get="/admin/ui/channels"' in html
    assert 'id="admin-content"' in html
    assert 'id="channelsTab"' not in html


@pytest.mark.anyio
async def test_admin_ui_fragments_exist(client):
    resp = await client.get("/admin/ui/channels")

    assert resp.status_code == 200
    assert "渠道列表" in resp.text
    assert "channelList" in resp.text
