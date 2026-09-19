import json

import httpx
import pytest
import pytest_asyncio

import config
import storage
from main import app
from tests.admin_auth_utils import login_admin


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "channels.json").write_text('{"channels": []}', encoding="utf-8")
    (data_dir / "api_keys.json").write_text('{"api_keys": []}', encoding="utf-8")
    settings_path = data_dir / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(data_dir / "channels.json"))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(data_dir / "api_keys.json"))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()
    storage._cache = None
    storage._keys_cache = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
        await login_admin(http_client)
        yield http_client


@pytest.mark.asyncio
async def test_fragment_settings_and_stats_endpoints(client):
    fragment = await client.get("/admin/ui/context-shaping")
    assert fragment.status_code == 200
    assert 'id="contextShapingTab"' in fragment.text
    assert 'id="shapingCustomText"' in fragment.text
    assert "Claude Code Cache Stabilization" not in fragment.text
    assert 'data-shaping-subtab="settings"' in fragment.text
    assert 'data-shaping-subtab="stats"' in fragment.text

    settings = await client.get("/admin/settings")
    assert settings.json()["context_shaping_strip_ansi"] is True
    assert settings.json()["context_shaping_caveman_enabled"] is False
    assert "context_shaping_claude_code_enabled" not in settings.json()
    assert "context_shaping_claude_code_core_metadata" not in settings.json()

    stats = await client.get("/admin/stats/context-shaping?days=7")
    assert stats.status_code == 200
    assert stats.json()["overall"]["action_count"] == 0


@pytest.mark.asyncio
async def test_custom_prompt_version_only_changes_with_body(client):
    first = await client.put("/admin/settings", json={"context_shaping_custom_prompt_text": "alpha"})
    assert first.status_code == 200
    assert "context_shaping_custom_prompt_version" in first.json()["updated"]
    version = (await client.get("/admin/settings")).json()["context_shaping_custom_prompt_version"]

    toggled = await client.put("/admin/settings", json={"context_shaping_custom_prompt_enabled": True})
    assert toggled.status_code == 200
    assert (await client.get("/admin/settings")).json()["context_shaping_custom_prompt_version"] == version

    unchanged = await client.put("/admin/settings", json={"context_shaping_custom_prompt_text": "alpha"})
    assert unchanged.status_code == 200
    assert "context_shaping_custom_prompt_version" not in unchanged.json()["updated"]


@pytest.mark.asyncio
async def test_custom_prompt_validation_and_authoritative_preview(client):
    blank = await client.put(
        "/admin/settings",
        json={"context_shaping_custom_prompt_enabled": True, "context_shaping_custom_prompt_text": " "},
    )
    assert blank.status_code == 400
    oversized = await client.put("/admin/settings", json={"context_shaping_custom_prompt_text": "你" * 11000})
    assert oversized.status_code == 400

    preview = await client.post(
        "/admin/context-shaping/preview",
        json={
            "api_type": "openai-response",
            "caveman_enabled": True,
            "custom_enabled": True,
            "custom_text": "草稿",
        },
    )
    assert preview.status_code == 200
    data = preview.json()
    assert data["draft"] is True
    assert data["placement"] == "instructions"
    assert data["order"] == ["caveman", "custom"]
    assert data["combined_text"].endswith("草稿")
    assert json.dumps(data, ensure_ascii=False).count("草稿") == 1
