# spec-storage — 存储层

> 对应文件：`storage.py`（约 305 行）

## 模块定位

`storage.py` 负责渠道数据、API Key 数据、模型组和负载均衡配置的持久化存储，使用 JSON 文件作为存储介质。它提供了异步安全的读写接口，并内置内存缓存以减少磁盘 IO。

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
_channels_lock: asyncio.Lock | None = None # 渠道数据异步锁
_keys_lock: asyncio.Lock | None = None     # API Keys 数据异步锁
_cache: dict | None = None    # 内存缓存
_cache_ts: float = 0          # 缓存时间戳
_CACHE_TTL = 5.0              # 缓存有效期（秒）
```

锁通过 `_get_channels_lock()` / `_get_keys_lock()` 懒加载创建，避免在模块加载时绑定到特定事件循环。

## 核心函数

### `load_data() -> dict`

**异步读取数据**，优先从内存缓存读取，缓存过期或不存在则从磁盘读取。

```python
async def load_data() -> dict[str, Any]
```

**流程**：

1. `_ensure_data_dir()` — 确保数据目录存在
2. `async with _get_channels_lock()` — 获取渠道异步锁
3. 双重检查缓存：在锁内再次检查 `_cache` 是否有效（`now - _cache_ts < _CACHE_TTL`）
4. 如果 `channels.json` 不存在 → 创建空文件 `{"channels": []}`（通过 `await asyncio.to_thread(_write_channels_to_disk, data)` 执行磁盘 IO）
5. 否则 `await asyncio.to_thread(_read_channels_from_disk)` → 读取 JSON
6. 更新 `_cache` 和 `_cache_ts`
7. 返回数据

**缓存 TTL**：5 秒。意味着直接修改 `channels.json` 文件后，最多需要 5 秒才能被 `load_data()` 感知到。

### `save_data(data) -> None`

**异步写入数据**，使用原子写入确保数据安全。

```python
async def save_data(data: dict[str, Any]) -> None
```

**流程**：

1. `_ensure_data_dir()` — 确保数据目录存在
2. `async with _get_channels_lock()` — 获取渠道异步锁
3. `await asyncio.to_thread(_write_channels_to_disk, data)` — 在线程池中执行磁盘 IO（创建临时文件 → 写入 JSON → `flush()` + `fsync()` → `os.replace()` 原子替换）
4. **立即更新内存缓存** `_cache = data`，`_cache_ts = now`
5. 触发保存回调 `_trigger_save_callbacks()`

**原子写入**：`os.replace()` 在 POSIX 系统上是原子操作，保证即使写入过程中崩溃，原文件也不会损坏。

### `invalidate_cache() -> None`

**强制失效缓存**，下次 `load_data()` 将从磁盘重新读取。同时失效模型组缓存和负载均衡配置缓存。

```python
async def invalidate_cache() -> None
```

### `_get_channels_lock() -> asyncio.Lock` / `_get_keys_lock() -> asyncio.Lock`

**懒加载获取异步锁**，首次调用时创建 `asyncio.Lock` 实例，后续调用返回同一实例。

```python
def _get_channels_lock() -> asyncio.Lock
def _get_keys_lock() -> asyncio.Lock
```

懒加载的原因：`asyncio.Lock` 在创建时绑定到当前事件循环，如果在模块加载时创建（此时事件循环可能尚未运行），会导致运行时错误。

### `register_save_callback(callback) -> None`

**注册保存回调函数**，在 `save_data()` 成功后自动调用。

```python
def register_save_callback(callback: Callable[[], None]) -> None:
```

**用途**：外部模块可注册回调以响应数据变更。例如 `proxy_core.py` 注册了 `_invalidate_model_channels_cache()` 回调，在渠道数据变更时自动失效模型缓存。

## 为什么用 asyncio.Lock 而不是 threading.RLock？

从 `threading.RLock` 迁移到 `asyncio.Lock`，因为 storage 现在是异步的：

- **`asyncio.Lock` 绑定到创建时的事件循环**，所以在模块加载时直接创建会导致锁绑定到错误的事件循环（模块加载时事件循环可能尚未运行）。因此使用懒加载模式（`_get_channels_lock()` / `_get_keys_lock()`），在首次调用时才创建锁，确保绑定到正确的事件循环。
- **不再需要 RLock 的可重入特性**。之前使用 `threading.RLock` 是因为 `load_data()` / `save_data()` 是同步函数，外部持锁后内部再次获取锁会死锁。改为异步后，`async with lock` 是协作式调度，异步函数天然不会在同一线程上嵌套获取锁——当 `load_data()` 在锁内 `await` 磁盘 IO 时，事件循环可以调度其他协程，但不会重入同一函数。

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

**异步读取 API Keys 数据**，与 `load_data()` 使用相同的缓存机制。

```python
async def load_api_keys() -> dict[str, Any]
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

**异步写入 API Keys 数据**，使用原子写入确保数据安全。

```python
async def save_api_keys(data: dict[str, Any]) -> None
```

### `invalidate_keys_cache() -> None`

**强制失效 API Keys 缓存**，下次 `load_api_keys()` 将从磁盘重新读取。

```python
async def invalidate_keys_cache() -> None
```

## 模型组存储

模型组数据存储在 `channels.json` 的 `model_groups` 字段中，使用与渠道数据相同的缓存模式（TTL + 缓存失效）。

### `load_model_groups() -> list[ModelGroup]`

**异步读取模型组列表**，优先从内存缓存读取。

```python
async def load_model_groups() -> list[ModelGroup]
```

内部通过 `await load_data()` 获取原始数据，然后从 `data.get("model_groups", [])` 解析为 `ModelGroup` 列表。

### `save_model_groups(groups) -> None`

**异步写入模型组列表**，将模型组序列化后写入 `channels.json` 的 `model_groups` 字段。

```python
async def save_model_groups(groups: list[ModelGroup]) -> None
```

流程：`await load_data()` → 修改 `data["model_groups"]` → `await save_data(data)` → 更新内存缓存。

### `get_model_group_by_name(name) -> ModelGroup | None`

**按名称查找已启用的模型组**。

```python
async def get_model_group_by_name(name: str) -> ModelGroup | None
```

### `add_model_group(group) -> ModelGroup`

**添加模型组**，追加到现有列表后保存。

```python
async def add_model_group(group: ModelGroup) -> ModelGroup
```

### `update_model_group(group_id, updates) -> ModelGroup | None`

**更新模型组**，根据 `group_id` 查找并应用 `updates` 字典，返回更新后的模型组，未找到返回 `None`。

```python
async def update_model_group(group_id: str, updates: dict) -> ModelGroup | None
```

### `delete_model_group(group_id) -> bool`

**删除模型组**，成功返回 `True`，未找到返回 `False`。

```python
async def delete_model_group(group_id: str) -> bool
```

### `invalidate_model_groups_cache() -> None`

**强制失效模型组缓存**，下次 `load_model_groups()` 将从磁盘重新读取。

```python
async def invalidate_model_groups_cache() -> None
```

## 负载均衡配置存储

负载均衡配置存储在 `channels.json` 的 `lb_config` 字段中，使用相同的缓存模式。

### `get_lb_config() -> LBConfig`

**异步读取负载均衡配置**，优先从内存缓存读取。

```python
async def get_lb_config() -> LBConfig
```

内部通过 `await load_data()` 获取原始数据，从 `data.get("lb_config", {})` 解析为 `LBConfig`。

### `save_lb_config(cfg) -> None`

**异步写入负载均衡配置**，将配置序列化后写入 `channels.json` 的 `lb_config` 字段。

```python
async def save_lb_config(cfg: LBConfig) -> None
```

流程：`await load_data()` → 修改 `data["lb_config"]` → `await save_data(data)` → 更新内存缓存。

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
