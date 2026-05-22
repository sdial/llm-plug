import asyncio
import contextlib
import json
import os
import re
import tempfile

from loguru import logger

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

_CONFIG_SCHEMA = {
    "host": {"type": "str", "default": "0.0.0.0", "requires_restart": True, "readonly": True},
    "port": {"type": "int", "default": 55555, "requires_restart": True, "readonly": True},
    "request_timeout": {"type": "int", "default": 300, "requires_restart": False},
    "max_body_size": {"type": "int", "default": 10 * 1024 * 1024, "requires_restart": False},
    "log_level": {"type": "str", "default": "info", "requires_restart": True},
    "stats_sqlite_path": {
        "type": "str",
        "default": os.path.join(DATA_DIR, "stats.db"),
        "requires_restart": False,
    },
    "request_log_db_type": {
        "type": "str",
        "default": "sqlite",
        "requires_restart": False,
    },
    "request_log_sqlite_path": {
        "type": "str",
        "default": os.path.join(DATA_DIR, "request_logs.db"),
        "requires_restart": False,
    },
    "request_log_database_url": {
        "type": "str",
        "default": "",
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
    "max_log_body_size": {
        "type": "int",
        "default": 64 * 1024,
        "requires_restart": False,
    },
    "max_fail_count": {"type": "int", "default": 5, "requires_restart": False},
    "cooldown_seconds": {"type": "int", "default": 60, "requires_restart": False},
    "response_state_max_entries": {"type": "int", "default": 1000, "requires_restart": False},
    "response_state_ttl_minutes": {"type": "int", "default": 60, "requires_restart": False},
    "response_state_cleanup_interval_minutes": {"type": "int", "default": 30, "requires_restart": False},
    "aggregation_timezone": {"type": "str", "default": "", "requires_restart": False},
    "request_log_retention_days": {
        "type": "int",
        "default": 0,
        "requires_restart": False,
    },
    "request_log_raw_retention_days": {
        "type": "int",
        "default": 0,
        "requires_restart": False,
    },
}

_settings: dict = {}
_settings_lock = asyncio.Lock()


HOST = _CONFIG_SCHEMA["host"]["default"]
PORT = _CONFIG_SCHEMA["port"]["default"]

CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
API_KEYS_FILE = os.path.join(DATA_DIR, "api_keys.json")

REQUEST_TIMEOUT = _CONFIG_SCHEMA["request_timeout"]["default"]
MAX_BODY_SIZE = _CONFIG_SCHEMA["max_body_size"]["default"]

LOG_LEVEL = _CONFIG_SCHEMA["log_level"]["default"]

_CONFIG_CONSTRAINTS: dict[str, dict] = {
    "request_timeout": {"min": 1, "max": 3600},
    "max_body_size": {"min": 1024, "max": 1024 * 1024 * 1024},
    "max_log_body_size": {"min": 0, "max": 256 * 1024 * 1024},
    "max_fail_count": {"min": 1, "max": 100000},
    "cooldown_seconds": {"min": 1, "max": 86400},
    "response_state_max_entries": {"min": 1, "max": 10_000_000},
    "response_state_ttl_minutes": {"min": 1, "max": 525600},
    "response_state_cleanup_interval_minutes": {"min": 1, "max": 1440},
    "log_level": {"choices": ("trace", "debug", "info", "warning", "error", "critical")},
    "request_log_db_type": {"choices": ("sqlite", "postgres")},
    "aggregation_timezone": {"validator": "iana_timezone"},
    "request_log_retention_days": {"min": 0},
    "request_log_raw_retention_days": {"min": 0},
}


def _validate_iana_timezone(value: str):
    if not value:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"aggregation_timezone 不是有效的 IANA 时区名: {value!r}") from exc


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
    _settings = {}
    for key, schema in _CONFIG_SCHEMA.items():
        if key in file_data:
            _settings[key] = _cast_value(file_data[key], schema["type"])
        else:
            _settings[key] = schema["default"]
    _sync_module_vars()


def _sync_module_vars():
    global HOST, PORT, LOG_LEVEL, REQUEST_TIMEOUT, MAX_BODY_SIZE
    HOST = _settings.get("host", "0.0.0.0")
    PORT = _settings.get("port", 55555)
    LOG_LEVEL = _settings.get("log_level", "info")
    REQUEST_TIMEOUT = _settings.get("request_timeout", 300)
    MAX_BODY_SIZE = _settings.get("max_body_size", 10 * 1024 * 1024)


def get_setting(key: str):
    if key in _settings:
        return _settings[key]
    schema = _CONFIG_SCHEMA.get(key)
    if schema:
        return schema["default"]
    return None


def _mask_db_url(url: str) -> str:
    return re.sub(r'://([^:]+):([^@]+)@', r'://\1:***@', url)


def get_settings() -> dict:
    result = dict(_settings)
    if result.get("request_log_database_url"):
        result["request_log_database_url"] = _mask_db_url(result["request_log_database_url"])
    result["request_log_database_url_masked"] = result.get("request_log_database_url", "")
    return result


def _apply_lb_settings():
    try:
        from balancer.load_balancer import load_balancer
        load_balancer.update_config(
            max_fail_count=_settings.get("max_fail_count", 5),
            cooldown_seconds=_settings.get("cooldown_seconds", 60),
        )
    except Exception:
        pass


async def _save_settings_to_disk():
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


async def init_settings():
    _init_settings_sync()
    await _migrate_lb_config()
    _apply_lb_settings()


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
            mode="w", encoding="utf-8", dir=dir_name, delete=False,
            prefix=".channels_", suffix=".tmp.json",
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
            _settings[key] = casted
            updated_keys.append(key)
            if schema.get("requires_restart"):
                needs_restart = True
        if updated_keys:
            await _save_settings_to_disk()
            _sync_module_vars()
    _apply_lb_settings()
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
            logger.warning(f"Failed to reload response state store after settings change: {e}")
    return {"updated": updated_keys, "needs_restart": needs_restart}
