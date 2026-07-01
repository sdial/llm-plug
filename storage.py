import asyncio
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from typing import Any

import config
from models.model_group import LBConfig, ModelGroup
from pydantic import ValidationError

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
_cache_file_sig: tuple[int, int] | None = None
_CACHE_TTL = 5.0

_save_callbacks: list[Callable[[], None]] = []
_api_keys_save_callbacks: list[Callable[[], None]] = []


def register_save_callback(callback: Callable[[], None]) -> None:
    _save_callbacks.append(callback)


def register_api_keys_save_callback(callback: Callable[[], None]) -> None:
    _api_keys_save_callbacks.append(callback)


def _trigger_save_callbacks() -> None:
    for cb in _save_callbacks:
        with contextlib.suppress(Exception):
            cb()


def _trigger_api_keys_save_callbacks() -> None:
    for cb in _api_keys_save_callbacks:
        with contextlib.suppress(Exception):
            cb()


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def _read_channels_from_disk() -> dict[str, Any]:
    try:
        with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        from loguru import logger

        logger.warning(f"channels file is not valid JSON, using empty channel list: {exc}")
        return {"channels": []}


def _file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _write_channels_to_disk(data: dict[str, Any]) -> None:
    dir_name = os.path.dirname(os.path.abspath(config.CHANNELS_FILE)) or "."
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".channels_",
        suffix=".tmp.json",
    ) as f:
        tmp_path = f.name
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config.CHANNELS_FILE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def load_data() -> dict[str, Any]:
    global _cache, _cache_ts, _cache_file_sig
    _ensure_data_dir()
    async with _get_channels_lock():
        now = time.time()
        current_file_sig = _file_signature(config.CHANNELS_FILE)
        if (
            _cache is not None
            and (now - _cache_ts) < _CACHE_TTL
            and current_file_sig == _cache_file_sig
        ):
            return _cache
        if not os.path.exists(config.CHANNELS_FILE):
            data = {"channels": []}
            await asyncio.to_thread(_write_channels_to_disk, data)
        else:
            data = await asyncio.to_thread(_read_channels_from_disk)
        _cache = data
        _cache_ts = time.time()
        _cache_file_sig = _file_signature(config.CHANNELS_FILE)
        return data


async def atomic_update_data(mutator: Callable[[dict[str, Any]], Any]):
    """在 channels 锁内完成 read-modify-write，消除 lost-update 竞态。

    mutator 接收当前 data 字典（已是最新磁盘状态），可原地修改或返回新字典。
    支持同步或异步 mutator。返回 mutator 的返回值。
    """
    global _cache, _cache_ts, _cache_file_sig
    _ensure_data_dir()
    async with _get_channels_lock():
        if not os.path.exists(config.CHANNELS_FILE):
            data: dict[str, Any] = {"channels": []}
        else:
            data = await asyncio.to_thread(_read_channels_from_disk)
        result = mutator(data)
        if asyncio.iscoroutine(result):
            result = await result
        new_data = result if isinstance(result, dict) else data
        await asyncio.to_thread(_write_channels_to_disk, new_data)
        _cache = new_data
        _cache_ts = time.time()
        _cache_file_sig = _file_signature(config.CHANNELS_FILE)
        _trigger_save_callbacks()
        return result


async def invalidate_cache() -> None:
    global _cache, _cache_ts, _cache_file_sig, _keys_cache, _keys_cache_ts, _keys_cache_file_sig, _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    async with _get_channels_lock():
        _cache = None
        _cache_ts = 0
        _cache_file_sig = None
        _MODEL_GROUPS_CACHE = None
        _MODEL_GROUPS_CACHE_TS = 0
    async with _get_keys_lock():
        _keys_cache = None
        _keys_cache_ts = 0
        _keys_cache_file_sig = None


async def save_data(data: dict[str, Any]) -> None:
    global _cache, _cache_ts, _cache_file_sig
    _ensure_data_dir()
    async with _get_channels_lock():
        await asyncio.to_thread(_write_channels_to_disk, data)
        _cache = data
        _cache_ts = time.time()
        _cache_file_sig = _file_signature(config.CHANNELS_FILE)
        _trigger_save_callbacks()


# ============ API Keys 存储 ============

_keys_cache: dict[str, Any] | None = None
_keys_cache_ts: float = 0
_keys_cache_file_sig: tuple[int, int] | None = None


def _read_api_keys_from_disk() -> dict[str, Any]:
    try:
        with open(config.API_KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        from loguru import logger

        logger.warning(f"api keys file is not valid JSON, using empty api key list: {exc}")
        return {"api_keys": []}


def _write_api_keys_to_disk(data: dict[str, Any]) -> None:
    dir_name = os.path.dirname(os.path.abspath(config.API_KEYS_FILE)) or "."
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".api_keys_",
        suffix=".tmp.json",
    ) as f:
        tmp_path = f.name
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config.API_KEYS_FILE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def load_api_keys() -> dict[str, Any]:
    global _keys_cache, _keys_cache_ts, _keys_cache_file_sig
    _ensure_data_dir()
    async with _get_keys_lock():
        now = time.time()
        current_file_sig = _file_signature(config.API_KEYS_FILE)
        if (
            _keys_cache is not None
            and (now - _keys_cache_ts) < _CACHE_TTL
            and current_file_sig == _keys_cache_file_sig
        ):
            return _keys_cache
        if not os.path.exists(config.API_KEYS_FILE):
            data = {"api_keys": []}
            await asyncio.to_thread(_write_api_keys_to_disk, data)
        else:
            data = await asyncio.to_thread(_read_api_keys_from_disk)
        _keys_cache = data
        _keys_cache_ts = time.time()
        _keys_cache_file_sig = _file_signature(config.API_KEYS_FILE)
        return data


async def save_api_keys(data: dict[str, Any]) -> None:
    global _keys_cache, _keys_cache_ts, _keys_cache_file_sig
    _ensure_data_dir()
    async with _get_keys_lock():
        await asyncio.to_thread(_write_api_keys_to_disk, data)
        _keys_cache = data
        _keys_cache_ts = time.time()
        _keys_cache_file_sig = _file_signature(config.API_KEYS_FILE)
        _trigger_api_keys_save_callbacks()


async def atomic_update_api_keys(mutator: Callable[[dict[str, Any]], Any]):
    """API Keys 版本的原子 read-modify-write。"""
    global _keys_cache, _keys_cache_ts, _keys_cache_file_sig
    _ensure_data_dir()
    async with _get_keys_lock():
        if not os.path.exists(config.API_KEYS_FILE):
            data: dict[str, Any] = {"api_keys": []}
        else:
            data = await asyncio.to_thread(_read_api_keys_from_disk)
        result = mutator(data)
        if asyncio.iscoroutine(result):
            result = await result
        new_data = result if isinstance(result, dict) else data
        await asyncio.to_thread(_write_api_keys_to_disk, new_data)
        _keys_cache = new_data
        _keys_cache_ts = time.time()
        _keys_cache_file_sig = _file_signature(config.API_KEYS_FILE)
        _trigger_api_keys_save_callbacks()
        return result


async def invalidate_keys_cache() -> None:
    global _keys_cache, _keys_cache_ts, _keys_cache_file_sig
    async with _get_keys_lock():
        _keys_cache = None
        _keys_cache_ts = 0
        _keys_cache_file_sig = None


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
_MODEL_GROUPS_CACHE_VERSION: int = 0
_model_groups_lock: asyncio.Lock | None = None


def _get_model_groups_lock() -> asyncio.Lock:
    global _model_groups_lock
    if _model_groups_lock is None:
        _model_groups_lock = asyncio.Lock()
    return _model_groups_lock


def _parse_model_groups(raw_groups: list[Any]) -> list[ModelGroup]:
    from loguru import logger

    groups: list[ModelGroup] = []
    for idx, raw_group in enumerate(raw_groups):
        try:
            groups.append(ModelGroup(**raw_group))
        except (TypeError, ValidationError) as exc:
            group_id = raw_group.get("id") if isinstance(raw_group, dict) else None
            logger.warning(
                f"skip invalid model group entry index={idx} id={group_id}: {exc}"
            )
    return groups


async def load_model_groups() -> list[ModelGroup]:
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    async with _get_model_groups_lock():
        while True:
            now = time.time()
            if _MODEL_GROUPS_CACHE is not None and (now - _MODEL_GROUPS_CACHE_TS) < _CACHE_TTL:
                return _MODEL_GROUPS_CACHE

            version = _MODEL_GROUPS_CACHE_VERSION
            data = await load_data()
            groups = _parse_model_groups(data.get("model_groups", []))

            if version != _MODEL_GROUPS_CACHE_VERSION:
                continue

            _MODEL_GROUPS_CACHE = groups
            _MODEL_GROUPS_CACHE_TS = time.time()
            return groups


async def get_model_group_by_name(name: str) -> ModelGroup | None:
    groups = await load_model_groups()
    for g in groups:
        if g.name == name and g.enabled:
            return g
    return None


async def save_model_groups(groups: list[ModelGroup]) -> None:
    serialized = [g.model_dump() for g in groups]
    await atomic_update_data(lambda data: data.__setitem__("model_groups", serialized))


async def add_model_group(group: ModelGroup) -> ModelGroup:
    def mutator(data):
        groups = _parse_model_groups(data.get("model_groups", []))
        groups.append(group)
        data["model_groups"] = [g.model_dump() for g in groups]

    await atomic_update_data(mutator)
    return group


async def update_model_group(group_id: str, updates: dict) -> ModelGroup | None:
    result: ModelGroup | None = None

    def mutator(data):
        nonlocal result
        groups = _parse_model_groups(data.get("model_groups", []))
        for i, g in enumerate(groups):
            if g.id == group_id:
                updated = g.model_copy(update=updates)
                groups[i] = updated
                result = updated
                break
        data["model_groups"] = [g.model_dump() for g in groups]

    await atomic_update_data(mutator)
    return result


async def delete_model_group(group_id: str) -> bool:
    found = False

    def mutator(data):
        nonlocal found
        groups = _parse_model_groups(data.get("model_groups", []))
        new_groups = [g for g in groups if g.id != group_id]
        found = len(new_groups) < len(groups)
        data["model_groups"] = [g.model_dump() for g in new_groups]

    await atomic_update_data(mutator)
    return found


async def invalidate_model_groups_cache() -> None:
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS, _MODEL_GROUPS_CACHE_VERSION
    async with _get_model_groups_lock():
        _MODEL_GROUPS_CACHE = None
        _MODEL_GROUPS_CACHE_TS = 0
        _MODEL_GROUPS_CACHE_VERSION += 1


def _invalidate_model_groups_cache_sync() -> None:
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS, _MODEL_GROUPS_CACHE_VERSION
    _MODEL_GROUPS_CACHE = None
    _MODEL_GROUPS_CACHE_TS = 0
    _MODEL_GROUPS_CACHE_VERSION += 1


# 注册缓存失效回调
register_save_callback(_invalidate_model_groups_cache_sync)
