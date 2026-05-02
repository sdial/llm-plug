# 异步阻塞问题修复计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 消除 REVIEW.md 中确认的 16 个异步阻塞/配置问题，使 hot path 不再阻塞事件循环。

**架构：** 分 5 批按优先级修复：P0 立即修复 hot path 同步 IO、P1 配置层快速修复、P2 中等优先级优化、P3 长期重构。每批独立可测。

**技术栈：** asyncio.to_thread / asyncio.Lock / asyncio.Queue / httpx.Limits / asyncpg / uvloop

---

## 文件结构

| 文件 | 变更类型 | 职责 |
|------|---------|------|
| `storage.py` | 修改 | 核心存储层异步化：`load_data`/`save_data`/`load_api_keys`/`save_api_keys` 改 async，threading.RLock → asyncio.Lock |
| `proxy_core.py` | 修改 | `_log_debug` 异步化；`_get_channels_for_model` 改 async + asyncio.Lock；`stats.record_request` 改 fire-and-forget |
| `main.py` | 修改 | 中间件 `load_api_keys` 改 async；body 大小限制 |
| `client.py` | 修改 | httpx.Limits 配置；流式客户端复用缓存 |
| `stats.py` | 修改 | asyncpg 连接池 max_size 调大 |
| `config.py` | 修改 | 新增 `MAX_BODY_SIZE` 配置 |
| `routers/admin.py` | 修改 | 同步 handler 改 async；`get_lock()` 调用适配 |
| `routers/proxy_models.py` | 修改 | `load_data` 调用适配 async |
| `routers/proxy_base.py` | 无变更 | 已是 async |
| `pyproject.toml` | 修改 | 移除 psycopg2-binary；添加 uvloop 条件依赖 |
| `docker-deploy/Dockerfile` | 修改 | log-level 改 info；添加性能参数 |
| `start.sh` | 修改 | 添加性能参数 |
| `tests/test_storage.py` | 修改 | 适配 async 接口 |
| `tests/test_client.py` | 修改 | 适配新 Limits 和流式缓存行为 |

---

## 任务 1：storage.py 异步化（核心）

> 解决问题 #1、#3、#4

**文件：**
- 修改：`storage.py`
- 修改：`tests/test_storage.py`

### 设计决策

1. `threading.RLock` → `asyncio.Lock`：storage 现在全部被 async 代码调用，用 asyncio.Lock 更合适
2. `load_data`/`save_data`/`load_api_keys`/`save_api_keys` 全部改 async：内部 IO 用 `await asyncio.to_thread()` 包裹
3. 保持缓存机制不变（5秒 TTL），只是锁和 IO 调用方式改变
4. `get_lock()` 不再暴露给外部——admin.py 中所有 `with get_lock():` 改为直接调用 storage 的 async 方法（storage 内部自行加锁）
5. `_invalidate_model_channels_cache` 回调也需要适配 async（改用 `asyncio.create_task` 或在 save_data 内部同步调用）

### 具体变更

#### storage.py

```python
# 将 threading 导入替换为 asyncio
import asyncio
import json
import os
import tempfile
import time
from typing import Any, Callable

import config
from models.model_group import LBConfig, ModelGroup

_lock = asyncio.Lock()

# 移除 get_lock() 函数（不再暴露锁给外部）
# 所有需要锁的操作都封装在 storage 模块内部

_cache: dict[str, Any] | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0

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


def _read_from_disk_sync() -> dict[str, Any]:
    with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_to_disk_sync(data: dict[str, Any]) -> None:
    dir_name = os.path.dirname(os.path.abspath(config.CHANNELS_FILE)) or "."
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


async def load_data() -> dict[str, Any]:
    global _cache, _cache_ts
    _ensure_data_dir()
    async with _lock:
        now = time.time()
        if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
            return _cache
        if not os.path.exists(config.CHANNELS_FILE):
            data = {"channels": []}
            await asyncio.to_thread(_write_to_disk_sync, data)
        else:
            data = await asyncio.to_thread(_read_from_disk_sync)
        _cache = data
        _cache_ts = time.time()
        return data


async def invalidate_cache() -> None:
    global _cache, _cache_ts, _keys_cache, _keys_cache_ts, _MODEL_GROUPS_CACHE, _MODEL_GROUPS_CACHE_TS, _LB_CONFIG_CACHE, _LB_CONFIG_CACHE_TS
    async with _lock:
        _cache = None
        _cache_ts = 0
        _keys_cache = None
        _keys_cache_ts = 0
        _MODEL_GROUPS_CACHE = None
        _MODEL_GROUPS_CACHE_TS = 0
        _LB_CONFIG_CACHE = None
        _LB_CONFIG_CACHE_TS = 0


async def save_data(data: dict[str, Any]) -> None:
    global _cache, _cache_ts
    _ensure_data_dir()
    async with _lock:
        await asyncio.to_thread(_write_to_disk_sync, data)
        _cache = data
        _cache_ts = time.time()
        _trigger_save_callbacks()
```

对 `load_api_keys`/`save_api_keys`/`invalidate_keys_cache`/`load_model_groups`/`save_model_groups`/`get_lb_config`/`save_lb_config` 等所有函数做同样改造：
- 加 `async` 前缀
- `with _lock:` → `async with _lock:`
- 内部 `open()`/`json.load()`/`json.dump()` → `await asyncio.to_thread(...)`
- `save_data`/`load_data` 内部调用 → `await save_data(...)` / `await load_data(...)`

#### tests/test_storage.py

所有调用 `storage.load_data()`/`storage.save_data()` 等的地方改为 `await storage.load_data()` 等，测试函数加 `async` + `@pytest.mark.anyio`。

---

## 任务 2：proxy_core.py 异步化（核心）

> 解决问题 #2、#4、#5、#16

**文件：**
- 修改：`proxy_core.py`

### 具体变更

#### 2a. `_log_debug` 异步化

将 `_log_debug` 改为 async 函数，内部文件 IO 用 `asyncio.to_thread`：

```python
async def _log_debug(
    channel: Channel,
    upstream_url: str,
    upstream_data: dict,
    upstream_headers: dict,
    response_data: Any = None,
    is_stream: bool = False,
    stream_content: Any = None,
    response_headers: dict | None = None,
    status_code: int | None = None,
    error: str | None = None,
):
    if not DEBUG:
        return
    try:
        os.makedirs(DEBUG_LOG_DIR, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(DEBUG_LOG_DIR, f"debug_{today}.jsonl")
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "channel": {
                "id": channel.id,
                "name": channel.name,
                "api_type": channel.api_type.value,
                "base_url": channel.base_url,
            },
            "request": {
                "url": upstream_url,
                "headers": {k: v for k, v in upstream_headers.items() if k.lower() != "authorization"},
                "body": upstream_data,
            },
            "response": {
                "is_stream": is_stream,
                "status_code": status_code,
                "headers": {k: v for k, v in (response_headers or {}).items() if k.lower() not in ("authorization", "set-cookie")},
            },
        }
        if is_stream:
            if stream_content:
                if isinstance(stream_content, list) and len(stream_content) > 100:
                    log_entry["response"]["stream_chunks_count"] = len(stream_content)
                    log_entry["response"]["stream_content_sample"] = stream_content[:10] + stream_content[-10:]
                else:
                    log_entry["response"]["stream_content"] = stream_content
        else:
            log_entry["response"]["data"] = response_data
        if error:
            log_entry["error"] = error

        line = json.dumps(log_entry, ensure_ascii=False) + "\n"
        await asyncio.to_thread(_write_log_line, log_file, line)
    except Exception as log_err:
        logger.warning(f"Failed to write debug log: {log_err}")


def _write_log_line(log_file: str, line: str) -> None:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)
```

所有调用 `_log_debug(...)` 的地方改为 `await _log_debug(...)`。但注意：在流式的 `_do_stream_request` 中，`_log_debug` 被调用在 try/except/finally 块中，generator 函数本身已是 async generator，可以直接 await。

#### 2b. `_get_channels_for_model` 改 async + asyncio.Lock

```python
_model_channels_cache: dict[str, list[Channel]] | None = None
_model_channels_lock = asyncio.Lock()


async def _invalidate_model_channels_cache() -> None:
    global _model_channels_cache
    async with _model_channels_lock:
        _model_channels_cache = None
    data = await storage.load_data()
    active_ids = {ch.get("id") for ch in data.get("channels", [])}
    load_balancer.cleanup_removed_channels(active_ids)


register_save_callback(lambda: asyncio.create_task(_invalidate_model_channels_cache()))


async def _get_channels_for_model(model: str) -> list[Channel]:
    global _model_channels_cache
    async with _model_channels_lock:
        if _model_channels_cache is not None:
            return _model_channels_cache.get(model, [])
        data = await storage.load_data()
        channels = [Channel(**ch) for ch in data.get("channels", [])]
        _model_channels_cache = {}
        for ch in channels:
            if not ch.enabled:
                continue
            for m in ch.models:
                _model_channels_cache.setdefault(m, []).append(ch)
        return _model_channels_cache.get(model, [])
```

所有调用 `_get_channels_for_model(model)` 的地方改为 `await _get_channels_for_model(model)`。

#### 2c. `stats.record_request` 改 fire-and-forget

将非流式路径和流式 finally 中的 `await stats.record_request(...)` 改为 `asyncio.create_task(stats.record_request(...))`。

注意：`record_request` 内部已有 `_db_available` 检查，如果 DB 不可用会直接返回。create_task 不会因为未 await 而丢失异常——异常会被 asyncio 记录为 "Task exception was never retrieved" 警告。为避免此警告，包装一个静默异常的辅助函数：

```python
def _fire_and_forget(coro):
    """创建后台任务，静默处理异常。"""
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return task
```

将所有 `await stats.record_request(...)` 替换为 `_fire_and_forget(stats.record_request(...))`。

---

## 任务 3：main.py 中间件 + body 限制

> 解决问题 #1（中间件部分）、#15

**文件：**
- 修改：`main.py`
- 修改：`config.py`

#### 3a. 中间件 `load_api_keys` 异步化

`main.py:117-118`：
```python
# 原来：
keys_data = load_api_keys()
# 改为：
keys_data = await storage.load_api_keys()
```

同时 `main.py:25` lifespan 中：
```python
channels_data = await load_data()
keys_data = await load_api_keys()
```

#### 3b. 添加 body 大小限制

在 `config.py` 新增：
```python
MAX_BODY_SIZE = _int_env("MAX_BODY_SIZE", 10 * 1024 * 1024)  # 10MB 默认
```

在 `main.py` CombinedMiddleware 的 body buffering 循环中添加检查：
```python
body_parts = []
more_body = True
total_size = 0
while more_body:
    message = await receive()
    chunk = message.get("body", b"")
    body_parts.append(chunk)
    total_size += len(chunk)
    if total_size > MAX_BODY_SIZE:
        await self._send_error(send, 413, "Request body too large")
        return
    more_body = message.get("more_body", False)
body_bytes = b"".join(body_parts)
```

---

## 任务 4：admin.py 适配异步 storage

> 解决问题 #9

**文件：**
- 修改：`routers/admin.py`

所有 `def` handler 改为 `async def`，所有 `with get_lock():` 块移除（storage 内部自行加锁），所有 `load_data()`/`save_data()`/`load_api_keys()`/`save_api_keys()` 调用加 `await`。

关键变更：
- `def list_channels` → `async def list_channels`，`_get_channels()` → `await _get_channels()`（`_get_channels` 本身也要改 async）
- `def create_channel` → `async def create_channel`，移除 `with get_lock():`
- `async def update_channel` → 移除 `with get_lock():`，`_get_channels()`/`_save_channels()` 加 await
- 同理 `delete_channel`、`toggle_channel`、`create_api_key`、`update_api_key`、`delete_api_key`、`regenerate_api_key` 等
- `_get_channels`/`_save_channels`/`_get_api_keys`/`_save_api_keys` 辅助函数改 async
- `from storage import ... get_lock` → 移除 `get_lock` 导入

---

## 任务 5：proxy_models.py 适配异步 storage

> 无独立问题编号，但需要配合任务 1

**文件：**
- 修改：`routers/proxy_models.py`

`_collect_models` 改 async：
```python
async def _collect_models() -> list[dict]:
    data = await storage.load_data()
    ...
```

两个 endpoint 函数中调用改为 `await _collect_models()`。

---

## 任务 6：client.py httpx Limits + 流式缓存

> 解决问题 #7、#8

**文件：**
- 修改：`client.py`
- 修改：`tests/test_client.py`

#### 6a. 添加 httpx.Limits

```python
_DEFAULT_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=60.0,
)
```

在 `get_or_create_client` 和 `create_stream_client` 中传入 `limits=_DEFAULT_LIMITS`。

#### 6b. 流式客户端复用缓存

添加独立的流式客户端缓存：

```python
_stream_clients: dict[str, httpx.AsyncClient] = {}
_stream_cache_ts: dict[str, float] = {}


async def get_or_create_stream_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient:
    if timeout is None:
        timeout = float(REQUEST_TIMEOUT)
    key = _cache_key(channel)
    client = _stream_clients.get(key)
    if client is not None and not client.is_closed:
        _stream_cache_ts[key] = time.time()
        return client
    async with _lock:
        client = _stream_clients.get(key)
        if client is not None and not client.is_closed:
            _stream_cache_ts[key] = time.time()
            return client
        proxy = channel.socks5_proxy
        if proxy:
            client = httpx.AsyncClient(
                proxy=proxy,
                timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
                limits=_DEFAULT_LIMITS,
            )
        else:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
                limits=_DEFAULT_LIMITS,
            )
        _stream_clients[key] = client
        _stream_cache_ts[key] = time.time()
        return client
```

同时 `create_stream_client` 保留为兼容函数（返回非缓存 client），但在 `proxy_core.py` 中改用 `await get_or_create_stream_client(channel)`。

流式 `_do_stream_request` 中：
```python
# 原来：
client = create_stream_client(channel)
# 改为：
client = await get_or_create_stream_client(channel)
```

**重要**：流式请求结束后不能 `await client.aclose()`（因为是共享缓存客户端），移除 finally 中的 aclose。`close_all_clients`/`cleanup_stale_clients` 需要同时清理 `_stream_clients`。

---

## 任务 7：stats.py 连接池调大

> 解决问题 #6

**文件：**
- 修改：`stats.py`

```python
# 原来：
_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
# 改为：
_pool = await asyncpg.create_pool(
    DATABASE_URL,
    min_size=2,
    max_size=40,
    max_inactive_connection_lifetime=600,
)
```

---

## 任务 8：移除 psycopg2-binary + 添加 uvloop

> 解决问题 #10、#11

**文件：**
- 修改：`pyproject.toml`

```toml
dependencies = [
    "asyncpg>=0.29.0",
    "python-dotenv>=1.0.0",
    "fastapi>=0.136.0",
    "httptools>=0.7.1",
    "httpx[socks]>=0.28.1",
    # 移除: "psycopg2-binary>=2.9.0",
    "pydantic>=2.13.3",
    "python-socks[asyncio]>=2.8.1",
    "uvicorn>=0.46.0",
    "loguru>=0.7.3",
    "uvloop>=0.21.0; sys_platform != 'win32'",
]
```

#### start.sh 添加 uvloop 启动参数

```bash
# run 模式添加：
--loop uvloop

# 但需要条件判断，Windows 不支持：
if [[ "$OSTYPE" != "msys" && "$OSTYPE" != "win32" && "$OSTYPE" != "cygwin" ]]; then
    LOOP_ARG="--loop uvloop"
else
    LOOP_ARG=""
fi
```

#### Dockerfile 添加 `--loop uvloop`

---

## 任务 9：Dockerfile + start.sh 性能参数

> 解决问题 #12、#13、#14

**文件：**
- 修改：`docker-deploy/Dockerfile`
- 修改：`start.sh`
- 修改：`main.py`

#### 9a. Dockerfile 日志级别改 info

```dockerfile
CMD ["uvicorn", "main:app", \
"--host", "0.0.0.0", \
"--port", "55555", \
"--http", "httptools", \
"--workers", "2", \
"--timeout-keep-alive", "360", \
"--log-level", "info", \
"--no-use-colors", \
"--no-server-header", \
"--ws", "none", \
"--loop", "uvloop", \
"--backlog", "2048"]
```

#### 9b. start.sh 添加性能参数

run 模式中添加：
```bash
${LOOP_ARG:+$LOOP_ARG} \
--ws none \
--backlog 2048
```

debug 模式不加（单 worker 无需）。

#### 9c. main.py __main__ 添加 httptools

```python
# 原来：
uvicorn.run("main:app", host=HOST, port=PORT, reload=True, log_level=args.log_level, log_config=log_config)
# 改为：
uvicorn.run("main:app", host=HOST, port=PORT, reload=True, log_level=args.log_level, log_config=log_config, http="httptools")
```

---

## 依赖关系

```
任务 1 (storage 异步化) ← 任务 2 (proxy_core)
                       ← 任务 3 (main.py 中间件)
                       ← 任务 4 (admin.py)
                       ← 任务 5 (proxy_models)

任务 6 (client.py) ← 独立，可并行
任务 7 (stats.py) ← 独立，可并行
任务 8 (pyproject.toml) ← 独立，可并行
任务 9 (Dockerfile/start.sh) ← 任务 8 中 uvloop 依赖

建议执行顺序：
批次 1（P0 立即）：任务 1 → 任务 2 → 任务 3 → 任务 4 → 任务 5
批次 2（P1 配置）：任务 6 + 任务 7 + 任务 8（可并行）
批次 3（P1 配置）：任务 9（依赖任务 8）
```

## 风险与注意事项

1. **`_invalidate_model_channels_cache` 是同步回调**：`register_save_callback` 注册的是 `Callable[[], None]`。改为 async 后，回调需要改为 `asyncio.create_task()` 方式触发。需要注意在 `save_data` 被同步代码路径调用时的兼容性（目前所有调用都已在 async 上下文中）。

2. **流式客户端缓存**：原来每个流式请求结束后 `await client.aclose()`，改为缓存后不能关闭。但 `async with client.stream(...)` 不会关闭底层连接池，只关闭当前 stream 上下文。需要确保 `_do_stream_request` 的 finally 块中移除 `aclose()`。

3. **`_fire_and_forget` 中 `stats.record_request` 异常**：如果 asyncpg 连接出错，create_task 中的异常会以 warning 形式记录。这是可接受的——统计失败不应影响主流程。

4. **`asyncio.Lock` 不可跨线程**：`threading.RLock` 可跨线程，`asyncio.Lock` 只能在同一事件循环中使用。由于 storage 的所有调用者现在都在 async 上下文中（FastAPI 路由），这不构成问题。但如果未来有同步代码需要调用 storage，需要另行处理。

5. **测试兼容**：`tests/conftest.py` 中 `_setup_e2e_channels` 是同步函数，直接操作文件不经过 storage，无需修改。但 `test_storage.py` 的所有测试需要改为 async。

6. **uvloop 仅 Linux/Mac**：Windows 不支持，需要条件判断。uvicorn 在 Windows 上会自动忽略 `--loop uvloop` 参数。
