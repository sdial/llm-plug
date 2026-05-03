import asyncio
import json
import os
import tempfile
import time
from typing import Any, Callable

import config
from models.model_group import LBConfig, ModelGroup

_channels_lock: asyncio.Lock | None = None
_keys_lock: asyncio.Lock | None = None


def _get_channels_lock() -> asyncio.Lock:
    global _channels_lock
    if _channels_lock is None:
        _channels_lock = asyncio.Lock()
    return _channels_lock


def _get_keys_lock() -> asyncio.Lock:
    global _keys_lock
    if _keys_lock is None:
        _keys_lock = asyncio.Lock()
    return _keys_lock

_cache: dict[str, Any] | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0

_save_callbacks: list[Callable[[], None]] = []


def register_save_callback(callback: Callable[[], None]) -> None:
    _save_callbacks.append(callback)


def _trigger_save_callbacks() -> None:
    for cb in _save_callbacks:
        try:
            cb()
        except Exception:
            pass


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def _read_channels_from_disk() -> dict[str, Any]:
    with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_channels_to_disk(data: dict[str, Any]) -> None:
    dir_name = os.path.dirname(os.path.abspath(config.CHANNELS_FILE)) or "."
    f = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".channels_",
        suffix=".tmp.json",
    )
    tmp_path = f.name
    try:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        os.replace(tmp_path, config.CHANNELS_FILE)
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def load_data() -> dict[str, Any]:
    global _cache, _cache_ts
    _ensure_data_dir()
    async with _get_channels_lock():
        now = time.time()
        if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache
        if not os.path.exists(config.CHANNELS_FILE):
            data = {"channels": []}
            await asyncio.to_thread(_write_channels_to_disk, data)
        else:
            data = await asyncio.to_thread(_read_channels_from_disk)
        _cache = data
        _cache_ts = time.time()
        return data


async def invalidate_cache() -> None:
    global _cache, _cache_ts, _keys_cache, _keys_cache_ts, _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    async with _get_channels_lock():
        _cache = None
        _cache_ts = 0
        _MODEL_GROUPS_CACHE = None
        _MODEL_GROUPS_CACHE_TS = 0
    async with _get_keys_lock():
        _keys_cache = None
        _keys_cache_ts = 0


async def save_data(data: dict[str, Any]) -> None:
    global _cache, _cache_ts
    _ensure_data_dir()
    async with _get_channels_lock():
        await asyncio.to_thread(_write_channels_to_disk, data)
        _cache = data
        _cache_ts = time.time()
        _trigger_save_callbacks()


# ============ API Keys 存储 ============

_keys_cache: dict[str, Any] | None = None
_keys_cache_ts: float = 0


def _read_api_keys_from_disk() -> dict[str, Any]:
    with open(config.API_KEYS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_api_keys_to_disk(data: dict[str, Any]) -> None:
    dir_name = os.path.dirname(os.path.abspath(config.API_KEYS_FILE)) or "."
    f = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".api_keys_",
        suffix=".tmp.json",
    )
    tmp_path = f.name
    try:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        os.replace(tmp_path, config.API_KEYS_FILE)
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def load_api_keys() -> dict[str, Any]:
    global _keys_cache, _keys_cache_ts
    _ensure_data_dir()
    async with _get_keys_lock():
        now = time.time()
        if _keys_cache is not None and (now - _keys_cache_ts) < _CACHE_TTL:
            return _keys_cache
        if not os.path.exists(config.API_KEYS_FILE):
            data = {"api_keys": []}
            await asyncio.to_thread(_write_api_keys_to_disk, data)
        else:
            data = await asyncio.to_thread(_read_api_keys_from_disk)
        _keys_cache = data
        _keys_cache_ts = time.time()
        return data


async def save_api_keys(data: dict[str, Any]) -> None:
    global _keys_cache, _keys_cache_ts
    _ensure_data_dir()
    async with _get_keys_lock():
        await asyncio.to_thread(_write_api_keys_to_disk, data)
        _keys_cache = data
        _keys_cache_ts = time.time()


async def invalidate_keys_cache() -> None:
    global _keys_cache, _keys_cache_ts
    async with _get_keys_lock():
        _keys_cache = None
        _keys_cache_ts = 0


# ============ 负载均衡配置（兼容接口，代理到 config settings） ============

async def get_lb_config() -> LBConfig:
    """兼容接口：从 config settings 读取 lb 配置"""
    import config as _config
    return LBConfig(
        max_fail_count=_config.get_setting("max_fail_count"),
        cooldown_seconds=_config.get_setting("cooldown_seconds"),
    )


async def save_lb_config(cfg: LBConfig) -> None:
    """兼容接口：写入 config settings"""
    import config as _config
    await _config.update_settings({
        "max_fail_count": cfg.max_fail_count,
        "cooldown_seconds": cfg.cooldown_seconds,
    })


# ============ 模型组存储 ============

_MODEL_GROUPS_CACHE: list[ModelGroup] | None = None
_MODEL_GROUPS_CACHE_TS: float = 0


async def load_model_groups() -> list[ModelGroup]:
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    now = time.time()
    if _MODEL_GROUPS_CACHE is not None and (now - _MODEL_GROUPS_CACHE_TS) < _CACHE_TTL:
        return _MODEL_GROUPS_CACHE

    data = await load_data()
    groups = [ModelGroup(**g) for g in data.get("model_groups", [])]
    _MODEL_GROUPS_CACHE = groups
    _MODEL_GROUPS_CACHE_TS = now
    return groups


async def get_model_group_by_name(name: str) -> ModelGroup | None:
    groups = await load_model_groups()
    for g in groups:
        if g.name == name and g.enabled:
            return g
    return None


async def save_model_groups(groups: list[ModelGroup]) -> None:
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    data = await load_data()
    data["model_groups"] = [g.model_dump() for g in groups]
    await save_data(data)
    _MODEL_GROUPS_CACHE = groups
    _MODEL_GROUPS_CACHE_TS = time.time()


async def add_model_group(group: ModelGroup) -> ModelGroup:
    groups = await load_model_groups()
    groups.append(group)
    await save_model_groups(groups)
    return group


async def update_model_group(group_id: str, updates: dict) -> ModelGroup | None:
    groups = await load_model_groups()
    for i, g in enumerate(groups):
        if g.id == group_id:
            updated = g.model_copy(update=updates)
            groups[i] = updated
            await save_model_groups(groups)
            return updated
    return None


async def delete_model_group(group_id: str) -> bool:
    groups = await load_model_groups()
    new_groups = [g for g in groups if g.id != group_id]
    if len(new_groups) == len(groups):
        return False
    await save_model_groups(new_groups)
    return True


async def invalidate_model_groups_cache() -> None:
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    async with _get_channels_lock():
        _MODEL_GROUPS_CACHE = None
        _MODEL_GROUPS_CACHE_TS = 0


# 注册缓存失效回调
register_save_callback(lambda: asyncio.create_task(_async_invalidate_model_groups_cache()))


async def _async_invalidate_model_groups_cache() -> None:
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    _MODEL_GROUPS_CACHE = None
    _MODEL_GROUPS_CACHE_TS = 0
