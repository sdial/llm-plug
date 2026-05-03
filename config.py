import asyncio
import json
import os
import re
import tempfile

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
_SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(DATA_DIR, "settings.json"))

_CONFIG_SCHEMA = {
    "host": {"type": "str", "default": "0.0.0.0", "requires_restart": True, "readonly": True, "env": "HOST"},
    "port": {"type": "int", "default": 55555, "requires_restart": True, "readonly": True, "env": "PORT"},
    "request_timeout": {"type": "int", "default": 300, "requires_restart": False, "env": "REQUEST_TIMEOUT"},
    "max_body_size": {"type": "int", "default": 10 * 1024 * 1024, "requires_restart": False, "env": "MAX_BODY_SIZE"},
    "debug": {"type": "bool", "default": False, "requires_restart": True, "env": "DEBUG"},
    "log_level": {"type": "str", "default": "info", "requires_restart": True, "env": "LOG_LEVEL"},
    "database_url": {"type": "str", "default": "", "requires_restart": True, "env": "DATABASE_URL"},
    "max_fail_count": {"type": "int", "default": 5, "requires_restart": False, "env": "MAX_FAIL_COUNT"},
    "cooldown_seconds": {"type": "int", "default": 60, "requires_restart": False, "env": "COOLDOWN_SECONDS"},
}

_settings: dict = {}
_settings_lock = asyncio.Lock()


def _int_env(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid integer for {key}={raw!r}, using default {default}")
        return default


HOST = os.getenv("HOST", "0.0.0.0")
PORT = _int_env("PORT", 55555)

CHANNELS_FILE = os.getenv("CHANNELS_FILE", os.path.join(DATA_DIR, "channels.json"))
API_KEYS_FILE = os.getenv("API_KEYS_FILE", os.path.join(DATA_DIR, "api_keys.json"))

REQUEST_TIMEOUT = _int_env("REQUEST_TIMEOUT", 300)
MAX_BODY_SIZE = _int_env("MAX_BODY_SIZE", 10 * 1024 * 1024)

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
DEBUG_LOG_DIR = os.getenv("DEBUG_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()

DATABASE_URL = os.getenv("DATABASE_URL", "")


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
            env_key = schema.get("env", "")
            env_val = os.getenv(env_key) if env_key else None
            if env_val is not None:
                _settings[key] = _cast_value(env_val, schema["type"])
            else:
                _settings[key] = schema["default"]
    _sync_module_vars()


def _sync_module_vars():
    global HOST, PORT, DEBUG, LOG_LEVEL, REQUEST_TIMEOUT, MAX_BODY_SIZE
    HOST = _settings.get("host", "0.0.0.0")
    PORT = _settings.get("port", 55555)
    DEBUG = _settings.get("debug", False)
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
    if "database_url" in result and result["database_url"]:
        result["database_url"] = _mask_db_url(result["database_url"])
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
    f = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".settings_",
        suffix=".tmp.json",
    )
    tmp_path = f.name
    try:
        json.dump(_settings, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        os.replace(tmp_path, _SETTINGS_FILE)
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


async def init_settings():
    _init_settings_sync()
    await _migrate_lb_config()


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
        f = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dir_name, delete=False,
            prefix=".channels_", suffix=".tmp.json",
        )
        tmp_path = f.name
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(tmp_path, channels_file)
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
            casted = _cast_value(value, schema["type"])
            _settings[key] = casted
            updated_keys.append(key)
            if schema.get("requires_restart"):
                needs_restart = True
        if updated_keys:
            await _save_settings_to_disk()
            _sync_module_vars()
    _apply_lb_settings()
    return {"updated": updated_keys, "needs_restart": needs_restart}
