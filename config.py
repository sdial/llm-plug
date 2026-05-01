import os

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


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


# 服务器配置
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _int_env("PORT", 55555)

# 数据存储
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
CHANNELS_FILE = os.getenv("CHANNELS_FILE", os.path.join(DATA_DIR, "channels.json"))
API_KEYS_FILE = os.getenv("API_KEYS_FILE", os.path.join(DATA_DIR, "api_keys.json"))

# 负载均衡
MAX_FAIL_COUNT = _int_env("MAX_FAIL_COUNT", 5)  # 连续失败N次后剔除
COOLDOWN_SECONDS = _int_env("COOLDOWN_SECONDS", 60)  # 冷却恢复时间(秒)

# 请求超时（秒）
REQUEST_TIMEOUT = _int_env("REQUEST_TIMEOUT", 300)

# 代理访问鉴权
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")  # 代理API密钥，为空则不鉴权

# Debug 模式
DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
DEBUG_LOG_DIR = os.getenv("DEBUG_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))

# 日志级别
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()

# PostgreSQL 配置
DATABASE_URL = os.getenv("DATABASE_URL", "")

# 统计追踪的请求头
# 空值或 "ALL" 表示追踪所有请求头
_stats_tracked_headers_raw = os.getenv("STATS_TRACKED_HEADERS", "")
TRACK_ALL_HEADERS = _stats_tracked_headers_raw.strip().upper() == "ALL" or not _stats_tracked_headers_raw.strip()
STATS_TRACKED_HEADERS = None if TRACK_ALL_HEADERS else _stats_tracked_headers_raw.split(",")
