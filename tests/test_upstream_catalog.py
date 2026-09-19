import pytest

from models.upstream_profile import CatalogConflict, UpstreamCatalogDocument
from upstream_catalog import CandidateConflictError, RevisionImmutableError, UpstreamCatalog, UpstreamCatalogError, _generic_document


@pytest.fixture
def upstream_store(tmp_path):
    return UpstreamCatalog(root=lambda: str(tmp_path / "upstream_catalog"))


@pytest.mark.asyncio
async def test_builtin_catalog_is_available_offline(upstream_store):
    assert await upstream_store.active_revision() == "builtin-2"
    document = await upstream_store.active()
    assert {profile.id for profile in document.upstream_profiles} == {
        "anthropic",
        "deepseek",
        "generic",
        "minimax",
        "openai",
        "openrouter",
        "qwen",
    }
    generic = next(profile for profile in document.upstream_profiles if profile.id == "generic")
    assert len(generic.endpoints) == 3


@pytest.mark.asyncio
async def test_published_revision_is_immutable_and_reads_are_copies(upstream_store):
    candidate = _generic_document().model_copy(update={"revision": "catalog-test"}, deep=True)
    await upstream_store.replace_candidate(candidate)
    published = await upstream_store.publish_candidate()
    assert await upstream_store.active_revision() == "builtin-2"
    published.upstream_profiles[0].name = "mutated by caller"

    reread = await upstream_store.revision("catalog-test")
    assert reread.upstream_profiles[0].name != "mutated by caller"

    changed = candidate.model_copy(deep=True)
    changed.upstream_profiles[0].name = "different immutable content"
    await upstream_store.replace_candidate(changed)
    with pytest.raises(RevisionImmutableError):
        await upstream_store.publish_candidate()


@pytest.mark.asyncio
async def test_candidate_with_conflicts_cannot_publish(upstream_store):
    candidate = _generic_document().model_copy(update={"revision": "catalog-conflict"}, deep=True)
    candidate.conflicts.append(CatalogConflict(path="providers.x.url", message="conflict"))
    await upstream_store.replace_candidate(candidate)
    with pytest.raises(CandidateConflictError):
        await upstream_store.publish_candidate()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["../outside", "..", "catalog/test", "catalog\\test"])
async def test_catalog_rejects_path_like_revision_names(upstream_store, revision):
    with pytest.raises(UpstreamCatalogError):
        await upstream_store.revision(revision)


def test_catalog_rejects_dangling_model_profile():
    payload = _generic_document().model_dump(mode="json")
    payload["model_profiles"] = [
        {
            "id": "missing:model",
            "upstream_profile_id": "missing",
            "model_id": "model",
        }
    ]
    with pytest.raises(ValueError, match="不存在"):
        UpstreamCatalogDocument.model_validate(payload)
