import os

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

import json

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
_SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(DATA_DIR, "settings.json"))

_CONFIG_SCHEMA = {
    "host": {"type": "str", "default": "0.0.0.0", "requires_restart": True, "readonly": True, "env": "HOST"},
    "port": {"type": "int", "default": 55555, "requires_restart": True, "readonly": True, "env": "PORT"},
    "request_timeout": {"type": "int", "default": 300, "requires_restart": False, "env": "REQUEST_TIMEOUT"},
    "max_body_size": {"type": "int", "default": 10 * 1024 * 1024, "requires_restart": False, "env": "MAX_BODY_SIZE"},
    "debug": {"type": "bool", "default": False, "requires_restart": True, "env": "DEBUG"},
    "log_level": {"type": "str", "default": "info", "requires_restart": True, "env": "LOG_LEVEL"},
    "stats_tracked_headers": {"type": "str", "default": "", "requires_restart": False, "env": "STATS_TRACKED_HEADERS"},
    "database_url": {"type": "str", "default": "", "requires_restart": True, "env": "DATABASE_URL"},
    "max_fail_count": {"type": "int", "default": 5, "requires_restart": False, "env": "MAX_FAIL_COUNT"},
    "cooldown_seconds": {"type": "int", "default": 60, "requires_restart": False, "env": "COOLDOWN_SECONDS"},
}


def _int_env(key: str, default: int) -> int:
    """读取整数环境变量，格式错误时回退到默认值。"""
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

_stats_tracked_headers_raw = os.getenv("STATS_TRACKED_HEADERS", "")
TRACK_ALL_HEADERS = _stats_tracked_headers_raw.strip().upper() == "ALL" or not _stats_tracked_headers_raw.strip()
STATS_TRACKED_HEADERS = None if TRACK_ALL_HEADERS else _stats_tracked_headers_raw.split(",")
