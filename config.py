import asyncio
import contextlib
import json
import os
import tempfile
from typing import Literal, TypedDict

from loguru import logger

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

ConfigValueType = Literal["str", "int", "bool"]
ConfigValue = str | int | bool


class _ConfigSchemaRequired(TypedDict):
    type: ConfigValueType
    default: ConfigValue
    requires_restart: bool


class ConfigSchemaEntry(_ConfigSchemaRequired, total=False):
    readonly: bool


_CONFIG_SCHEMA: dict[str, ConfigSchemaEntry] = {
    "host": {
        "type": "str",
        "default": "0.0.0.0",
        "requires_restart": True,
        "readonly": True,
    },
    "port": {
        "type": "int",
        "default": 55555,
        "requires_restart": True,
        "readonly": True,
    },
    "request_timeout": {"type": "int", "default": 300, "requires_restart": False},
    "max_body_size": {
        "type": "int",
        "default": 10 * 1024 * 1024,
        "requires_restart": False,
    },
    "stats_sqlite_path": {
        "type": "str",
        "default": os.path.join(DATA_DIR, "stats.db"),
        "requires_restart": False,
    },
    "request_log_sqlite_path": {
        "type": "str",
        "default": os.path.join(DATA_DIR, "request_logs.db"),
        "requires_restart": False,
    },
    "save_request_headers": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "save_response_headers": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "save_request_body": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "save_response_body": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "save_files": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "save_images": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "save_audios": {
        "type": "bool",
        "default": False,
        "requires_restart": False,
    },
    "max_log_body_size": {
        "type": "int",
        "default": 64 * 1024,
        "requires_restart": False,
    },
    "max_stream_chunks": {
        "type": "int",
        "default": 10000,
        "requires_restart": False,
    },
    "allow_format_conversion": {
        "type": "bool",
        "default": True,
        "requires_restart": False,
    },
    "max_fail_count": {"type": "int", "default": 5, "requires_restart": False},
    "cooldown_seconds": {"type": "int", "default": 60, "requires_restart": False},
    "lb_strategy": {"type": "str", "default": "round_robin", "requires_restart": False},
    "sticky_ttl": {"type": "int", "default": 1800, "requires_restart": False},
    "sticky_cache_max_entries": {
        "type": "int",
        "default": 10000,
        "requires_restart": False,
    },
    "response_state_max_entries": {
        "type": "int",
        "default": 1000,
        "requires_restart": False,
    },
    "response_state_ttl_minutes": {
        "type": "int",
        "default": 60,
        "requires_restart": False,
    },
    "response_state_cleanup_interval_minutes": {
        "type": "int",
        "default": 30,
        "requires_restart": False,
    },
    "aggregation_timezone": {"type": "str", "default": "", "requires_restart": False},
    "request_log_retention_days": {
        "type": "int",
        "default": 7,
        "requires_restart": False,
    },
    "request_log_raw_retention_days": {
        "type": "int",
        "default": 1,
        "requires_restart": False,
    },
    "admin_max_attempts": {
        "type": "int",
        "default": 10,
        "requires_restart": False,
    },
    "admin_lockout_base_seconds": {
        "type": "int",
        "default": 60,
        "requires_restart": False,
    },
}

_settings: dict = {}
_settings_lock = asyncio.Lock()


HOST = _CONFIG_SCHEMA["host"]["default"]
PORT = _CONFIG_SCHEMA["port"]["default"]

CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
API_KEYS_FILE = os.path.join(DATA_DIR, "api_keys.json")
ADMIN_AUTH_FILE = os.path.join(DATA_DIR, "admin_auth.json")

REQUEST_TIMEOUT = _CONFIG_SCHEMA["request_timeout"]["default"]
MAX_BODY_SIZE = _CONFIG_SCHEMA["max_body_size"]["default"]

LOG_LEVEL = "info"  # 仅通过 --log-level CLI 参数设置

_CONFIG_CONSTRAINTS: dict[str, dict] = {
    "request_timeout": {"min": 1, "max": 3600},
    "max_body_size": {"min": 1024, "max": 1024 * 1024 * 1024},
    "max_log_body_size": {"min": 0, "max": 256 * 1024 * 1024},
    "max_stream_chunks": {"min": 100, "max": 100000},
    "max_fail_count": {"min": 1, "max": 100000},
    "cooldown_seconds": {"min": 1, "max": 86400},
    "lb_strategy": {"choices": ("round_robin", "backup", "sticky")},
    "sticky_ttl": {"min": 60, "max": 86400},
    "sticky_cache_max_entries": {"min": 100, "max": 1000000},
    "response_state_max_entries": {"min": 1, "max": 10_000_000},
    "response_state_ttl_minutes": {"min": 1, "max": 525600},
    "response_state_cleanup_interval_minutes": {"min": 1, "max": 1440},
    "aggregation_timezone": {"validator": "iana_timezone"},
    "request_log_retention_days": {"min": 0},
    "request_log_raw_retention_days": {"min": 0},
    "admin_max_attempts": {"min": 1, "max": 100},
    "admin_lockout_base_seconds": {"min": 10, "max": 86400},
}


def _validate_iana_timezone(value: str):
    if not value:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"aggregation_timezone 不是有效的 IANA 时区名: {value!r}"
        ) from exc


def _validate_setting(key: str, value):
    constraints = _CONFIG_CONSTRAINTS.get(key)
    if not constraints:
        return
    if "min" in constraints and value < constraints["min"]:
        raise ValueError(f"{key} must be >= {constraints['min']}, got {value}")
    if "max" in constraints and value > constraints["max"]:
        raise ValueError(f"{key} must be <= {constraints['max']}, got {value}")
    if "choices" in constraints and str(value).lower() not in constraints["choices"]:
        raise ValueError(
            f"{key} must be one of {constraints['choices']}, got {value!r}"
        )
    validator = constraints.get("validator")
    if validator == "iana_timezone":
        _validate_iana_timezone(value)


def _cast_value(value, type_name):
    if type_name == "int":
        return int(value)
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    return str(value)


def _init_settings_sync():
    global _settings
    file_data = {}
    if os.path.exists(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"Failed to read {_SETTINGS_FILE}, using defaults")
    legacy_existing_file_defaults = {}
    if os.path.exists(_SETTINGS_FILE):
        legacy_existing_file_defaults = {
            "request_log_retention_days": 0,
            "request_log_raw_retention_days": 0,
        }
    _settings = {}
    for key, schema in _CONFIG_SCHEMA.items():
        if key in file_data:
            _settings[key] = _cast_value(file_data[key], schema["type"])
        else:
            _settings[key] = legacy_existing_file_defaults.get(key, schema["default"])
    _sync_module_vars()


def _sync_module_vars():
    global HOST, PORT, REQUEST_TIMEOUT, MAX_BODY_SIZE
    HOST = _settings.get("host", "0.0.0.0")
    PORT = _settings.get("port", 55555)
    REQUEST_TIMEOUT = _settings.get("request_timeout", 300)
    MAX_BODY_SIZE = _settings.get("max_body_size", 10 * 1024 * 1024)


def get_setting(key: str):
    if key in _settings:
        return _settings[key]
    schema = _CONFIG_SCHEMA.get(key)
    if schema:
        return schema["default"]
    return None


def get_settings() -> dict:
    return dict(_settings)


async def _apply_lb_settings():
    try:
        from balancer.load_balancer import load_balancer

        await load_balancer.update_config(
            max_fail_count=_settings.get("max_fail_count", 5),
            cooldown_seconds=_settings.get("cooldown_seconds", 60),
            strategy=_settings.get("lb_strategy", "round_robin"),
            sticky_ttl=_settings.get("sticky_ttl", 1800),
            sticky_cache_max_entries=_settings.get("sticky_cache_max_entries", 10000),
        )
    except Exception:
        logger.warning(
            "Failed to apply LB settings, load balancer will use previous config",
            exc_info=True,
        )


def _save_settings_to_disk_sync():
    """同步写入 settings.json（原子写）"""
    dir_name = os.path.dirname(os.path.abspath(_SETTINGS_FILE)) or "."
    os.makedirs(dir_name, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".settings_",
        suffix=".tmp.json",
    ) as f:
        tmp_path = f.name
        json.dump(_settings, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp_path, _SETTINGS_FILE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def _save_settings_to_disk():
    await asyncio.to_thread(_save_settings_to_disk_sync)


async def init_settings():
    _init_settings_sync()
    await _migrate_lb_config()
    await _apply_lb_settings()


def _migrate_lb_config_sync(channels_file: str):
    """从 channels.json 的 lb_config 迁移到 settings.json"""
    global _settings
    if not os.path.exists(channels_file):
        return
    try:
        with open(channels_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    lb_config = data.get("lb_config")
    if not lb_config:
        return

    if "max_fail_count" in lb_config and _settings.get("max_fail_count", 5) == 5:
        _settings["max_fail_count"] = lb_config["max_fail_count"]
    if "cooldown_seconds" in lb_config and _settings.get("cooldown_seconds", 60) == 60:
        _settings["cooldown_seconds"] = lb_config["cooldown_seconds"]

    if "lb_config" in data:
        del data["lb_config"]
        dir_name = os.path.dirname(os.path.abspath(channels_file)) or "."
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
            os.replace(tmp_path, channels_file)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


async def _migrate_lb_config():
    """异步迁移（从 CHANNELS_FILE 读取）"""
    _migrate_lb_config_sync(CHANNELS_FILE)


async def update_settings(updates: dict) -> dict:
    global _settings
    updated_keys = []
    needs_restart = False
    staged: dict[str, object] = {}
    async with _settings_lock:
        for key, value in updates.items():
            schema = _CONFIG_SCHEMA.get(key)
            if schema is None:
                continue
            if schema.get("readonly"):
                continue
            try:
                casted = _cast_value(value, schema["type"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} type cast failed: {exc}") from exc
            _validate_setting(key, casted)
            staged[key] = casted
            updated_keys.append(key)
            if schema.get("requires_restart"):
                needs_restart = True
        _settings.update(staged)
        if updated_keys:
            await _save_settings_to_disk()
            _sync_module_vars()
    await _apply_lb_settings()
    # 如果 request_timeout 变更，清理客户端缓存以应用新超时
    if "request_timeout" in updated_keys:
        try:
            from client import invalidate_all_clients

            await invalidate_all_clients()
        except Exception as e:
            logger.warning(f"Failed to invalidate clients after timeout change: {e}")
    if any(key.startswith("response_state_") for key in updated_keys):
        try:
            from response_state import reload_responses_store

            reload_responses_store()
        except Exception as e:
            logger.warning(
                f"Failed to reload response state store after settings change: {e}"
            )
    return {"updated": updated_keys, "needs_restart": needs_restart}
