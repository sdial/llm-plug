"""版本化 Upstream Catalog：外部候选与运行时 revision 的唯一住所。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

import config
from atomic_json import write_json_atomic
from models.api_types import APIType
from models.upstream_profile import (
    DEFAULT_CATALOG_REVISION,
    AuthScheme,
    CapabilityMatrix,
    CapabilityState,
    UpstreamCatalogDocument,
    UpstreamEndpointProfile,
    UpstreamProfile,
)


class UpstreamCatalogError(RuntimeError):
    pass


class RevisionNotFoundError(UpstreamCatalogError):
    pass


class RevisionImmutableError(UpstreamCatalogError):
    pass


class CandidateConflictError(UpstreamCatalogError):
    pass


_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_revision_name(revision: str) -> str:
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision) or ".." in revision:
        raise UpstreamCatalogError("Catalog revision 只能包含字母、数字、点、下划线和连字符，且不能含 '..'")
    return revision


def _generic_document() -> UpstreamCatalogDocument:
    common = CapabilityMatrix(
        input_modalities={"text": CapabilityState.SUPPORTED},
        output_modalities={"text": CapabilityState.SUPPORTED},
    )
    return UpstreamCatalogDocument(
        revision=DEFAULT_CATALOG_REVISION,
        generated_at="2026-09-09T00:00:00Z",
        upstream_profiles=[
            UpstreamProfile(
                id="generic",
                name="Generic standard upstream",
                endpoints=[
                    UpstreamEndpointProfile(api_type=APIType.OPENAI_CHAT, auth_scheme=AuthScheme.BEARER),
                    UpstreamEndpointProfile(api_type=APIType.OPENAI_RESPONSE, auth_scheme=AuthScheme.BEARER),
                    UpstreamEndpointProfile(
                        api_type=APIType.ANTHROPIC,
                        auth_scheme=AuthScheme.X_API_KEY,
                        anthropic_version="2023-06-01",
                    ),
                ],
                capabilities=common,
            ),
            UpstreamProfile(
                id="openai",
                name="OpenAI",
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=APIType.OPENAI_CHAT,
                        canonical_base_url="https://api.openai.com/v1",
                        auth_scheme=AuthScheme.BEARER,
                    ),
                    UpstreamEndpointProfile(
                        api_type=APIType.OPENAI_RESPONSE,
                        canonical_base_url="https://api.openai.com/v1",
                        auth_scheme=AuthScheme.BEARER,
                    ),
                ],
                capabilities=common,
            ),
            UpstreamProfile(
                id="anthropic",
                name="Anthropic",
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=APIType.ANTHROPIC,
                        canonical_base_url="https://api.anthropic.com",
                        auth_scheme=AuthScheme.X_API_KEY,
                        anthropic_version="2023-06-01",
                    )
                ],
                capabilities=common,
            ),
            UpstreamProfile(
                id="deepseek",
                name="DeepSeek",
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=APIType.OPENAI_CHAT,
                        canonical_base_url="https://api.deepseek.com",
                        auth_scheme=AuthScheme.BEARER,
                    )
                ],
                capabilities=CapabilityMatrix(
                    input_modalities={"text": CapabilityState.SUPPORTED},
                    output_modalities={"text": CapabilityState.SUPPORTED},
                    features={"parallel_tool_calls": CapabilityState.UNSUPPORTED},
                ),
                filter_think_content=True,
            ),
            UpstreamProfile(
                id="minimax",
                name="MiniMax",
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=APIType.OPENAI_CHAT,
                        canonical_base_url="https://api.minimax.chat/v1",
                        auth_scheme=AuthScheme.BEARER,
                    )
                ],
                capabilities=common,
                requires_single_system_message=True,
            ),
            UpstreamProfile(
                id="qwen",
                name="Qwen / DashScope",
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=APIType.OPENAI_CHAT,
                        canonical_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        auth_scheme=AuthScheme.BEARER,
                    )
                ],
                capabilities=common,
                normalize_developer_role=True,
            ),
            UpstreamProfile(
                id="openrouter",
                name="OpenRouter",
                endpoints=[
                    UpstreamEndpointProfile(
                        api_type=APIType.OPENAI_CHAT,
                        canonical_base_url="https://openrouter.ai/api/v1",
                        auth_scheme=AuthScheme.BEARER,
                    )
                ],
                capabilities=common,
            ),
        ],
    )


def builtin_profiles() -> list[UpstreamProfile]:
    return [profile.model_copy(deep=True) for profile in _generic_document().upstream_profiles]


def canonical_document_bytes(document: UpstreamCatalogDocument) -> bytes:
    payload = document.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def document_digest(document: UpstreamCatalogDocument) -> str:
    return hashlib.sha256(canonical_document_bytes(document)).hexdigest()


class UpstreamCatalog:
    def __init__(self, root: Callable[[], str]) -> None:
        self._root = root
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._cache: dict[str, UpstreamCatalogDocument] = {}

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def reset(self) -> None:
        self._lock = None
        self._lock_loop = None
        self._cache.clear()

    def _path(self, *parts: str) -> str:
        return str(Path(self._root(), *parts))

    async def ensure_builtin(self) -> None:
        async with self._get_lock():
            path = self._path("revisions", f"{DEFAULT_CATALOG_REVISION}.json")
            if not os.path.exists(path):
                await asyncio.to_thread(self._write_document, path, _generic_document())
            active_path = self._path("active.json")
            active_needs_upgrade = not os.path.exists(active_path)
            if not active_needs_upgrade:
                try:
                    active_needs_upgrade = self._read_active() == "builtin-1"
                except (OSError, ValueError, UpstreamCatalogError):
                    active_needs_upgrade = True
            if active_needs_upgrade:
                await asyncio.to_thread(
                    write_json_atomic,
                    active_path,
                    {"revision": DEFAULT_CATALOG_REVISION},
                    temp_prefix=".upstream_active_",
                )

    async def active_revision(self) -> str:
        await self.ensure_builtin()
        return await asyncio.to_thread(self._read_active)

    async def active(self) -> UpstreamCatalogDocument:
        return await self.revision(await self.active_revision())

    async def revision(self, revision: str) -> UpstreamCatalogDocument:
        revision = _validate_revision_name(revision)
        if revision.startswith("builtin-"):
            await self.ensure_builtin()
        async with self._get_lock():
            cached = self._cache.get(revision)
            if cached is not None:
                return cached.model_copy(deep=True)
            path = self._path("revisions", f"{revision}.json")
            if not os.path.exists(path):
                raise RevisionNotFoundError(f"Upstream Catalog revision 不存在: {revision}")
            document = await asyncio.to_thread(self._read_document, path)
            if document.revision != revision:
                raise UpstreamCatalogError(f"revision 文件名与内容不一致: {revision} != {document.revision}")
            self._cache[revision] = document
            return document.model_copy(deep=True)

    async def candidate(self) -> UpstreamCatalogDocument | None:
        path = self._path("candidate.json")
        if not os.path.exists(path):
            return None
        return await asyncio.to_thread(self._read_document, path)

    async def replace_candidate(self, document: UpstreamCatalogDocument) -> UpstreamCatalogDocument:
        async with self._get_lock():
            await asyncio.to_thread(self._write_document, self._path("candidate.json"), document)
            return document.model_copy(deep=True)

    async def publish_candidate(self) -> UpstreamCatalogDocument:
        async with self._get_lock():
            candidate_path = self._path("candidate.json")
            if not os.path.exists(candidate_path):
                raise RevisionNotFoundError("没有可发布的 Catalog Candidate")
            document = await asyncio.to_thread(self._read_document, candidate_path)
            if document.conflicts:
                raise CandidateConflictError("Catalog Candidate 存在未解决冲突")
            revision_path = self._path("revisions", f"{document.revision}.json")
            if os.path.exists(revision_path):
                existing = await asyncio.to_thread(self._read_document, revision_path)
                if document_digest(existing) != document_digest(document):
                    raise RevisionImmutableError(f"revision 已存在且内容不同: {document.revision}")
            else:
                await asyncio.to_thread(self._write_document, revision_path, document)
            self._cache[document.revision] = document
            return document.model_copy(deep=True)

    async def activate_revision(self, revision: str) -> UpstreamCatalogDocument:
        """切换新建绑定默认 revision；不改写任何既有 Channel。"""
        revision = _validate_revision_name(revision)
        await self.ensure_builtin()
        async with self._get_lock():
            document = self._cache.get(revision)
            if document is None:
                path = self._path("revisions", f"{revision}.json")
                if not os.path.exists(path):
                    raise RevisionNotFoundError(f"Upstream Catalog revision 不存在: {revision}")
                document = await asyncio.to_thread(self._read_document, path)
                if document.revision != revision:
                    raise UpstreamCatalogError(f"revision 文件名与内容不一致: {revision} != {document.revision}")
                self._cache[revision] = document
            await asyncio.to_thread(
                write_json_atomic,
                self._path("active.json"),
                {"revision": revision},
                temp_prefix=".upstream_active_",
            )
        return document

    async def revisions(self) -> list[str]:
        await self.ensure_builtin()
        root = Path(self._path("revisions"))
        return sorted(path.stem for path in root.glob("*.json"))

    async def delete_revision(self, revision: str) -> None:
        """删除一个未激活 revision；Channel 引用检查由管理 API 的目录边界执行。"""
        revision = _validate_revision_name(revision)
        if revision == DEFAULT_CATALOG_REVISION:
            raise RevisionImmutableError("内置 Catalog Revision 不可删除")
        await self.ensure_builtin()
        async with self._get_lock():
            if revision == await asyncio.to_thread(self._read_active):
                raise RevisionImmutableError("active Catalog Revision 不可删除")
            path = self._path("revisions", f"{revision}.json")
            if not os.path.exists(path):
                raise RevisionNotFoundError(f"Catalog Revision 不存在: {revision}")
            await asyncio.to_thread(os.remove, path)
            self._cache.pop(revision, None)

    def _read_active(self) -> str:
        with open(self._path("active.json"), encoding="utf-8") as file:
            payload = json.load(file)
        revision = payload.get("revision")
        if not isinstance(revision, str) or not revision:
            raise UpstreamCatalogError("active.json 缺少 revision")
        return _validate_revision_name(revision)

    @staticmethod
    def _read_document(path: str) -> UpstreamCatalogDocument:
        try:
            with open(path, encoding="utf-8") as file:
                return UpstreamCatalogDocument.model_validate(json.load(file))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise UpstreamCatalogError(f"无效 Upstream Catalog: {path}: {exc}") from exc

    @staticmethod
    def _write_document(path: str, document: UpstreamCatalogDocument) -> None:
        payload: dict[str, Any] = document.model_dump(mode="json")
        write_json_atomic(path, payload, temp_prefix=".upstream_catalog_")


catalog = UpstreamCatalog(root=lambda: os.path.join(config.DATA_DIR, "upstream_catalog"))
