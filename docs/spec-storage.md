# spec-storage — 存储层

> 对应文件：`storage.py`（约 167 行）

## 模块定位

`storage.py` 负责渠道数据和 API Key 数据的持久化存储，使用 JSON 文件作为存储介质。它提供了线程安全的读写接口，并内置内存缓存以减少磁盘 IO。

## 存储格式

数据存储在 `channels.json` 文件中，格式为：

```json
{
  "channels": [
    {
      "id": "ch_a1b2c3d4",
      "name": "OpenAI 官方",
      "api_type": "openai-chat-completions",
      "base_url": "https://api.openai.com",
      "api_key": "sk-xxx",
      "models": ["gpt-4", "gpt-3.5-turbo"],
      "enabled": true,
      "weight": 1,
      "priority": 1,
      "socks5_proxy": null,
      "created_at": "2024-01-01T00:00:00+00:00"
    }
  ]
}
```

## 全局状态

```python
_lock = threading.RLock()   # 可重入锁，保证线程安全
_cache: dict | None = None  # 内存缓存
_cache_ts: float = 0        # 缓存时间戳
_CACHE_TTL = 5.0            # 缓存有效期（秒）
```

## 核心函数

### `load_data() -> dict`

**读取数据**，优先从内存缓存读取，缓存过期或不存在则从磁盘读取。

```python
def load_data() -> dict[str, Any]:
```

**流程**：

1. `_ensure_data_dir()` — 确保数据目录存在
2. 获取 `_lock`
3. 双重检查缓存：在锁内再次检查 `_cache` 是否有效（`now - _cache_ts < _CACHE_TTL`）
4. 如果 `channels.json` 不存在 → 创建空文件 `{"channels": []}`
5. `_read_from_disk()` → 读取 JSON
6. 更新 `_cache` 和 `_cache_ts`
7. 返回数据

**缓存 TTL**：5 秒。意味着直接修改 `channels.json` 文件后，最多需要 5 秒才能被 `load_data()` 感知到。

### `save_data(data) -> None`

**写入数据**，使用原子写入确保数据安全。

```python
def save_data(data: dict[str, Any]) -> None:
```

**流程**：

1. `_ensure_data_dir()` — 确保数据目录存在
2. 获取 `_lock`
3. 创建临时文件（同目录下，前缀 `.channels_`，后缀 `.tmp.json`）
4. 写入 JSON 到临时文件
5. `f.flush()` + `os.fsync()` — 确保数据落盘
6. `os.replace(tmp_path, channels.json)` — 原子替换
7. **立即更新内存缓存** `_cache = data`，`_cache_ts = now`

**原子写入**：`os.replace()` 在 POSIX 系统上是原子操作，保证即使写入过程中崩溃，原文件也不会损坏。

### `invalidate_cache() -> None`

**强制失效缓存**，下次 `load_data()` 将从磁盘重新读取。

```python
def invalidate_cache() -> None:
```

### `get_lock() -> threading.RLock`

**获取锁实例**，供外部需要保证读写原子性的场景使用（如 admin 路由中的读-改-写操作）。

```python
def get_lock() -> threading.RLock:
```

### `register_save_callback(callback) -> None`

**注册保存回调函数**，在 `save_data()` 成功后自动调用。

```python
def register_save_callback(callback: Callable[[], None]) -> None:
```

**用途**：外部模块可注册回调以响应数据变更。例如 `proxy_core.py` 注册了 `_invalidate_model_channels_cache()` 回调，在渠道数据变更时自动失效模型缓存。

## 为什么用 RLock？

使用 `threading.RLock`（可重入锁）而非普通 `Lock`，因为同一线程可能嵌套获取锁：

```python
# admin.py 中的典型用法
with get_lock():           # 第一次获取锁
    channels = _get_channels()   # _get_channels 内部调用 load_data()，也会获取锁
    # ... 修改 channels ...
    _save_channels(channels)     # _save_channels 内部调用 save_data()，也会获取锁
```

如果用普通 `Lock`，同一线程第二次获取锁会死锁。`RLock` 允许同一线程多次获取。

## 重要的设计决策

### 为什么 save_data() 后立即更新缓存？

`save_data()` 在写入磁盘后立即更新 `_cache`，这样后续的 `load_data()` 调用可以立即读到最新数据，无需等待 5 秒 TTL 过期。这是**写后读一致性**的保证。

### 为什么不能直接修改 channels.json？

如果你直接写 `channels.json` 文件（绕过 `save_data()`），内存缓存不会更新，`load_data()` 在 TTL 内仍返回旧数据，最多延迟 5 秒才能感知变更。**所有修改操作必须通过 `save_data()`**。

### 为什么缓存 TTL 是 5 秒？

这是一个权衡值：
- 太短（如 0）：每次 `load_data()` 都读磁盘，IO 压力大
- 太长（如 60）：外部修改 `channels.json` 后感知太慢
- 5 秒：对于渠道配置变更这种低频操作，5 秒延迟可接受

### 为什么用 JSON 文件而不是数据库？

项目面向小规模部署（几十个渠道），JSON 文件足够。优点是零依赖、易备份、易调试（直接打开文件查看）。

## API Keys 存储

### `load_api_keys() -> dict`

**读取 API Keys 数据**，与 `load_data()` 使用相同的缓存机制。

```python
def load_api_keys() -> dict[str, Any]
```

**存储格式**：

```json
{
  "api_keys": [
    {
      "id": "key_a1b2c3d4",
      "name": "生产环境",
      "key": "llmplug-api-xxx",
      "allowed_models": [],
      "notes": "",
      "request_count": 0,
      "total_input_tokens": 0,
      "total_output_tokens": 0,
      "created_at": "2024-01-01T00:00:00+00:00"
    }
  ]
}
```

### `save_api_keys(data) -> None`

**写入 API Keys 数据**，使用原子写入确保数据安全。

```python
def save_api_keys(data: dict[str, Any]) -> None
```

### `invalidate_keys_cache() -> None`

**强制失效 API Keys 缓存**，下次 `load_api_keys()` 将从磁盘重新读取。

```python
def invalidate_keys_cache() -> None
```

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DATA_DIR` | 项目根目录下 `data/` | 数据目录 |
| `CHANNELS_FILE` | `DATA_DIR/channels.json` | 渠道配置文件路径 |
| `API_KEYS_FILE` | `DATA_DIR/api_keys.json` | API Key 配置文件路径 |

## 文件操作安全

1. **原子写入**：`tempfile` + `os.replace()` 确保写入不会损坏原文件
2. **fsync**：`os.fsync()` 确保数据真正落盘
3. **临时文件清理**：写入失败时自动删除临时文件
4. **目录自动创建**：`_ensure_data_dir()` 使用 `os.makedirs(exist_ok=True)`
5. **文件自动创建**：`load_data()` 发现文件不存在时自动创建空 JSON
