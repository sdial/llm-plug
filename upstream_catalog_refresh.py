"""固定来源的非阻塞 Upstream Catalog Candidate 刷新。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from loguru import logger

from upstream_catalog import UpstreamCatalog, catalog
from upstream_catalog_importers import (
    LITELLM_MODELS_URL,
    LITELLM_PROVIDERS_URL,
    MODELS_DEV_API_URL,
    build_candidate,
)

CATALOG_REFRESH_INTERVAL_SECONDS = 86400
CATALOG_REFRESH_INITIAL_DELAY_SECONDS = 30
CATALOG_SOURCE_MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DownloadedCatalogSource:
    name: str
    url: str
    body: bytes
    version: str | None


_refresh_lock: asyncio.Lock | None = None
_refresh_lock_loop: asyncio.AbstractEventLoop | None = None
_last_status: dict = {"status": "never"}


def _lock() -> asyncio.Lock:
    global _refresh_lock, _refresh_lock_loop
    loop = asyncio.get_running_loop()
    if _refresh_lock is None or _refresh_lock_loop is not loop:
        _refresh_lock = asyncio.Lock()
        _refresh_lock_loop = loop
    return _refresh_lock


def refresh_status() -> dict:
    return dict(_last_status)


async def _download(client: httpx.AsyncClient, name: str, url: str) -> DownloadedCatalogSource:
    async with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and int(declared) > CATALOG_SOURCE_MAX_BYTES:
            raise ValueError(f"{name} 超过目录下载大小上限")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > CATALOG_SOURCE_MAX_BYTES:
                raise ValueError(f"{name} 超过目录下载大小上限")
            chunks.append(chunk)
        version = response.headers.get("etag") or response.headers.get("last-modified")
        return DownloadedCatalogSource(name=name, url=url, body=b"".join(chunks), version=version)


async def refresh_catalog_candidate(*, upstream_catalog: UpstreamCatalog = catalog) -> dict:
    global _last_status
    async with _lock():
        started_at = datetime.now(UTC).isoformat()
        _last_status = {"status": "running", "started_at": started_at}
        try:
            timeout = httpx.Timeout(30.0, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                sources = await asyncio.gather(
                    _download(client, "models.dev", MODELS_DEV_API_URL),
                    _download(client, "litellm-models", LITELLM_MODELS_URL),
                    _download(client, "litellm-providers", LITELLM_PROVIDERS_URL),
                )
            by_name = {source.name: source for source in sources}
            candidate = build_candidate(
                by_name["models.dev"].body,
                by_name["litellm-models"].body,
                by_name["litellm-providers"].body,
                fetched_at=started_at,
                source_versions={name: source.version for name, source in by_name.items()},
            )
            current = await upstream_catalog.candidate()
            changed = current is None or current.revision != candidate.revision
            if changed:
                await upstream_catalog.replace_candidate(candidate)
            _last_status = {
                "status": "ok",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "candidate_revision": candidate.revision,
                "changed": changed,
                "conflict_count": len(candidate.conflicts),
                "source_versions": {name: source.version for name, source in by_name.items()},
            }
        except Exception as exc:
            logger.warning(f"upstream catalog refresh failed: {exc}")
            _last_status = {
                "status": "error",
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "error": str(exc),
            }
        return dict(_last_status)


async def run_catalog_refresh_loop() -> None:
    await asyncio.sleep(CATALOG_REFRESH_INITIAL_DELAY_SECONDS)
    while True:
        await refresh_catalog_candidate()
        await asyncio.sleep(CATALOG_REFRESH_INTERVAL_SECONDS)
