import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import config
import storage
from main import app
from tests.admin_auth_utils import login_admin


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


@pytest.mark.asyncio
async def test_settings_fragment_has_pii_section(client):
    resp = await client.get("/admin/ui/settings")
    assert resp.status_code == 200
    html = resp.text
    assert 'data-section="pii-filter"' in html
    assert 'id="settings_pii-filter"' in html
    assert 'id="set_pii_filter_enabled"' in html


def test_i18n_keys_present():
    for path in ("static/js/i18n-zh.js", "static/js/i18n-en.js"):
        js = Path(path).read_text(encoding="utf-8")
        assert "navPiiFilter" in js
        assert "piiTitle" in js


def test_settings_js_handles_pii():
    js = Path("static/js/settings.js").read_text(encoding="utf-8")
    assert "pii_filter_enabled" in js
    assert "pii_custom_rules" in js
    assert "_runPiiTest" in js


def test_settings_fragment_has_pii_help_and_preset_actions():
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")
    assert 'id="pii_help_btn"' in html
    assert 'id="pii_help_panel"' in html
    for key in ("pii_preset_phone_action", "pii_preset_id_card_action", "pii_preset_email_action", "pii_preset_bank_card_action"):
        assert f'id="set_{key}"' in html
    assert "pii-rule-context" not in html


def test_i18n_keys_for_pii_help():
    for path in ("static/js/i18n-zh.js", "static/js/i18n-en.js"):
        js = Path(path).read_text(encoding="utf-8")
        assert "piiHelpMask" in js
        assert "piiHelpReplace" in js
        assert "piiHelpBlock" in js
        assert "piiActionMask" in js
        assert "piiActionReplace" in js
        assert "piiActionBlock" in js


def test_settings_js_handles_preset_actions():
    js = Path("static/js/settings.js").read_text(encoding="utf-8")
    for key in ("pii_preset_phone_action", "pii_preset_id_card_action", "pii_preset_email_action", "pii_preset_bank_card_action"):
        assert key in js
    assert "pii-rule-context" not in js
    assert "context:" not in js


def test_settings_js_pii_test_includes_actions():
    js = Path("static/js/settings.js").read_text(encoding="utf-8")
    assert "pii_preset_phone_action" in js
    assert "pii_preset_bank_card_action" in js
