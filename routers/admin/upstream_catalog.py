"""Upstream Catalog 管理端：候选、发布、匹配与 Channel 档案绑定。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from channel_catalog import catalog as channel_catalog
from models.api_types import APIType
from models.upstream_profile import UpstreamCatalogDocument
from upstream_catalog import CandidateConflictError, RevisionImmutableError, RevisionNotFoundError, catalog
from upstream_catalog_refresh import refresh_catalog_candidate, refresh_status
from upstream_profile_resolver import ProfileResolutionError, exact_url_profile_match, resolve_upstream_profile

from .common import AdminAuthRoute, _validate_outbound_url

router = APIRouter(prefix="/admin/upstream-catalog", tags=["上游目录"], route_class=AdminAuthRoute)


class BindChannelProfileRequest(BaseModel):
    upstream_profile_id: str
    catalog_revision: str
    apply_default_url: bool = False
    confirm_high_risk: bool = False


class ActivateRevisionRequest(BaseModel):
    revision: str
    confirm_high_risk: bool = False


class DeleteRevisionRequest(BaseModel):
    confirm_high_risk: bool = False


def _summary(document: UpstreamCatalogDocument) -> dict:
    return {
        "revision": document.revision,
        "generated_at": document.generated_at,
        "profile_count": len(document.upstream_profiles),
        "model_count": len(document.model_profiles),
        "conflict_count": len(document.conflicts),
        "provenance": [item.model_dump(mode="json") for item in document.provenance],
    }


async def _catalog_document(revision: str | None) -> UpstreamCatalogDocument:
    try:
        return await catalog.revision(revision) if revision else await catalog.active()
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("")
async def get_catalog_status():
    active = await catalog.active()
    candidate = await catalog.candidate()
    return {
        "active": _summary(active),
        "candidate": _summary(candidate) if candidate else None,
        "revisions": await catalog.revisions(),
        "refresh": refresh_status(),
    }


@router.get("/profiles")
async def list_profiles(revision: Annotated[str | None, Query()] = None):
    document = await _catalog_document(revision)
    return {
        "revision": document.revision,
        "profiles": [profile.model_dump(mode="json") for profile in document.upstream_profiles],
    }


@router.get("/models")
async def list_profile_models(
    upstream_profile_id: Annotated[str, Query()],
    revision: Annotated[str | None, Query()] = None,
    query: Annotated[str, Query()] = "",
):
    document = await _catalog_document(revision)
    needle = query.casefold().strip()
    models = [
        model.model_dump(mode="json")
        for model in document.model_profiles
        if model.upstream_profile_id == upstream_profile_id
        and (not needle or needle in model.model_id.casefold() or needle in (model.family or "").casefold())
    ]
    return {"revision": document.revision, "models": models}


@router.get("/candidate")
async def get_candidate():
    candidate = await catalog.candidate()
    if candidate is None:
        raise HTTPException(status_code=404, detail="没有 Catalog Candidate")
    active = await catalog.active()
    return {
        "candidate": candidate.model_dump(mode="json"),
        "diff": {
            "from_revision": active.revision,
            "to_revision": candidate.revision,
            "profiles": len(candidate.upstream_profiles) - len(active.upstream_profiles),
            "models": len(candidate.model_profiles) - len(active.model_profiles),
            "conflicts": [conflict.model_dump(mode="json") for conflict in candidate.conflicts],
        },
    }


@router.put("/candidate")
async def import_candidate(document: UpstreamCatalogDocument):
    stored = await catalog.replace_candidate(document)
    return _summary(stored)


@router.post("/check")
async def check_now():
    return await refresh_catalog_candidate()


@router.post("/publish")
async def publish_candidate():
    try:
        return _summary(await catalog.publish_candidate())
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CandidateConflictError, RevisionImmutableError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/activate")
async def activate_revision(body: ActivateRevisionRequest):
    if not body.confirm_high_risk:
        raise HTTPException(status_code=409, detail="切换 active revision 需要 confirm_high_risk=true")
    try:
        return _summary(await catalog.activate_revision(body.revision))
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/revisions/{revision}")
async def delete_revision(revision: str, body: DeleteRevisionRequest):
    if not body.confirm_high_risk:
        raise HTTPException(status_code=409, detail="删除 revision 需要 confirm_high_risk=true")
    references = [channel.id for channel in (await channel_catalog.snapshot()).channels if channel.catalog_revision == revision]
    if references:
        raise HTTPException(status_code=409, detail=f"Catalog Revision 正被 Channel 引用: {', '.join(sorted(references))}")
    try:
        await catalog.delete_revision(revision)
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RevisionImmutableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": revision}


@router.get("/match-url")
async def match_url(base_url: Annotated[str, Query()], api_type: Annotated[APIType, Query()], revision: Annotated[str | None, Query()] = None):
    _validate_outbound_url(base_url)
    document = await _catalog_document(revision)
    matched = exact_url_profile_match(document, base_url, api_type)
    return {
        "revision": document.revision,
        "upstream_profile_id": matched or "generic",
        "match": "exact-unique" if matched else "generic-fallback",
    }


@router.get("/channels/{channel_id}/resolved")
async def resolved_channel_profile(
    channel_id: str,
    api_type: Annotated[APIType, Query()],
    model: Annotated[str, Query()],
):
    channel = next((item for item in (await channel_catalog.snapshot()).channels if item.id == channel_id), None)
    if channel is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    endpoint = channel.enabled_endpoint_for(api_type)
    if endpoint is None:
        raise HTTPException(status_code=404, detail=f"渠道无启用的 {api_type.value} 接入点")
    try:
        return (await resolve_upstream_profile(channel, endpoint, model)).model_dump(mode="json")
    except ProfileResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/channels/{channel_id}/bind")
async def bind_channel_profile(channel_id: str, body: BindChannelProfileRequest):
    channel = next((item for item in (await channel_catalog.snapshot()).channels if item.id == channel_id), None)
    if channel is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    try:
        document = await catalog.revision(body.catalog_revision)
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    profile = next((item for item in document.upstream_profiles if item.id == body.upstream_profile_id), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="Upstream Profile 不存在")
    endpoints = [endpoint.model_copy(deep=True) for endpoint in channel.endpoints]
    url_changed = False
    if body.apply_default_url:
        defaults = {item.api_type: item.canonical_base_url for item in profile.endpoints if item.canonical_base_url}
        for endpoint in endpoints:
            default_url = defaults.get(endpoint.api_type)
            if default_url and endpoint.base_url.rstrip("/") != default_url.rstrip("/"):
                _validate_outbound_url(default_url)
                endpoint.base_url = default_url
                url_changed = True
    revision_changed = channel.catalog_revision != body.catalog_revision or channel.upstream_profile_id != body.upstream_profile_id
    if (url_changed or revision_changed) and not body.confirm_high_risk:
        raise HTTPException(status_code=409, detail="档案版本或 URL 变化需要 confirm_high_risk=true")
    updated = await channel_catalog.update_channel(
        channel_id,
        {
            "upstream_profile_id": body.upstream_profile_id,
            "catalog_revision": body.catalog_revision,
            "endpoints": [endpoint.model_dump() for endpoint in endpoints],
        },
    )
    return updated
