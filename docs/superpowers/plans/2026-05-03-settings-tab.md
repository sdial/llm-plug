# 设置 Tab + 全局配置集中化 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 新增设置 Tab 集中管理全局配置，支持在线修改持久化，热更新项立即生效，需重启项提供手动重启按钮。

**架构：** 新增 `data/settings.json` 持久化配置，改造 `config.py` 为可读写模式（启动时加载 settings.json + 环境变量回退），新增 settings/restart API 端点，前端新增设置 Tab 并将 LB Tab 改名为模型分组。

**技术栈：** Python / FastAPI / 原生 HTML + TailwindCSS CDN

---

## 文件结构

| 文件 | 变更类型 | 职责 |
|------|----------|------|
| `config.py` | 重构 | 可读写配置模块：加载 settings.json + 环境变量回退，提供 get/update 接口 |
| `storage.py` | 修改 | 新增 settings.json 加载/保存/迁移逻辑，lb_config 代理到 settings |
| `balancer/load_balancer.py` | 修改 | 从 config.get_setting() 读取参数 |
| `routers/admin.py` | 修改 | 新增 settings/restart API 端点 |
| `static/index.html` | 修改 | 新增设置 Tab，LB Tab 改名模型分组，移除 LB 全局参数卡片 |
| `main.py` | 修改 | 启动时初始化 settings，移除 PROXY_API_KEY 引用 |
| `models/model_group.py` | 修改 | LBConfig 保留但标记为内部兼容 |
| `tests/test_settings.py` | 新建 | settings 加载/保存/迁移/热更新测试 |

---

### 任务 1：改造 config.py — 配置定义与加载

**文件：**
- 修改：`config.py`
- 创建：`tests/test_settings.py`

- [ ] **步骤 1：编写失败的测试 — 配置定义与默认值**

```python
# tests/test_settings.py
import pytest

def test_config_defaults():
    """验证配置项默认值"""
    from config import _CONFIG_SCHEMA
    assert _CONFIG_SCHEMA["host"]["default"] == "0.0.0.0"
    assert _CONFIG_SCHEMA["port"]["default"] == 55555
    assert _CONFIG_SCHEMA["request_timeout"]["default"] == 300
    assert _CONFIG_SCHEMA["max_body_size"]["default"] == 10485760
    assert _CONFIG_SCHEMA["debug"]["default"] is False
    assert _CONFIG_SCHEMA["log_level"]["default"] == "info"
    assert _CONFIG_SCHEMA["stats_tracked_headers"]["default"] == ""
    assert _CONFIG_SCHEMA["database_url"]["default"] == ""
    assert _CONFIG_SCHEMA["max_fail_count"]["default"] == 5
    assert _CONFIG_SCHEMA["cooldown_seconds"]["default"] == 60

def test_config_requires_restart():
    """验证需重启标记"""
    from config import _CONFIG_SCHEMA
    restart_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("requires_restart")]
    assert "host" in restart_keys
    assert "port" in restart_keys
    assert "debug" in restart_keys
    assert "log_level" in restart_keys
    assert "database_url" in restart_keys
    # 热更新项不在列表中
    assert "request_timeout" not in restart_keys
    assert "max_fail_count" not in restart_keys
    assert "cooldown_seconds" not in restart_keys

def test_config_readonly():
    """验证只读标记"""
    from config import _CONFIG_SCHEMA
    readonly_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("readonly")]
    assert "host" in readonly_keys
    assert "port" in readonly_keys
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_settings.py -v`

预期：FAIL — `ImportError: cannot import name '_CONFIG_SCHEMA'`

- [ ] **步骤 3：实现配置定义**

在 `config.py` 顶部（`load_dotenv()` 之后）添加：

```python
import json

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
```

移除 `PROXY_API_KEY` 相关变量。移除 `DATABASE_URL`、`STATS_TRACKED_HEADERS`、`TRACK_ALL_HEADERS` 旧变量（它们将由 settings 系统统一管理）。保留 `HOST`、`PORT`、`DEBUG`、`LOG_LEVEL`、`REQUEST_TIMEOUT`、`MAX_BODY_SIZE` 模块级变量用于向后兼容，但值从 `_settings` 读取。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_settings.py::test_config_defaults tests/test_settings.py::test_config_requires_restart tests/test_settings.py::test_config_readonly -v`

预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add config.py tests/test_settings.py
git commit -m "feat: add config schema definition for settings tab"
```

---

### 任务 2：实现 config.py — settings 加载与读写接口

**文件：**
- 修改：`config.py`
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写失败的测试 — settings 加载**

```python
# tests/test_settings.py 追加
import tempfile
import os

@pytest.fixture
def tmp_settings_file(tmp_path):
    """创建临时 settings.json"""
    settings_path = tmp_path / "settings.json"
    return str(settings_path)

def test_init_settings_from_file(tmp_settings_file):
    """从 settings.json 加载配置"""
    import json
    data = {"request_timeout": 600, "max_fail_count": 10}
    with open(tmp_settings_file, "w") as f:
        json.dump(data, f)

    import config
    config._SETTINGS_FILE = tmp_settings_file
    config._settings = {}
    config._init_settings_sync()

    assert config._settings["request_timeout"] == 600
    assert config._settings["max_fail_count"] == 10
    # 未设置的项回退到默认值
    assert config._settings["cooldown_seconds"] == 60

def test_init_settings_env_fallback(tmp_settings_file, monkeypatch):
    """settings.json 无对应项时回退到环境变量"""
    import json
    with open(tmp_settings_file, "w") as f:
        json.dump({}, f)

    monkeypatch.setenv("REQUEST_TIMEOUT", "500")

    import config
    config._SETTINGS_FILE = tmp_settings_file
    config._settings = {}
    config._init_settings_sync()

    assert config._settings["request_timeout"] == 500

def test_init_settings_defaults(tmp_settings_file):
    """settings.json 不存在时使用默认值"""
    import config
    config._SETTINGS_FILE = tmp_settings_file  # 文件不存在
    config._settings = {}
    config._init_settings_sync()

    assert config._settings["request_timeout"] == 300
    assert config._settings["max_fail_count"] == 5

def test_get_setting():
    """get_setting 返回内存缓存中的值"""
    import config
    config._settings = {"request_timeout": 600}
    assert config.get_setting("request_timeout") == 600

def test_get_setting_default():
    """get_setting 对不存在的键返回默认值"""
    import config
    config._settings = {}
    assert config.get_setting("max_fail_count") == 5
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_settings.py -k "init_settings or get_setting" -v`

预期：FAIL — 缺少 `_init_settings_sync` / `get_setting`

- [ ] **步骤 3：实现 settings 加载与读取**

在 `config.py` 中添加：

```python
import asyncio

_settings: dict = {}
_settings_lock = asyncio.Lock()


def _init_settings_sync():
    """同步初始化 settings（用于测试和启动时）"""
    global _settings
    _settings = {}
    file_data = {}
    if os.path.exists(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            from loguru import logger
            logger.warning(f"settings.json 解析失败，回退到环境变量和默认值")

    for key, schema in _CONFIG_SCHEMA.items():
        if key in file_data:
            _settings[key] = _cast_value(file_data[key], schema["type"])
        elif schema["env"] and os.getenv(schema["env"]):
            _settings[key] = _cast_value(os.getenv(schema["env"]), schema["type"])
        else:
            _settings[key] = schema["default"]

    # 更新模块级变量（向后兼容）
    _sync_module_vars()


def _cast_value(value, type_name: str):
    if type_name == "int":
        return int(value)
    elif type_name == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    return str(value)


def _sync_module_vars():
    """同步 settings 到模块级变量（向后兼容）"""
    global HOST, PORT, DEBUG, LOG_LEVEL, REQUEST_TIMEOUT, MAX_BODY_SIZE
    HOST = _settings.get("host", "0.0.0.0")
    PORT = _settings.get("port", 55555)
    DEBUG = _settings.get("debug", False)
    LOG_LEVEL = _settings.get("log_level", "info")
    REQUEST_TIMEOUT = _settings.get("request_timeout", 300)
    MAX_BODY_SIZE = _settings.get("max_body_size", 10 * 1024 * 1024)


def get_setting(key: str):
    """同步读取单个配置值"""
    if key in _settings:
        return _settings[key]
    schema = _CONFIG_SCHEMA.get(key)
    if schema:
        return schema["default"]
    return None


def get_settings() -> dict:
    """同步读取所有配置项（敏感字段脱敏）"""
    result = {}
    for key, schema in _CONFIG_SCHEMA.items():
        val = _settings.get(key, schema["default"])
        if key == "database_url" and val:
            val = _mask_db_url(val)
        result[key] = val
    return result


def _mask_db_url(url: str) -> str:
    """脱敏 database_url：隐藏密码"""
    import re
    return re.sub(r'://([^:]+):([^@]+)@', r'://\1:***@', url)


async def init_settings():
    """异步初始化 settings（主启动流程调用）"""
    _init_settings_sync()
    # 执行 lb_config 迁移
    await _migrate_lb_config()


async def update_settings(updates: dict) -> dict:
    """异步更新配置：验证 → 持久化 → 更新内存 → 返回结果"""
    global _settings
    validated = {}
    needs_restart = False
    for key, value in updates.items():
        schema = _CONFIG_SCHEMA.get(key)
        if schema is None:
            continue
        if schema.get("readonly"):
            continue
        casted = _cast_value(value, schema["type"])
        validated[key] = casted
        if schema.get("requires_restart"):
            needs_restart = True

    async with _settings_lock:
        _settings.update(validated)
        await _save_settings_to_disk()
        _sync_module_vars()

    # 热更新：负载均衡参数
    if "max_fail_count" in validated or "cooldown_seconds" in validated:
        _apply_lb_settings()

    # 热更新：统计追踪请求头
    if "stats_tracked_headers" in validated:
        _apply_stats_headers_settings()

    return {"updated": list(validated.keys()), "needs_restart": needs_restart}


def _apply_lb_settings():
    """热更新负载均衡参数"""
    from balancer.load_balancer import load_balancer
    load_balancer.update_config(
        max_fail_count=_settings.get("max_fail_count", 5),
        cooldown_seconds=_settings.get("cooldown_seconds", 60),
    )


def _apply_stats_headers_settings():
    """热更新统计追踪请求头设置"""
    global TRACK_ALL_HEADERS, STATS_TRACKED_HEADERS
    raw = _settings.get("stats_tracked_headers", "")
    TRACK_ALL_HEADERS = raw.strip().upper() == "ALL" or not raw.strip()
    STATS_TRACKED_HEADERS = None if TRACK_ALL_HEADERS else raw.split(",")


async def _save_settings_to_disk():
    """将内存缓存写入 settings.json（原子写入）"""
    import tempfile
    dir_name = os.path.dirname(os.path.abspath(_SETTINGS_FILE)) or "."
    os.makedirs(dir_name, exist_ok=True)
    f = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=dir_name, delete=False,
        prefix=".settings_", suffix=".tmp.json",
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
```

同时给 `LoadBalancer` 添加 `update_config` 方法（在任务 3 中实现，此处先声明接口）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_settings.py -v`

预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add config.py tests/test_settings.py
git commit -m "feat: implement settings load/read/write in config.py"
```

---

### 任务 3：改造 load_balancer.py — 从 config 读取参数

**文件：**
- 修改：`balancer/load_balancer.py`
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写失败的测试 — update_config**

```python
# tests/test_settings.py 追加
def test_lb_update_config():
    """LoadBalancer.update_config 热更新参数"""
    from balancer.load_balancer import LoadBalancer
    lb = LoadBalancer()
    lb.update_config(max_fail_count=3, cooldown_seconds=30)
    assert lb._max_fail_count == 3
    assert lb._cooldown_seconds == 30
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_settings.py::test_lb_update_config -v`

预期：FAIL — `LoadBalancer` 无 `update_config` 方法

- [ ] **步骤 3：实现 load_balancer 改造**

修改 `balancer/load_balancer.py`：

```python
class LoadBalancer:
    def __init__(self):
        self._health: dict[str, ChannelHealth] = defaultdict(ChannelHealth)
        self._lock = asyncio.Lock()
        self._max_fail_count: int = 5
        self._cooldown_seconds: float = 60

    def update_config(self, max_fail_count: int, cooldown_seconds: int):
        """热更新配置参数"""
        self._max_fail_count = max_fail_count
        self._cooldown_seconds = float(cooldown_seconds)

    async def select_channel(
        self,
        channels: list[Channel],
        exclude_ids: set[str] | None = None,
    ) -> Optional[Channel]:
        exclude_ids = exclude_ids or set()
        async with self._lock:
            available = [
                ch
                for ch in channels
                if ch.enabled
                and ch.id not in exclude_ids
                and self._health[ch.id].is_healthy(self._max_fail_count, self._cooldown_seconds)
            ]
            if not available:
                return None
            available.sort(key=lambda ch: ch.priority)
            min_priority = available[0].priority
            top_group = [ch for ch in available if ch.priority == min_priority]
            if len(top_group) == 1:
                return top_group[0]
            return self._weighted_round_robin(top_group)
```

移除 `import storage`（不再从 storage 读取 lb_config）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_settings.py::test_lb_update_config -v`

预期：PASS

- [ ] **步骤 5：运行全部现有测试确认无回归**

运行：`uv run pytest tests/ -v --timeout=30`

预期：全部 PASS

- [ ] **步骤 6：Commit**

```bash
git add balancer/load_balancer.py
git commit -m "refactor: LoadBalancer reads config from update_config, not storage"
```

---

### 任务 4：实现 lb_config 迁移逻辑

**文件：**
- 修改：`config.py`
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写失败的测试 — lb_config 迁移**

```python
# tests/test_settings.py 追加
import json

@pytest.fixture
def tmp_data_dir(tmp_path):
    """创建临时数据目录"""
    d = tmp_path / "data"
    d.mkdir()
    return str(d)

def test_migrate_lb_config(tmp_data_dir, tmp_settings_file):
    """lb_config 自动迁移到 settings.json"""
    import config

    channels_file = os.path.join(tmp_data_dir, "channels.json")
    channels_data = {
        "channels": [],
        "lb_config": {"max_fail_count": 8, "cooldown_seconds": 120}
    }
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)

    config._SETTINGS_FILE = tmp_settings_file
    config._settings = {"max_fail_count": 5, "cooldown_seconds": 60}

    # 执行迁移
    config._migrate_lb_config_sync(channels_file)

    # settings 中应为迁移后的值
    assert config._settings["max_fail_count"] == 8
    assert config._settings["cooldown_seconds"] == 120

    # channels.json 中 lb_config 应被移除
    with open(channels_file, "r") as f:
        migrated = json.load(f)
    assert "lb_config" not in migrated
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_settings.py::test_migrate_lb_config -v`

预期：FAIL — `_migrate_lb_config_sync` 不存在

- [ ] **步骤 3：实现迁移逻辑**

在 `config.py` 中添加：

```python
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

    # 从 channels.json 移除 lb_config
    if "lb_config" in data:
        del data["lb_config"]
        import tempfile
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
    """异步迁移（从 config.CHANNELS_FILE 读取）"""
    _migrate_lb_config_sync(CHANNELS_FILE)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_settings.py::test_migrate_lb_config -v`

预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add config.py tests/test_settings.py
git commit -m "feat: add lb_config migration from channels.json to settings.json"
```

---

### 任务 5：改造 storage.py — lb_config 代理到 settings

**文件：**
- 修改：`storage.py`

- [ ] **步骤 1：修改 get_lb_config 和 save_lb_config**

将 `storage.py` 中的 `get_lb_config` 和 `save_lb_config` 改为代理到 config 模块：

```python
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
```

移除 `_LB_CONFIG_CACHE`、`_LB_CONFIG_CACHE_TS` 及相关缓存逻辑。移除 `invalidate_cache` 中的 `_LB_CONFIG_CACHE` 清理。

- [ ] **步骤 2：运行全部测试确认无回归**

运行：`uv run pytest tests/ -v --timeout=30`

预期：全部 PASS

- [ ] **步骤 3：Commit**

```bash
git add storage.py
git commit -m "refactor: lb_config reads/writes delegate to config settings"
```

---

### 任务 6：新增 settings/restart API 端点

**文件：**
- 修改：`routers/admin.py`
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写失败的测试 — settings API**

```python
# tests/test_settings.py 追加
import pytest

@pytest.mark.asyncio
async def test_update_settings_api():
    """PUT /admin/settings 更新配置"""
    from config import _settings
    _settings["request_timeout"] = 300

    import config
    result = await config.update_settings({"request_timeout": 600, "max_fail_count": 10})
    assert "request_timeout" in result["updated"]
    assert result["needs_restart"] is False
    assert _settings["request_timeout"] == 600
    assert _settings["max_fail_count"] == 10

@pytest.mark.asyncio
async def test_update_settings_needs_restart():
    """PUT /admin/settings 需重启配置返回 needs_restart=true"""
    from config import _settings
    result = await config.update_settings({"debug": True})
    assert result["needs_restart"] is True

@pytest.mark.asyncio
async def test_update_settings_readonly_ignored():
    """PUT /admin/settings 忽略只读配置"""
    from config import _settings
    original_port = _settings.get("port", 55555)
    result = await config.update_settings({"port": 9999})
    assert "port" not in result["updated"]
    assert _settings.get("port", 55555) == original_port
```

- [ ] **步骤 2：运行测试验证通过**

运行：`uv run pytest tests/test_settings.py -k "update_settings" -v`

预期：PASS（核心逻辑已在任务 2 实现，此步骤验证 API 层行为）

- [ ] **步骤 3：在 admin.py 新增 API 端点**

```python
# routers/admin.py 追加
import config as _config

@router.get("/settings")
async def get_settings_endpoint():
    """获取所有配置项"""
    return _config.get_settings()


@router.put("/settings")
async def update_settings_endpoint(body: dict):
    """批量更新配置"""
    result = await _config.update_settings(body)
    return result


@router.post("/restart")
async def restart_server(body: dict):
    """触发服务重启（Docker restart 策略自动拉起）"""
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="需要 confirm=true 确认重启")
    from loguru import logger
    logger.info("配置变更触发重启")
    import os
    os._exit(0)
```

- [ ] **步骤 4：Commit**

```bash
git add routers/admin.py tests/test_settings.py
git commit -m "feat: add settings/restart API endpoints"
```

---

### 任务 7：改造 main.py — 启动时初始化 settings

**文件：**
- 修改：`main.py`

- [ ] **步骤 1：修改 lifespan 初始化 settings**

在 `main.py` 的 `lifespan` 函数中，在 `await init_stats_db()` 之前添加：

```python
from config import init_settings
await init_settings()
```

移除 `from config import DEBUG, HOST, PORT, MAX_BODY_SIZE` 中的 `DEBUG`（改用 `config.get_setting("debug")`），其余保留用于 uvicorn 启动参数。

移除 `from config import PROXY_API_KEY` 引用（如有）。

- [ ] **步骤 2：修改 CombinedMiddleware 中的 config 引用**

`CombinedMiddleware` 中 `from config import STATS_TRACKED_HEADERS, TRACK_ALL_HEADERS` 改为从 `config` 模块运行时读取：

```python
import config as _config
# 在 __call__ 中：
tracked_headers_raw = _config.get_setting("stats_tracked_headers")
track_all = tracked_headers_raw.strip().upper() == "ALL" or not tracked_headers_raw.strip()
if track_all:
    scope["state"]["tracked_headers"] = headers_dict
else:
    scope["state"]["tracked_headers"] = {
        k: v for k, v in headers_dict.items()
        if k.lower() in [h.lower() for h in tracked_headers_raw.split(",")]
    }
```

- [ ] **步骤 3：运行全部测试确认无回归**

运行：`uv run pytest tests/ -v --timeout=30`

预期：全部 PASS

- [ ] **步骤 4：Commit**

```bash
git add main.py
git commit -m "feat: init settings on startup, remove PROXY_API_KEY"
```

---

### 任务 8：前端 — 新增设置 Tab

**文件：**
- 修改：`static/index.html`

- [ ] **步骤 1：在 Tab 栏添加"设置"按钮**

在 Tab 栏的"请求记录"按钮后添加：

```html
<button onclick="switchTab('settings')" id="tab_settings" class="px-4 py-2.5 text-sm font-medium tab-inactive">设置</button>
```

- [ ] **步骤 2：添加设置 Tab HTML 面板**

在 `<div id="requestsTab">` 之后添加设置 Tab 面板：

```html
<!-- 设置 Tab -->
<div id="settingsTab" class="hidden">
  <!-- 服务信息（只读） -->
  <div class="card p-5 mb-5">
    <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-4">服务信息</h2>
    <div class="grid grid-cols-2 gap-4">
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">监听地址</label>
        <input type="text" id="s_host" readonly class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm bg-surface-50 text-ink-400 cursor-not-allowed">
        <p class="text-xs text-ink-400 mt-1">Docker 运行时不可修改</p>
      </div>
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">监听端口</label>
        <input type="text" id="s_port" readonly class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm bg-surface-50 text-ink-400 cursor-not-allowed">
        <p class="text-xs text-ink-400 mt-1">Docker 运行时不可修改</p>
      </div>
    </div>
  </div>

  <!-- 请求处理 -->
  <div class="card p-5 mb-5">
    <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-4">请求处理</h2>
    <div class="grid grid-cols-2 gap-4">
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">请求超时(秒)</label>
        <input type="number" id="s_request_timeout" min="1" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
      </div>
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">请求体上限(字节)</label>
        <input type="number" id="s_max_body_size" min="1" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
      </div>
    </div>
  </div>

  <!-- 调试与日志 -->
  <div class="card p-5 mb-5">
    <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-4">调试与日志</h2>
    <div class="grid grid-cols-2 gap-4">
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">Debug 模式</label>
        <div class="flex items-center gap-2">
          <input type="checkbox" id="s_debug" class="rounded border-surface-200 text-brand-500 focus:ring-brand-500/30">
          <span class="text-xs text-amber-600 font-medium">需重启</span>
        </div>
      </div>
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">日志级别 <span class="text-xs text-amber-600 font-medium">需重启</span></label>
        <select id="s_log_level" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
          <option value="debug">debug</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
        </select>
      </div>
    </div>
  </div>

  <!-- 统计 -->
  <div class="card p-5 mb-5">
    <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-4">统计</h2>
    <div>
      <label class="block text-sm font-medium text-ink-900 mb-1">追踪请求头</label>
      <input type="text" id="s_stats_tracked_headers" placeholder="逗号分隔，留空或 ALL 表示追踪全部" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
      <p class="text-xs text-ink-400 mt-1">逗号分隔指定 header 名称，留空或 ALL 追踪全部</p>
    </div>
  </div>

  <!-- 数据库 -->
  <div class="card p-5 mb-5">
    <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-4">数据库</h2>
    <div>
      <label class="block text-sm font-medium text-ink-900 mb-1">PostgreSQL 连接串 <span class="text-xs text-amber-600 font-medium">需重启</span></label>
      <input type="password" id="s_database_url" placeholder="postgres://user:pass@host:port/db" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white font-mono">
      <div id="s_database_url_preview" class="text-xs text-ink-400 mt-1"></div>
    </div>
  </div>

  <!-- 负载均衡 -->
  <div class="card p-5 mb-5">
    <h2 class="text-xs font-semibold text-ink-600 uppercase tracking-wider mb-4">负载均衡</h2>
    <div class="grid grid-cols-2 gap-4">
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">失败次数阈值</label>
        <input type="number" id="s_max_fail_count" min="1" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
      </div>
      <div>
        <label class="block text-sm font-medium text-ink-900 mb-1">冷却时间(秒)</label>
        <input type="number" id="s_cooldown_seconds" min="1" class="w-full border border-surface-200 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 bg-white">
      </div>
    </div>
  </div>

  <!-- 保存/重启按钮 -->
  <div class="flex items-center gap-3">
    <button onclick="saveSettings()" id="saveSettingsBtn" class="btn-primary text-sm px-4 py-2 font-medium">保存设置</button>
    <button onclick="restartServer()" id="restartBtn" class="hidden bg-rose-600 hover:bg-rose-700 text-white text-sm px-4 py-2 font-medium rounded-lg transition">重启服务</button>
    <span id="settingsHint" class="text-sm text-ink-600 opacity-0 transition-opacity duration-300"></span>
  </div>
</div>
```

- [ ] **步骤 3：添加设置 Tab JavaScript 逻辑**

```javascript
// ========== 设置管理 ==========
async function loadSettings() {
  try {
    const resp = await fetch('/admin/settings');
    const data = await resp.json();
    document.getElementById('s_host').value = data.host || '0.0.0.0';
    document.getElementById('s_port').value = data.port || 55555;
    document.getElementById('s_request_timeout').value = data.request_timeout || 300;
    document.getElementById('s_max_body_size').value = data.max_body_size || 10485760;
    document.getElementById('s_debug').checked = data.debug || false;
    document.getElementById('s_log_level').value = data.log_level || 'info';
    document.getElementById('s_stats_tracked_headers').value = data.stats_tracked_headers || '';
    document.getElementById('s_database_url').value = '';
    document.getElementById('s_database_url').placeholder = data.database_url || '未配置';
    if (data.database_url) {
      document.getElementById('s_database_url_preview').textContent = '当前: ' + data.database_url;
    }
    document.getElementById('s_max_fail_count').value = data.max_fail_count || 5;
    document.getElementById('s_cooldown_seconds').value = data.cooldown_seconds || 60;
  } catch (e) {
    console.error('加载设置失败:', e);
  }
}

async function saveSettings() {
  const updates = {};
  // 收集非只读字段的修改
  const fields = {
    request_timeout: parseInt(document.getElementById('s_request_timeout').value),
    max_body_size: parseInt(document.getElementById('s_max_body_size').value),
    debug: document.getElementById('s_debug').checked,
    log_level: document.getElementById('s_log_level').value,
    stats_tracked_headers: document.getElementById('s_stats_tracked_headers').value,
    max_fail_count: parseInt(document.getElementById('s_max_fail_count').value),
    cooldown_seconds: parseInt(document.getElementById('s_cooldown_seconds').value),
  };
  const dbUrl = document.getElementById('s_database_url').value.trim();
  if (dbUrl) fields.database_url = dbUrl;

  Object.assign(updates, fields);

  try {
    const resp = await fetch('/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    const result = await resp.json();
    const hint = document.getElementById('settingsHint');
    hint.textContent = '设置已保存';
    hint.classList.remove('opacity-0', 'text-amber-600');
    hint.classList.add('opacity-100', 'text-emerald-600');
    setTimeout(() => { hint.classList.remove('opacity-100'); hint.classList.add('opacity-0'); }, 2000);

    if (result.needs_restart) {
      document.getElementById('restartBtn').classList.remove('hidden');
      hint.textContent = '已保存，部分配置需重启生效';
      hint.classList.remove('text-emerald-600');
      hint.classList.add('text-amber-600');
      hint.classList.remove('opacity-0');
      hint.classList.add('opacity-100');
    } else {
      document.getElementById('restartBtn').classList.add('hidden');
    }
    loadSettings();
  } catch (e) {
    alert('保存失败: ' + e.message);
  }
}

async function restartServer() {
  if (!confirm('确认重启服务？服务将短暂不可用。')) return;
  try {
    await fetch('/admin/restart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
  } catch (e) {
    // 请求会因进程退出而失败，这是正常行为
  }
  const hint = document.getElementById('settingsHint');
  hint.textContent = '服务正在重启，请等待...';
  hint.classList.remove('opacity-0', 'text-amber-600');
  hint.classList.add('opacity-100', 'text-ink-600');
  // 5秒后尝试重新加载
  setTimeout(() => { location.reload(); }, 5000);
}
```

- [ ] **步骤 4：修改 switchTab 函数**

在 `switchTab` 函数中添加 settings tab 的处理：

```javascript
const tabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings'];
const panelMap = { channels: 'channelsTab', apikeys: 'apikeysTab', lb: 'lbTab', stats: 'statsTab', requests: 'requestsTab', settings: 'settingsTab' };
```

在 switchTab 函数体中添加：

```javascript
if (tab === 'settings') { loadSettings(); }
```

- [ ] **步骤 5：Commit**

```bash
git add static/index.html
git commit -m "feat: add settings tab UI with save and restart"
```

---

### 任务 9：前端 — LB Tab 改名模型分组 + 移除全局参数卡片

**文件：**
- 修改：`static/index.html`

- [ ] **步骤 1：修改 Tab 按钮文字**

将 LB Tab 按钮文字改为"模型分组"：

```html
<button onclick="switchTab('lb')" id="tab_lb" class="px-4 py-2.5 text-sm font-medium tab-inactive">模型分组</button>
```

- [ ] **步骤 2：移除 LB Tab 中的全局参数卡片**

删除 `lbTab` 中的全局参数卡片 HTML（包含 `lbMaxFailCount`、`lbCooldownSeconds` 输入框和保存按钮的 `<div class="card p-5 mb-5">` 卡片）。

删除 `saveLbConfig()` 和 `loadLbConfig()` JavaScript 函数。

- [ ] **步骤 3：手动验证**

启动服务 `uv run python main.py`，在浏览器中确认：
1. "模型分组"Tab 只显示模型组列表，无全局参数卡片
2. "设置"Tab 正常显示所有配置项
3. 保存设置功能正常
4. 重启按钮在修改需重启配置后出现

- [ ] **步骤 4：Commit**

```bash
git add static/index.html
git commit -m "refactor: rename LB tab to 模型分组, remove global params card"
```

---

### 任务 10：集成测试与最终验证

**文件：**
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写集成测试**

```python
# tests/test_settings.py 追加
import json
import os

def test_full_settings_lifecycle(tmp_path, monkeypatch):
    """完整设置生命周期测试：创建 → 读取 → 修改 → 读取"""
    settings_file = str(tmp_path / "settings.json")
    channels_file = str(tmp_path / "channels.json")

    # 初始 channels.json 带 lb_config
    with open(channels_file, "w") as f:
        json.dump({"channels": [], "lb_config": {"max_fail_count": 7, "cooldown_seconds": 90}}, f)

    import config
    config._SETTINGS_FILE = settings_file
    config._settings = {}
    config._init_settings_sync()
    config._migrate_lb_config_sync(channels_file)

    # 迁移后应包含 lb_config 的值
    assert config._settings["max_fail_count"] == 7
    assert config._settings["cooldown_seconds"] == 90

    # 读取
    all_settings = config.get_settings()
    assert all_settings["max_fail_count"] == 7

    # 单键读取
    assert config.get_setting("cooldown_seconds") == 90

    # settings.json 应已创建
    assert os.path.exists(settings_file)
```

- [ ] **步骤 2：运行全部测试**

运行：`uv run pytest tests/ -v --timeout=30`

预期：全部 PASS

- [ ] **步骤 3：运行 ruff 检查**

运行：`uv run ruff check .`

预期：无错误

- [ ] **步骤 4：手动端到端验证**

启动服务 `uv run python main.py`，在浏览器中完整走一遍：
1. 打开设置 Tab，确认所有配置项正确加载
2. 修改热更新项（如请求超时），保存，确认立即生效
3. 修改需重启项（如 Debug），保存，确认出现重启按钮
4. 打开模型分组 Tab，确认无全局参数卡片
5. 确认渠道管理、API Key、统计、请求记录 Tab 功能正常

- [ ] **步骤 5：Commit**

```bash
git add tests/test_settings.py
git commit -m "test: add settings lifecycle integration test"
```
