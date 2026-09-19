"""Access Key 持久化与旧 LB 配置兼容入口。

Channel 与 Model Group 已由 :mod:`channel_catalog` 拥有；本模块不再暴露其
原始 JSON、mutator 或保存回调。
"""

import asyncio
import contextlib
import json
import os
import shutil
import time
from collections.abc import Callable
from typing import Any

import config
from atomic_json import write_json_atomic
from models.model_group import LBConfig

_keys_lock: asyncio.Lock | None = None
_keys_cache: dict[str, Any] | None = None
_keys_cache_ts: float = 0
_keys_cache_file_sig: tuple[int, int] | None = None
_CACHE_TTL = 5.0
_api_keys_save_callbacks: list[Callable[[], None]] = []


def _get_keys_lock() -> asyncio.Lock:
    global _keys_lock
    if _keys_lock is None:
        _keys_lock = asyncio.Lock()
    return _keys_lock


def register_api_keys_save_callback(callback: Callable[[], None]) -> None:
    _api_keys_save_callbacks.append(callback)


def _trigger_api_keys_save_callbacks() -> None:
    for callback in _api_keys_save_callbacks:
        with contextlib.suppress(Exception):
            callback()


class StorageCorruptionError(RuntimeError):
    """持久化 JSON 已损坏；原文件保持不变并已备份。"""

    def __init__(self, path: str, backup_path: str, original: Exception):
        super().__init__(f"{path} is not valid JSON; preserved original and backed up to {backup_path}")
        self.path = path
        self.backup_path = backup_path
        self.original = original


def _ensure_data_dir() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)


def _backup_corrupt_json_file(path: str) -> str:
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    backup_path = f"{path}.corrupt-{timestamp}-{time.time_ns()}"
    shutil.copy2(path, backup_path)
    return backup_path


def _file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    return stat_result.st_mtime_ns, stat_result.st_size


def _read_api_keys_from_disk() -> dict[str, Any]:
    try:
        with open(config.API_KEYS_FILE, encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError as exc:
        from loguru import logger

        backup_path = _backup_corrupt_json_file(config.API_KEYS_FILE)
        logger.error(f"api keys file is not valid JSON, refusing to overwrite it; backup saved to {backup_path}: {exc}")
        raise StorageCorruptionError(config.API_KEYS_FILE, backup_path, exc) from exc


def _write_api_keys_to_disk(data: dict[str, Any]) -> None:
    write_json_atomic(config.API_KEYS_FILE, data, temp_prefix=".api_keys_")


async def load_api_keys() -> dict[str, Any]:
    global _keys_cache, _keys_cache_ts, _keys_cache_file_sig
    _ensure_data_dir()
    async with _get_keys_lock():
        now = time.time()
        current_file_sig = _file_signature(config.API_KEYS_FILE)
        if _keys_cache is not None and now - _keys_cache_ts < _CACHE_TTL and current_file_sig == _keys_cache_file_sig:
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
    """在 Access Key 锁内完成 read-modify-write。"""
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


async def get_lb_config() -> LBConfig:
    """从 settings 读取旧管理端仍使用的 LB 配置形态。"""
    return LBConfig(
        max_fail_count=config.get_setting("max_fail_count"),
        cooldown_seconds=config.get_setting("cooldown_seconds"),
    )


async def save_lb_config(cfg: LBConfig) -> None:
    """把旧管理端 LB 配置形态写入 settings。"""
    await config.update_settings(
        {
            "max_fail_count": cfg.max_fail_count,
            "cooldown_seconds": cfg.cooldown_seconds,
        }
    )
