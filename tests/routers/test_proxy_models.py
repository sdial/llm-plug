"""Tests for /v1/models and /v1/anthropic/models endpoints."""
import json

import pytest
from fastapi.testclient import TestClient

import config
import storage


@pytest.fixture(autouse=True)
def setup_channels(tmp_path, monkeypatch):
    """Set up test channels data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    channels_data = {
        "channels": [
            {
                "id": "ch_test1",
                "name": "Test Channel",
                "api_type": "openai-chat-completions",
                "base_url": "https://api.example.com",
                "api_key": "test-key",
                "models": ["gpt-4o"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
            {
                "id": "ch_test2",
                "name": "Anthropic Channel",
                "api_type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "test-key",
                "models": ["claude-sonnet-4-20250514"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
        ]
    }
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)
    with open(api_keys_file, "w") as f:
        json.dump({"api_keys": []}, f)

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


class TestOpenAIModelsEndpoint:
    def test_returns_models_without_auth(self):
        """GET /v1/models should work without any Authorization header."""
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert "gpt-4o" in model_ids
        assert "claude-sonnet-4-20250514" in model_ids

    def test_returns_models_even_with_proxy_api_key_set(self, monkeypatch):
        """GET /v1/models should work even when PROXY_API_KEY is set."""
        monkeypatch.setattr("routers.auth.PROXY_API_KEY", "some-secret-key")
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) > 0

    def test_returns_models_with_wrong_auth_header(self, monkeypatch):
        """GET /v1/models should work even with an invalid Bearer token."""
        monkeypatch.setattr("routers.auth.PROXY_API_KEY", "some-secret-key")
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 200


class TestAnthropicModelsEndpoint:
    def test_returns_models_without_auth(self):
        """GET /v1/anthropic/models should work without any Authorization header."""
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/anthropic/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        model_ids = [m["id"] for m in data["data"]]
        assert "claude-sonnet-4-20250514" in model_ids

    def test_returns_models_even_with_proxy_api_key_set(self, monkeypatch):
        """GET /v1/anthropic/models should work even when PROXY_API_KEY is set."""
        monkeypatch.setattr("routers.auth.PROXY_API_KEY", "some-secret-key")
        from main import app
        with TestClient(app) as client:
            resp = client.get("/v1/anthropic/models")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) > 0
