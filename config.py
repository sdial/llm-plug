import os

# 服务器配置
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# 数据存储
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
CHANNELS_FILE = os.getenv("CHANNELS_FILE", os.path.join(DATA_DIR, "channels.json"))
API_KEYS_FILE = os.getenv("API_KEYS_FILE", os.path.join(DATA_DIR, "api_keys.json"))

# 负载均衡
MAX_FAIL_COUNT = int(os.getenv("MAX_FAIL_COUNT", "5"))  # 连续失败N次后剔除
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))  # 冷却恢复时间(秒)

# 请求超时（秒）
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))

# 代理访问鉴权
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")  # 代理API密钥，为空则不鉴权

# Debug 模式
DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
DEBUG_LOG_DIR = os.getenv("DEBUG_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))
