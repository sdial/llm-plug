"""Channel Catalog：Channel 与 Model Group 定义及其精确变更事实的唯一住所。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from loguru import logger
from pydantic import ValidationError

import config
from atomic_json import write_json_atomic
from models.channel import Channel, migrate_channel_profile_payload, normalize_channel_payload
from models.model_group import ModelGroup

_CACHE_TTL = 5.0
_SCHEMA_VERSION = 3


class ChangeKind(str, Enum):
    created = "created"
    updated = "updated"
    toggled = "toggled"
    deleted = "deleted"


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    channels: tuple[Channel, ...]
    model_groups: tuple[ModelGroup, ...]


@dataclass(frozen=True, slots=True)
class ChannelChange:
    kind: ChangeKind
    channel_id: str
    before: Channel | None
    after: Channel | None


@dataclass(frozen=True, slots=True)
class ModelGroupChange:
    kind: ChangeKind
    model_group_id: str
    before: ModelGroup | None
    after: ModelGroup | None


class CatalogEffects(Protocol):
    async def apply_channel_change(self, change: ChannelChange, snapshot: CatalogSnapshot) -> None: ...

    async def apply_model_group_change(self, change: ModelGroupChange, snapshot: CatalogSnapshot) -> None: ...


class _NoopEffects:
    async def apply_channel_change(self, change: ChannelChange, snapshot: CatalogSnapshot) -> None:
        return None

    async def apply_model_group_change(self, change: ModelGroupChange, snapshot: CatalogSnapshot) -> None:
        return None


class CatalogCorruptionError(RuntimeError):
    """目录 JSON 已损坏；原文件保持不变并已备份。"""

    def __init__(self, path: str, backup_path: str, original: Exception):
        super().__init__(f"{path} is not valid JSON; preserved original and backed up to {backup_path}")
        self.path = path
        self.backup_path = backup_path
        self.original = original


class CatalogSynchronizationError(RuntimeError):
    """目录已提交，但运行状态未能完成同步。"""

    def __init__(self, change: ChannelChange | ModelGroupChange, original: Exception):
        super().__init__(f"catalog change committed but runtime synchronization failed: {change}")
        self.change = change
        self.committed = True
        self.original = original


class CatalogConflictError(ValueError):
    """目录唯一性约束冲突。"""


class ChannelCatalog:
    """以一个类型化 interface 隐藏 JSON、缓存、迁移和变更同步。"""

    def __init__(
        self,
        *,
        path: Callable[[], str],
        effects: CatalogEffects | None = None,
    ) -> None:
        self._path = path
        self._effects: CatalogEffects = effects or _NoopEffects()
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._snapshot: CatalogSnapshot | None = None
        self._raw: dict[str, Any] | None = None
        self._cache_ts = 0.0
        self._file_sig: tuple[int, int] | None = None
        self._cached_path: str | None = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def reset(self) -> None:
        """释放缓存与 loop 绑定状态；应用测试和热重载可显式调用。"""
        self._lock = None
        self._lock_loop = None
        self._snapshot = None
        self._raw = None
        self._cache_ts = 0.0
        self._file_sig = None
        self._cached_path = None

    async def snapshot(self) -> CatalogSnapshot:
        async with self._get_lock():
            return _copy_snapshot(await self._load_locked())

    async def model_groups(self) -> list[ModelGroup]:
        return list((await self.snapshot()).model_groups)

    async def add_channel(self, channel: Channel) -> Channel:
        async with self._get_lock():
            snapshot = await self._load_locked()
            stored = channel.model_copy(deep=True)
            if any(existing.id == stored.id for existing in snapshot.channels):
                raise CatalogConflictError(f"渠道 ID 已存在: {stored.id}")
            next_snapshot = CatalogSnapshot((*snapshot.channels, stored), snapshot.model_groups)
            change = ChannelChange(ChangeKind.created, stored.id, None, stored)
            await self._commit_locked(next_snapshot, change)
            return stored.model_copy(deep=True)

    async def update_channel(self, channel_id: str, updates: dict[str, Any]) -> Channel | None:
        async with self._get_lock():
            if "id" in updates:
                raise CatalogConflictError("渠道 ID 不可修改")
            snapshot = await self._load_locked()
            channels = list(snapshot.channels)
            for index, channel in enumerate(channels):
                if channel.id != channel_id:
                    continue
                updated = Channel(**{**channel.model_dump(), **updates})
                channels[index] = updated
                next_snapshot = CatalogSnapshot(tuple(channels), snapshot.model_groups)
                change = ChannelChange(ChangeKind.updated, channel_id, channel, updated)
                await self._commit_locked(next_snapshot, change)
                return updated.model_copy(deep=True)
            return None

    async def toggle_channel(self, channel_id: str) -> Channel | None:
        async with self._get_lock():
            snapshot = await self._load_locked()
            channels = list(snapshot.channels)
            for index, channel in enumerate(channels):
                if channel.id != channel_id:
                    continue
                updated = channel.model_copy(update={"enabled": not channel.enabled})
                channels[index] = updated
                next_snapshot = CatalogSnapshot(tuple(channels), snapshot.model_groups)
                change = ChannelChange(ChangeKind.toggled, channel_id, channel, updated)
                await self._commit_locked(next_snapshot, change)
                return updated.model_copy(deep=True)
            return None

    async def delete_channel(self, channel_id: str) -> Channel | None:
        async with self._get_lock():
            snapshot = await self._load_locked()
            removed = next((channel for channel in snapshot.channels if channel.id == channel_id), None)
            if removed is None:
                return None
            next_snapshot = CatalogSnapshot(tuple(channel for channel in snapshot.channels if channel.id != channel_id), snapshot.model_groups)
            change = ChannelChange(ChangeKind.deleted, channel_id, removed, None)
            await self._commit_locked(next_snapshot, change)
            return removed.model_copy(deep=True)

    async def channels_for_model(self, model: str) -> list[Channel]:
        snapshot = await self.snapshot()
        return [channel for channel in snapshot.channels if channel.enabled and model in channel.models]

    async def get_model_group_by_name(self, name: str) -> ModelGroup | None:
        snapshot = await self.snapshot()
        return next((group for group in snapshot.model_groups if group.name == name and group.enabled), None)

    async def add_model_group(self, group: ModelGroup) -> ModelGroup:
        async with self._get_lock():
            snapshot = await self._load_locked()
            stored = group.model_copy(deep=True)
            if any(existing.id == stored.id for existing in snapshot.model_groups):
                raise CatalogConflictError(f"模型组 ID 已存在: {stored.id}")
            if any(existing.name == stored.name for existing in snapshot.model_groups):
                raise CatalogConflictError("模型组名称已存在")
            groups = [*snapshot.model_groups, stored]
            next_snapshot = CatalogSnapshot(snapshot.channels, tuple(groups))
            change = ModelGroupChange(ChangeKind.created, stored.id, None, stored)
            await self._commit_locked(next_snapshot, change)
            return stored.model_copy(deep=True)

    async def update_model_group(self, group_id: str, updates: dict[str, Any]) -> ModelGroup | None:
        async with self._get_lock():
            if "id" in updates:
                raise CatalogConflictError("模型组 ID 不可修改")
            snapshot = await self._load_locked()
            groups = list(snapshot.model_groups)
            for index, group in enumerate(groups):
                if group.id != group_id:
                    continue
                updated = ModelGroup(**{**group.model_dump(), **updates})
                if any(existing.id != group_id and existing.name == updated.name for existing in groups):
                    raise CatalogConflictError("模型组名称已存在")
                groups[index] = updated
                next_snapshot = CatalogSnapshot(snapshot.channels, tuple(groups))
                change = ModelGroupChange(ChangeKind.updated, group_id, group, updated)
                await self._commit_locked(next_snapshot, change)
                return updated.model_copy(deep=True)
            return None

    async def toggle_model_group(self, group_id: str) -> ModelGroup | None:
        async with self._get_lock():
            snapshot = await self._load_locked()
            groups = list(snapshot.model_groups)
            for index, group in enumerate(groups):
                if group.id != group_id:
                    continue
                updated = group.model_copy(update={"enabled": not group.enabled})
                groups[index] = updated
                next_snapshot = CatalogSnapshot(snapshot.channels, tuple(groups))
                change = ModelGroupChange(ChangeKind.toggled, group_id, group, updated)
                await self._commit_locked(next_snapshot, change)
                return updated.model_copy(deep=True)
            return None

    async def delete_model_group(self, group_id: str) -> bool:
        async with self._get_lock():
            snapshot = await self._load_locked()
            removed = next((group for group in snapshot.model_groups if group.id == group_id), None)
            if removed is None:
                return False
            next_snapshot = CatalogSnapshot(snapshot.channels, tuple(group for group in snapshot.model_groups if group.id != group_id))
            change = ModelGroupChange(ChangeKind.deleted, group_id, removed, None)
            await self._commit_locked(next_snapshot, change)
            return True

    async def take_legacy_lb_config(self) -> dict[str, Any] | None:
        """一次性取出旧 ``channels.json.lb_config``，并由目录自身完成删除。"""
        async with self._get_lock():
            snapshot = await self._load_locked()
            raw = self._raw or {}
            legacy = raw.get("lb_config")
            if not isinstance(legacy, dict):
                return None
            next_raw = dict(raw)
            next_raw.pop("lb_config", None)
            path = self._path()
            await asyncio.to_thread(_write_raw, path, next_raw)
            self._raw = next_raw
            self._snapshot = snapshot
            self._cache_ts = time.time()
            self._file_sig = _file_signature(path)
            self._cached_path = path
            return dict(legacy)

    async def invalidate(self) -> None:
        async with self._get_lock():
            self._snapshot = None
            self._raw = None
            self._cache_ts = 0.0
            self._file_sig = None
            self._cached_path = None

    async def _load_locked(self) -> CatalogSnapshot:
        path = self._path()
        now = time.time()
        file_sig = _file_signature(path)
        if self._snapshot is not None and self._cached_path == path and now - self._cache_ts < _CACHE_TTL and file_sig == self._file_sig:
            return self._snapshot

        raw, migrated = await asyncio.to_thread(_read_raw, path)
        snapshot = _parse_snapshot(raw)
        if file_sig is None:
            await asyncio.to_thread(_write_raw, path, _serialize_snapshot(snapshot, raw))
            raw = _serialize_snapshot(snapshot, raw)
        elif migrated:
            backup_path = f"{path}.pre-endpoints-{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns()}.bak"
            await asyncio.to_thread(shutil.copy2, path, backup_path)
            await asyncio.to_thread(_write_raw, path, _serialize_snapshot(snapshot, raw))
            raw = _serialize_snapshot(snapshot, raw)
        self._raw = raw
        self._snapshot = snapshot
        self._cache_ts = time.time()
        self._file_sig = _file_signature(path)
        self._cached_path = path
        return snapshot

    async def _commit_locked(self, snapshot: CatalogSnapshot, change: ChannelChange | ModelGroupChange) -> None:
        raw = _serialize_snapshot(snapshot, self._raw or {})
        await asyncio.to_thread(_write_raw, self._path(), raw)
        self._raw = raw
        self._snapshot = snapshot
        self._cache_ts = time.time()
        self._file_sig = _file_signature(self._path())
        self._cached_path = self._path()
        try:
            effect_snapshot = _copy_snapshot(snapshot)
            if isinstance(change, ChannelChange):
                effect_change = ChannelChange(
                    change.kind,
                    change.channel_id,
                    change.before.model_copy(deep=True) if change.before else None,
                    change.after.model_copy(deep=True) if change.after else None,
                )
                await self._effects.apply_channel_change(effect_change, effect_snapshot)
            else:
                effect_change = ModelGroupChange(
                    change.kind,
                    change.model_group_id,
                    change.before.model_copy(deep=True) if change.before else None,
                    change.after.model_copy(deep=True) if change.after else None,
                )
                await self._effects.apply_model_group_change(effect_change, effect_snapshot)
        except Exception as exc:
            raise CatalogSynchronizationError(change, exc) from exc


def _copy_snapshot(snapshot: CatalogSnapshot) -> CatalogSnapshot:
    return CatalogSnapshot(
        tuple(channel.model_copy(deep=True) for channel in snapshot.channels),
        tuple(group.model_copy(deep=True) for group in snapshot.model_groups),
    )


def _file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    return stat_result.st_mtime_ns, stat_result.st_size


def _read_raw(path: str) -> tuple[dict[str, Any], bool]:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if not os.path.exists(path):
        return {"channels": [], "model_groups": []}, False
    try:
        with open(path, encoding="utf-8") as file:
            raw = json.load(file)
    except json.JSONDecodeError as exc:
        timestamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        backup_path = f"{path}.corrupt-{timestamp}-{time.time_ns()}"
        shutil.copy2(path, backup_path)
        logger.error(f"catalog file is not valid JSON, refusing to overwrite it; backup saved to {backup_path}: {exc}")
        raise CatalogCorruptionError(path, backup_path, exc) from exc
    if not isinstance(raw, dict):
        raw = {"channels": [], "model_groups": []}
    channels = raw.get("channels")
    if not isinstance(channels, list):
        logger.warning("catalog channels must be a list; normalizing invalid value to an empty list")
        raw = {**raw, "channels": []}
        channels = []
        migrated = True
    else:
        migrated = False
    normalized = []
    for channel in channels:
        original = channel
        channel_migrated = False
        if isinstance(channel, dict) and "endpoints" not in channel and channel:
            channel = normalize_channel_payload(channel)
            channel_migrated = True
        channel, profile_migrated = migrate_channel_profile_payload(channel)
        channel_migrated = channel_migrated or profile_migrated
        try:
            Channel(**channel)
        except (TypeError, ValidationError):
            normalized.append(original)
            continue
        normalized.append(channel)
        migrated = migrated or channel_migrated
    migrated = migrated or raw.get("schema_version") != _SCHEMA_VERSION
    if migrated:
        raw = {**raw, "schema_version": _SCHEMA_VERSION, "channels": normalized}
    return raw, migrated


def _parse_snapshot(raw: dict[str, Any]) -> CatalogSnapshot:
    channels: list[Channel] = []
    for index, value in enumerate(raw.get("channels", [])):
        try:
            channels.append(Channel(**value))
        except (TypeError, ValidationError) as exc:
            channel_id = value.get("id") if isinstance(value, dict) else None
            logger.warning(f"skip invalid channel entry index={index} id={channel_id}: {exc}")
    groups: list[ModelGroup] = []
    for index, value in enumerate(raw.get("model_groups", [])):
        try:
            groups.append(ModelGroup(**value))
        except (TypeError, ValidationError) as exc:
            group_id = value.get("id") if isinstance(value, dict) else None
            logger.warning(f"skip invalid model group entry index={index} id={group_id}: {exc}")
    return CatalogSnapshot(tuple(channels), tuple(groups))


def _serialize_snapshot(snapshot: CatalogSnapshot, base: dict[str, Any]) -> dict[str, Any]:
    invalid_channels = _invalid_entries(base.get("channels", []), Channel)
    invalid_groups = _invalid_entries(base.get("model_groups", []), ModelGroup)
    return {
        **base,
        "schema_version": _SCHEMA_VERSION,
        "channels": [channel.to_storage_dict() for channel in snapshot.channels] + invalid_channels,
        "model_groups": [group.model_dump() for group in snapshot.model_groups] + invalid_groups,
    }


def _invalid_entries(values: Any, model_type: type[Channel] | type[ModelGroup]) -> list[Any]:
    if not isinstance(values, list):
        return []
    invalid = []
    for value in values:
        try:
            model_type(**value)
        except (TypeError, ValidationError):
            invalid.append(value)
    return invalid


def _write_raw(path: str, raw: dict[str, Any]) -> None:
    write_json_atomic(path, raw, temp_prefix=".channels_")


class RuntimeCatalogEffects:
    """把精确目录变更同步到进程内运行状态。"""

    async def apply_channel_change(self, change: ChannelChange, snapshot: CatalogSnapshot) -> None:
        if change.kind is ChangeKind.created:
            return
        from client import remove_channel_client
        from proxy import outcomes

        if change.before is not None:
            await remove_channel_client(change.before)
        if change.kind is ChangeKind.deleted:
            import quota_limits
            from balancer.load_balancer import load_balancer
            from rate_limiter import rate_limiter

            await load_balancer.remove_channel(change.channel_id)
            rate_limiter.remove_key(change.channel_id)
            quota_limits.unblock(change.channel_id)
            return
        outcomes.clear_permanent_for_channel(change.channel_id)

    async def apply_model_group_change(self, change: ModelGroupChange, snapshot: CatalogSnapshot) -> None:
        return None


catalog = ChannelCatalog(path=lambda: config.CHANNELS_FILE, effects=RuntimeCatalogEffects())
