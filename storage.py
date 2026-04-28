import json
import os
import tempfile
import threading
import time
from typing import Any

import config

_lock = threading.RLock()


def get_lock() -> threading.Lock:
    return _lock

_cache: dict[str, Any] | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)


def _read_from_disk() -> dict[str, Any]:
    with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_data() -> dict[str, Any]:
    global _cache, _cache_ts
    _ensure_data_dir()
    with _lock:
        # 双重检查：在锁内再次检查缓存，避免多线程重复读磁盘
        now = time.time()
        if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache
        if not os.path.exists(config.CHANNELS_FILE):
            with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
                json.dump({"channels": []}, f, ensure_ascii=False, indent=2)
        data = _read_from_disk()
        _cache = data
        _cache_ts = time.time()
        return data


def invalidate_cache() -> None:
    global _cache, _cache_ts
    with _lock:
        _cache = None
        _cache_ts = 0


def save_data(data: dict[str, Any]) -> None:
    global _cache, _cache_ts
    _ensure_data_dir()
    dir_name = os.path.dirname(os.path.abspath(config.CHANNELS_FILE)) or "."
    with _lock:
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
            _cache = data
            _cache_ts = time.time()
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
