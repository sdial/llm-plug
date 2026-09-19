import json

import pytest

import upstream_catalog_refresh as refresh_module
from upstream_catalog import UpstreamCatalog
from upstream_catalog_refresh import DownloadedCatalogSource, refresh_catalog_candidate


def _raw(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.fixture
def upstream_store(tmp_path):
    return UpstreamCatalog(root=lambda: str(tmp_path / "upstream_catalog"))


@pytest.mark.asyncio
async def test_refresh_creates_candidate_without_activating_it(monkeypatch, upstream_store):
    async def fake_download(_client, name, url):
        bodies = {
            "models.dev": _raw(
                {
                    "relay": {
                        "id": "relay",
                        "name": "Relay",
                        "npm": "@ai-sdk/openai-compatible",
                        "api": "https://relay.example/v1",
                        "models": {},
                    }
                }
            ),
            "litellm-models": _raw({}),
            "litellm-providers": _raw({}),
        }
        return DownloadedCatalogSource(name=name, url=url, body=bodies[name], version="v1")

    monkeypatch.setattr(refresh_module, "_download", fake_download)
    before = await upstream_store.active_revision()
    status = await refresh_catalog_candidate(upstream_catalog=upstream_store)

    assert status["status"] == "ok"
    assert status["changed"] is True
    assert (await upstream_store.candidate()).revision == status["candidate_revision"]
    assert await upstream_store.active_revision() == before


@pytest.mark.asyncio
async def test_refresh_failure_preserves_existing_candidate(monkeypatch, upstream_store):
    existing = await upstream_store.active()
    existing.revision = "catalog-existing"
    await upstream_store.replace_candidate(existing)

    async def failed_download(_client, name, _url):
        if name == "litellm-models":
            raise RuntimeError("source unavailable")
        return DownloadedCatalogSource(name=name, url="https://example.invalid", body=_raw({}), version=None)

    monkeypatch.setattr(refresh_module, "_download", failed_download)
    status = await refresh_catalog_candidate(upstream_catalog=upstream_store)

    assert status["status"] == "error"
    assert "source unavailable" in status["error"]
    assert (await upstream_store.candidate()).revision == "catalog-existing"
