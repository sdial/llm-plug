import pytest
from fastapi import HTTPException

import routers.admin.upstream_catalog as catalog_router
from models.api_types import APIType
from models.channel import Channel, Endpoint
from upstream_catalog import UpstreamCatalog, _generic_document


@pytest.fixture
def upstream_store(tmp_path, monkeypatch):
    store = UpstreamCatalog(root=lambda: str(tmp_path / "upstream_catalog"))
    monkeypatch.setattr(catalog_router, "catalog", store)
    return store


@pytest.mark.asyncio
async def test_catalog_status_profiles_and_exact_url_match(upstream_store):
    status = await catalog_router.get_catalog_status()
    profiles = await catalog_router.list_profiles()
    matched = await catalog_router.match_url("https://api.anthropic.com/", APIType.ANTHROPIC)
    fallback = await catalog_router.match_url("https://relay.example/v1", APIType.OPENAI_CHAT)

    assert status["active"]["revision"] == "builtin-2"
    assert len(profiles["profiles"]) >= 7
    assert matched["upstream_profile_id"] == "anthropic"
    assert matched["match"] == "exact-unique"
    assert fallback == {"revision": "builtin-2", "upstream_profile_id": "generic", "match": "generic-fallback"}


@pytest.mark.asyncio
async def test_publish_and_activate_do_not_touch_channel_catalog(monkeypatch, upstream_store):
    candidate = _generic_document().model_copy(update={"revision": "catalog-next"}, deep=True)
    await upstream_store.replace_candidate(candidate)

    async def unexpected_channel_mutation(*_args, **_kwargs):
        raise AssertionError("发布目录不得产生 Channel mutation")

    monkeypatch.setattr(catalog_router.channel_catalog, "update_channel", unexpected_channel_mutation)
    published = await catalog_router.publish_candidate()
    assert published["revision"] == "catalog-next"
    assert await upstream_store.active_revision() == "builtin-2"

    with pytest.raises(HTTPException) as caught:
        await catalog_router.activate_revision(catalog_router.ActivateRevisionRequest(revision="catalog-next"))
    assert caught.value.status_code == 409
    activated = await catalog_router.activate_revision(catalog_router.ActivateRevisionRequest(revision="catalog-next", confirm_high_risk=True))
    assert activated["revision"] == "catalog-next"


@pytest.mark.asyncio
async def test_referenced_revision_cannot_be_deleted(monkeypatch, upstream_store):
    candidate = _generic_document().model_copy(update={"revision": "catalog-old"}, deep=True)
    await upstream_store.replace_candidate(candidate)
    await upstream_store.publish_candidate()
    channel = Channel(
        id="ch-ref",
        name="reference",
        api_key="secret",
        upstream_profile_id="generic",
        catalog_revision="catalog-old",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://relay.example")],
    )

    class Snapshot:
        channels = [channel]

    async def snapshot():
        return Snapshot()

    monkeypatch.setattr(catalog_router.channel_catalog, "snapshot", snapshot)
    with pytest.raises(HTTPException) as caught:
        await catalog_router.delete_revision(
            "catalog-old",
            catalog_router.DeleteRevisionRequest(confirm_high_risk=True),
        )
    assert caught.value.status_code == 409

    Snapshot.channels = []
    assert await catalog_router.delete_revision(
        "catalog-old",
        catalog_router.DeleteRevisionRequest(confirm_high_risk=True),
    ) == {"deleted": "catalog-old"}


@pytest.mark.asyncio
async def test_bind_preserves_manual_url_unless_explicitly_applied(monkeypatch, upstream_store):
    channel = Channel(
        id="ch-one",
        name="one",
        api_key="secret",
        upstream_profile_id="generic",
        catalog_revision="builtin-2",
        endpoints=[Endpoint(api_type=APIType.ANTHROPIC, base_url="https://relay.example")],
    )
    captured = {}

    class Snapshot:
        channels = [channel]

    async def snapshot():
        return Snapshot()

    async def update(_channel_id, changes):
        captured.update(changes)
        return channel

    monkeypatch.setattr(catalog_router.channel_catalog, "snapshot", snapshot)
    monkeypatch.setattr(catalog_router.channel_catalog, "update_channel", update)

    with pytest.raises(HTTPException) as caught:
        await catalog_router.bind_channel_profile(
            "ch-one",
            catalog_router.BindChannelProfileRequest(upstream_profile_id="anthropic", catalog_revision="builtin-2"),
        )
    assert caught.value.status_code == 409

    await catalog_router.bind_channel_profile(
        "ch-one",
        catalog_router.BindChannelProfileRequest(
            upstream_profile_id="anthropic",
            catalog_revision="builtin-2",
            confirm_high_risk=True,
        ),
    )
    assert captured["endpoints"][0]["base_url"] == "https://relay.example"

    await catalog_router.bind_channel_profile(
        "ch-one",
        catalog_router.BindChannelProfileRequest(
            upstream_profile_id="anthropic",
            catalog_revision="builtin-2",
            apply_default_url=True,
            confirm_high_risk=True,
        ),
    )
    assert captured["endpoints"][0]["base_url"] == "https://api.anthropic.com"
