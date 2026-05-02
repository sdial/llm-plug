import json
import os
import tempfile
import threading
import time
from typing import Any, Callable

import config
from models.model_group import LBConfig, ModelGroup

_lock = threading.RLock()


def get_lock() -> threading.RLock:
    return _lock

_cache: dict[str, Any] | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0

# 外部模块可注册缓存失效回调（避免循环导入）
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
    global _cache, _cache_ts, _keys_cache, _keys_cache_ts, _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS, _LB_CONFIG_CACHE, _LB_CONFIG_CACHE_TS
    with _lock:
        _cache = None
        _cache_ts = 0
        _keys_cache = None
        _keys_cache_ts = 0
        _MODEL_GROUPS_CACHE = None
        _MODEL_GROUPS_CACHE_TS = 0
        _LB_CONFIG_CACHE = None
        _LB_CONFIG_CACHE_TS = 0


def save_data(data: dict[str, Any]) -> None:
    """保存渠道数据"""
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
            _trigger_save_callbacks()
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


# ============ API Keys 存储 ============

_keys_cache: dict[str, Any] | None = None
_keys_cache_ts: float = 0


def load_api_keys() -> dict[str, Any]:
    global _keys_cache, _keys_cache_ts
    _ensure_data_dir()
    with _lock:
        now = time.time()
        if _keys_cache is not None and (now - _keys_cache_ts) < _CACHE_TTL:
            return _keys_cache
        if not os.path.exists(config.API_KEYS_FILE):
            with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
                json.dump({"api_keys": []}, f, ensure_ascii=False, indent=2)
        with open(config.API_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _keys_cache = data
        _keys_cache_ts = time.time()
        return data


def save_api_keys(data: dict[str, Any]) -> None:
    global _keys_cache, _keys_cache_ts
    _ensure_data_dir()
    dir_name = os.path.dirname(os.path.abspath(config.API_KEYS_FILE)) or "."
    with _lock:
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
            _keys_cache = data
            _keys_cache_ts = time.time()
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


def invalidate_keys_cache() -> None:
    global _keys_cache, _keys_cache_ts
    with _lock:
        _keys_cache = None
        _keys_cache_ts = 0


# ============ 负载均衡配置 ============

_LB_CONFIG_CACHE: LBConfig | None = None
_LB_CONFIG_CACHE_TS: float = 0


def get_lb_config() -> LBConfig:
    """获取负载均衡全局配置"""
    global _LB_CONFIG_CACHE, _LB_CONFIG_CACHE_TS
    now = time.time()
    if _LB_CONFIG_CACHE is not None and (now - _LB_CONFIG_CACHE_TS) < _CACHE_TTL:
        return _LB_CONFIG_CACHE

    data = load_data()
    lb_dict = data.get("lb_config", {})
    config = LBConfig(**lb_dict)
    _LB_CONFIG_CACHE = config
    _LB_CONFIG_CACHE_TS = now
    return config


def save_lb_config(config: LBConfig) -> None:
    """保存负载均衡全局配置"""
    global _LB_CONFIG_CACHE, _LB_CONFIG_CACHE_TS
    data = load_data()
    data["lb_config"] = config.model_dump()
    save_data(data)
    _LB_CONFIG_CACHE = config
    _LB_CONFIG_CACHE_TS = time.time()


# ============ 模型组存储 ============

_MODEL_GROUPS_CACHE: list[ModelGroup] | None = None
_MODEL_GROUPS_CACHE_TS: float = 0


def load_model_groups() -> list[ModelGroup]:
    """获取所有模型组"""
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    now = time.time()
    if _MODEL_GROUPS_CACHE is not None and (now - _MODEL_GROUPS_CACHE_TS) < _CACHE_TTL:
        return _MODEL_GROUPS_CACHE

    data = load_data()
    groups = [ModelGroup(**g) for g in data.get("model_groups", [])]
    _MODEL_GROUPS_CACHE = groups
    _MODEL_GROUPS_CACHE_TS = now
    return groups


def get_model_group_by_name(name: str) -> ModelGroup | None:
    """根据名称获取模型组"""
    groups = load_model_groups()
    for g in groups:
        if g.name == name and g.enabled:
            return g
    return None


def save_model_groups(groups: list[ModelGroup]) -> None:
    """保存所有模型组"""
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    data = load_data()
    data["model_groups"] = [g.model_dump() for g in groups]
    save_data(data)
    _MODEL_GROUPS_CACHE = groups
    _MODEL_GROUPS_CACHE_TS = time.time()


def add_model_group(group: ModelGroup) -> ModelGroup:
    """添加模型组"""
    groups = load_model_groups()
    groups.append(group)
    save_model_groups(groups)
    return group


def update_model_group(group_id: str, updates: dict) -> ModelGroup | None:
    """更新模型组"""
    groups = load_model_groups()
    for i, g in enumerate(groups):
        if g.id == group_id:
            updated = g.model_copy(update=updates)
            groups[i] = updated
            save_model_groups(groups)
            return updated
    return None


def delete_model_group(group_id: str) -> bool:
    """删除模型组"""
    groups = load_model_groups()
    new_groups = [g for g in groups if g.id != group_id]
    if len(new_groups) == len(groups):
        return False
    save_model_groups(new_groups)
    return True


def invalidate_model_groups_cache() -> None:
    """使模型组缓存失效"""
    global _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS
    with _lock:
        _MODEL_GROUPS_CACHE = None
        _MODEL_GROUPS_CACHE_TS = 0


# 注册缓存失效回调
register_save_callback(invalidate_model_groups_cache)
