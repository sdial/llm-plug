# 移除 PostgreSQL 支持，仅保留 SQLite3 后端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从代码、配置、前端、依赖、测试和文档中彻底移除 PostgreSQL / `asyncpg` 相关入口，请求记录模块仅保留 SQLite3 实现。

**Architecture:** 删除 `request_logs.py` 中的 `_BaseRequestLogBackend` 抽象类与 `PostgresRequestLogBackend` 实现，直接构造 `SQLiteRequestLogBackend`；从 `config.py` 中删除 `request_log_db_type` 和 `request_log_database_url` 配置项；前端设置页移除数据库类型下拉框与 PostgreSQL 连接串输入框；`pyproject.toml` 移除 `asyncpg` 依赖；同步调整相关测试与项目文档。

**Tech Stack:** Python 3.10+ / FastAPI / SQLite3 / pytest / ruff

---

## File Structure

| 文件 | 职责变更 |
|------|----------|
| `request_logs.py` | 删除 PG 后端、抽象基类、PG 字段映射；`_RAW_FIELD_SELECT_SQLITE` 重命名为 `_RAW_FIELD_SELECT`；添加 `BACKEND = "sqlite3"` 常量；更新类型注解 |
| `config.py` | 删除 `request_log_db_type`、`request_log_database_url`、`_mask_db_url()` 及相关约束/脱敏逻辑 |
| `static/fragments/admin/settings.html` | 删除数据库类型下拉框与 PostgreSQL 连接串输入框 |
| `static/js/settings.js` | 删除 `syncRequestLogDbMode()` 及相关读写逻辑 |
| `pyproject.toml` | 删除 `"asyncpg>=0.29.0"` |
| `uv.lock` | 运行 `uv lock` 重新生成 |
| `tests/test_request_logs.py` | 删除 PG 测试、调整 reload 测试为非法 SQLite 路径 |
| `tests/test_settings.py` | 删除脱敏测试、调整默认值/重启/页面控件断言 |
| `tests/routers/test_admin.py` | 移除 fixture 中 `request_log_db_type` |
| `tests/routers/test_admin_whitelist.py` | 移除 fixture 中 `request_log_db_type` |
| `tests/test_sqlite_hygiene.py` | 移除 fixture 中 `request_log_db_type` |
| `README.md` | 更新描述为「请求记录使用 SQLite3」 |
| `agents.md` | 移除「可在设置页切到 PostgreSQL」描述 |
| `docs/modules.md` | 删除 PG 配置行，request_logs.py 说明改为仅 SQLite |
| `docs/deployment.md` | 删除「创建 PostgreSQL 数据库」章节 |
| `docs/troubleshooting.md` | 删除「请求记录 PostgreSQL 连接失败」章节 |
| `docs/architecture.md` | request_logs.py 说明改为「SQLite3 后端 + 异步队列写入」 |

---

## Task 1: 修改 `request_logs.py` 删除 PostgreSQL 后端与抽象基类

**Files:**
- Modify: `request_logs.py`
- Test: `tests/test_request_logs.py`（后续 Task 5 调整）

- [ ] **Step 1.1: 删除 `TYPE_CHECKING` 中的 `asyncpg` 导入**

```python
# 删除以下块
if TYPE_CHECKING:
    import asyncpg
```

Run: `uv run ruff check request_logs.py`
Expected: no errors yet（此时还有其他待删代码）

- [ ] **Step 1.2: 在模块顶部添加 `BACKEND = "sqlite3"` 常量**

在 `_SQLITE_MMAP_SIZE_BYTES` 常量之前插入：

```python
# 请求记录仅支持 SQLite3，不再扩展其他关系型数据库后端。
BACKEND = "sqlite3"
```

- [ ] **Step 1.3: 重命名 `_RAW_FIELD_SELECT_SQLITE` 并删除 `_RAW_FIELD_SELECT_POSTGRES`**

将：

```python
_RAW_FIELD_SELECT_SQLITE: dict[str, str] = {
    "request_headers": "SELECT request_headers FROM request_logs WHERE id = ?",
    "response_headers": "SELECT response_headers FROM request_logs WHERE id = ?",
    "request_body": "SELECT request_body FROM request_logs WHERE id = ?",
    "response_body": "SELECT response_body FROM request_logs WHERE id = ?",
}

_RAW_FIELD_SELECT_POSTGRES: dict[str, str] = {
    "request_headers": "SELECT request_headers FROM request_logs WHERE id = $1",
    "response_headers": "SELECT response_headers FROM request_logs WHERE id = $1",
    "request_body": "SELECT request_body FROM request_logs WHERE id = $1",
    "response_body": "SELECT response_body FROM request_logs WHERE id = $1",
}
```

改为：

```python
_RAW_FIELD_SELECT: dict[str, str] = {
    "request_headers": "SELECT request_headers FROM request_logs WHERE id = ?",
    "response_headers": "SELECT response_headers FROM request_logs WHERE id = ?",
    "request_body": "SELECT request_body FROM request_logs WHERE id = ?",
    "response_body": "SELECT response_body FROM request_logs WHERE id = ?",
}
```

- [ ] **Step 1.4: 将 `_get_request_field_sync` 中使用的 `_RAW_FIELD_SELECT_SQLITE` 改为 `_RAW_FIELD_SELECT`**

```python
# 查找
sql = _RAW_FIELD_SELECT_SQLITE.get(field)
# 替换为
sql = _RAW_FIELD_SELECT.get(field)
```

- [ ] **Step 1.5: 删除 `_BaseRequestLogBackend` 抽象类**

删除整个 `class _BaseRequestLogBackend:`（约 246-275 行）。

- [ ] **Step 1.6: 将 `SQLiteRequestLogBackend` 的基类继承移除**

将：

```python
class SQLiteRequestLogBackend(_BaseRequestLogBackend):
```

改为：

```python
class SQLiteRequestLogBackend:
```

- [ ] **Step 1.7: 删除 `PostgresRequestLogBackend` 类**

删除从 `class PostgresRequestLogBackend(_BaseRequestLogBackend):` 开始到 `_build_backend` 函数之前的所有代码（约 783-1022 行）。

- [ ] **Step 1.8: 重写 `_build_backend` 函数**

将：

```python
def _build_backend(settings: dict | None = None) -> _BaseRequestLogBackend:
    db_type = str(_get_setting(settings, "request_log_db_type") or "sqlite").lower()
    if db_type == "sqlite":
        db_path = _get_setting(settings, "request_log_sqlite_path")
        if not db_path:
            db_path = os.path.join(config.DATA_DIR, "request_logs.db")
        return SQLiteRequestLogBackend(str(db_path))
    if db_type == "postgres":
        return PostgresRequestLogBackend(str(_get_setting(settings, "request_log_database_url") or ""))
    raise ValueError(f"unsupported request_log_db_type: {db_type}")
```

改为：

```python
def _build_backend(settings: dict | None = None) -> SQLiteRequestLogBackend:
    db_path = _get_setting(settings, "request_log_sqlite_path")
    if not db_path:
        db_path = os.path.join(config.DATA_DIR, "request_logs.db")
    return SQLiteRequestLogBackend(str(db_path))
```

- [ ] **Step 1.9: 更新模块级类型注解**

将：

```python
_backend: "_BaseRequestLogBackend | None" = None
```

改为：

```python
_backend: SQLiteRequestLogBackend | None = None
```

将 `_create_initialized_backend` 的签名：

```python
async def _create_initialized_backend(settings: dict | None = None) -> tuple[_BaseRequestLogBackend | None, dict]:
    backend: _BaseRequestLogBackend | None = None
```

改为：

```python
async def _create_initialized_backend(settings: dict | None = None) -> tuple[SQLiteRequestLogBackend | None, dict]:
    backend: SQLiteRequestLogBackend | None = None
```

- [ ] **Step 1.10: 运行 lint 并运行 request_logs 相关测试**

Run: `uv run ruff check request_logs.py`
Expected: no errors

Run: `uv run pytest tests/test_request_logs.py::test_sqlite_backend_initializes_writes_and_lists_paginated tests/test_request_logs.py::test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails -v`
Expected: 此时 `test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails` 仍会失败（因为还传 postgres 配置），Task 5 会修复。

- [ ] **Step 1.11: Commit**

```bash
git add request_logs.py
git commit -m "feat(request_logs): 删除 PostgreSQL 后端，仅保留 SQLite3"
```

---

## Task 2: 修改 `config.py` 删除 PostgreSQL 相关配置项与校验

**Files:**
- Modify: `config.py`
- Test: `tests/test_settings.py`（后续 Task 5 调整）

- [ ] **Step 2.1: 从 `_CONFIG_SCHEMA` 删除 `request_log_db_type` 和 `request_log_database_url`**

删除以下条目：

```python
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
```

保留 `request_log_sqlite_path`。

- [ ] **Step 2.2: 从 `_CONFIG_CONSTRAINTS` 删除 `request_log_db_type` choices**

删除：

```python
    "request_log_db_type": {"choices": ("sqlite", "postgres")},
```

- [ ] **Step 2.3: 删除 `_mask_db_url` 函数和 `get_settings` 中的脱敏逻辑**

删除：

```python
def _mask_db_url(url: str) -> str:
    return re.sub(r'://([^:]+):([^@]+)@', r'://\1:***@', url)
```

并将 `get_settings()` 改为：

```python
def get_settings() -> dict:
    return dict(_settings)
```

- [ ] **Step 2.4: 确认 `re` 和 `tempfile`/`contextlib` 导入仍被使用**

`re` 在删除 `_mask_db_url` 后不再使用，因此删除 `import re`。

- [ ] **Step 2.5: 运行 lint**

Run: `uv run ruff check config.py`
Expected: no errors

- [ ] **Step 2.6: Commit**

```bash
git add config.py
git commit -m "feat(config): 删除 PostgreSQL 请求记录配置项与脱敏逻辑"
```

---

## Task 3: 修改前端设置页移除 PostgreSQL 相关控件

**Files:**
- Modify: `static/fragments/admin/settings.html`
- Modify: `static/js/settings.js`
- Test: `tests/test_settings.py`（后续 Task 5 调整）

- [ ] **Step 3.1: 在 `settings.html` 中删除数据库类型下拉框**

删除以下卡片块：

```html
            <div class="card p-5">
              <div class="text-sm font-medium text-ink-900 mb-1.5">数据库类型</div>
              <select id="set_request_log_db_type" data-section="database" onchange="syncRequestLogDbMode()" class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
                <option value="sqlite">SQLite</option>
                <option value="postgres">PostgreSQL</option>
              </select>
              <p class="text-xs text-ink-400 mt-1.5">SQLite 适合单机部署，开箱即用；PostgreSQL 适合多实例共享或需要外部访问的场景。切换后立即生效，已有记录不会迁移。</p>
            </div>
```

- [ ] **Step 3.2: 在 `settings.html` 中删除 PostgreSQL 连接串输入框**

删除以下卡片块：

```html
          <div class="card p-5 mt-4">
            <div class="flex items-center gap-2 mb-1.5">
              <span class="text-sm font-medium text-ink-900">PostgreSQL 连接串</span>
              <span class="pill pill-success">热更新</span>
            </div>
            <input type="text" id="set_request_log_database_url" data-section="database" class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white font-mono" placeholder="留空保持不变">
            <p class="text-xs text-ink-400 mt-1">格式：<span class="font-mono">postgresql://用户名:密码@主机:端口/数据库名</span>。出于安全考虑，页面只展示脱敏后的值，修改时需重新输入完整连接串。</p>
          </div>
```

- [ ] **Step 3.3: 调整 `settings.html` 中数据库说明文字**

将「请求记录数据库」区域顶部说明改为：

```html
            <p class="text-sm text-ink-600 mt-1">请求记录使用 SQLite3，默认保存在 <span class="font-mono">data/request_logs.db</span>，按月分库存储。统计聚合单独保存在 <span class="font-mono">data/stats.db</span>。</p>
```

保留「SQLite 文件路径」只读输入框和「原始信息保存」区域。

- [ ] **Step 3.4: 在 `settings.js` 中删除 `syncRequestLogDbMode` 函数**

删除：

```javascript
function syncRequestLogDbMode() {
  const typeEl = document.getElementById('set_request_log_db_type');
  const pgUrlEl = document.getElementById('set_request_log_database_url');
  if (!typeEl || !pgUrlEl) return;
  const usingPostgres = typeEl.value === 'postgres';
  pgUrlEl.disabled = !usingPostgres;
  pgUrlEl.classList.toggle('bg-surface-50', !usingPostgres);
  pgUrlEl.classList.toggle('text-ink-500', !usingPostgres);
  pgUrlEl.classList.toggle('cursor-not-allowed', !usingPostgres);
  pgUrlEl.placeholder = usingPostgres ? 'postgresql://user:pass@host:5432/db' : 'SQLite 模式下无需填写';
}
```

- [ ] **Step 3.5: 在 `settings.js` 中移除 `Object.assign` 导出的 `syncRequestLogDbMode`**

将：

```javascript
Object.assign(window, {
    switchSettingsSection,
    initSettings,
    syncRequestLogDbMode,
    syncLbStrategyMode,
    loadSettings,
    saveSettings,
    restartServer,
    loadFormatConversionPanel,
});
```

改为：

```javascript
Object.assign(window, {
    switchSettingsSection,
    initSettings,
    syncLbStrategyMode,
    loadSettings,
    saveSettings,
    restartServer,
    loadFormatConversionPanel,
});
```

- [ ] **Step 3.6: 在 `initSettings` 中删除对 `syncRequestLogDbMode` 的调用**

删除 `initSettings` 函数中的：

```javascript
  syncRequestLogDbMode();
```

- [ ] **Step 3.7: 在 `loadSettings` 中删除 PG 相关字段的读写**

删除：

```javascript
    document.getElementById('set_request_log_db_type').value = data.request_log_db_type || 'sqlite';
```

和：

```javascript
    document.getElementById('set_request_log_database_url').value = data.request_log_database_url_masked || '';
```

以及：

```javascript
    syncRequestLogDbMode();
```

- [ ] **Step 3.8: 在 `_detectSettingsDirty` 中删除 PG 相关逻辑**

删除：

```javascript
  const requestLogDbType = document.getElementById('set_request_log_db_type').value;
  if (requestLogDbType !== (orig.request_log_db_type || 'sqlite')) _settingsDirtySections.add('database');
  const requestLogDbUrl = document.getElementById('set_request_log_database_url').value;
  if (requestLogDbType === 'postgres' && requestLogDbUrl && requestLogDbUrl !== (orig.request_log_database_url_masked || '')) _settingsDirtySections.add('database');
```

- [ ] **Step 3.9: 在 `saveSettings` 中删除 PG 相关保存逻辑**

删除：

```javascript
  const requestLogDbType = document.getElementById('set_request_log_db_type').value;
  if (requestLogDbType !== (orig.request_log_db_type || 'sqlite')) data.request_log_db_type = requestLogDbType;
  const requestLogDbUrl = document.getElementById('set_request_log_database_url').value;
  if (requestLogDbType === 'postgres' && requestLogDbUrl && requestLogDbUrl !== (orig.request_log_database_url_masked || '')) data.request_log_database_url = requestLogDbUrl;
```

- [ ] **Step 3.10: 运行 lint 检查 HTML/JS（ruff 不检查 JS，只做 Python lint）**

Run: `uv run ruff check .`
Expected: no errors

- [ ] **Step 3.11: Commit**

```bash
git add static/fragments/admin/settings.html static/js/settings.js
git commit -m "feat(admin): 设置页移除 PostgreSQL 数据库切换控件"
```

---

## Task 4: 更新 `pyproject.toml` 并重新生成 `uv.lock`

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`（通过 `uv lock` 重新生成）

- [ ] **Step 4.1: 删除 `asyncpg` 依赖**

将 `pyproject.toml` 的 `dependencies` 列表中的 `"asyncpg>=0.29.0",` 删除。

- [ ] **Step 4.2: 重新生成 `uv.lock`**

Run: `uv lock`
Expected: 成功，无报错

- [ ] **Step 4.3: 确认 `uv.lock` 中不再有 `asyncpg` 包**

Run: `grep -c "asyncpg" uv.lock`（或 `Select-String -Path uv.lock -Pattern "asyncpg"`）
Expected: 0

- [ ] **Step 4.4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): 移除 asyncpg 依赖"
```

---

## Task 5: 调整测试

**Files:**
- Modify: `tests/test_request_logs.py`
- Modify: `tests/test_settings.py`
- Modify: `tests/test_sqlite_hygiene.py`
- Modify: `tests/routers/test_admin.py`
- Modify: `tests/routers/test_admin_whitelist.py`

- [ ] **Step 5.1: 更新 `tests/test_request_logs.py` 中所有 fixture 的 `init_backend` 调用**

将 fixture `sqlite_request_logs` 和测试中所有：

```python
        {
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(db_path),
        }
```

改为：

```python
        {
            "request_log_sqlite_path": str(db_path),
        }
```

需要修改的位置：
- `sqlite_request_logs` fixture（约 58-63 行）
- `test_sqlite_backend_migrates_existing_db_for_cache_token_columns`（约 165-169 行）
- `test_migrate_legacy_db_streams_rows_in_batches`（约 257-261 行）
- `test_migrate_legacy_db_idempotent`（约 341-344 行）

- [ ] **Step 5.2: 删除 `test_postgres_backend_smoke` 测试**

删除整个 `async def test_postgres_backend_smoke(monkeypatch):` 函数（约 489-526 行）。

- [ ] **Step 5.3: 调整 `test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails`**

将原测试：

```python
async def test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails(sqlite_request_logs):
    _sample_record(channel_id="ch_keep", channel_name="Keep")
    await request_logs.drain_queue()

    result = await request_logs.reload_backend(
        {
            "request_log_db_type": "postgres",
            "request_log_database_url": "",
        }
    )
    listed = await request_logs.list_requests()

    assert result["available"] is False
    assert "error" in result
    assert listed["available"] is True
    assert listed["total"] == 1
    assert listed["items"][0]["channel_id"] == "ch_keep"
```

改为（使用非法 SQLite 路径触发初始化失败）：

```python
async def test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails(sqlite_request_logs):
    _sample_record(channel_id="ch_keep", channel_name="Keep")
    await request_logs.drain_queue()

    # 空路径无法创建 SQLite 数据库，触发 init 失败
    result = await request_logs.reload_backend({"request_log_sqlite_path": ""})
    listed = await request_logs.list_requests()

    assert result["available"] is False
    assert "error" in result
    assert listed["available"] is True
    assert listed["total"] == 1
    assert listed["items"][0]["channel_id"] == "ch_keep"
```

- [ ] **Step 5.4: 更新 `tests/test_settings.py` 删除 `test_get_settings_masks_db_url` 测试**

删除整个 `def test_get_settings_masks_db_url():` 函数（约 83-95 行）。

- [ ] **Step 5.5: 调整 `tests/test_settings.py` 中 `test_config_defaults` 断言**

将：

```python
    assert _CONFIG_SCHEMA["request_log_db_type"]["default"] == "sqlite"
    assert os.path.basename(_CONFIG_SCHEMA["request_log_sqlite_path"]["default"]) == "request_logs.db"
    assert _CONFIG_SCHEMA["request_log_database_url"]["default"] == ""
```

改为：

```python
    assert os.path.basename(_CONFIG_SCHEMA["request_log_sqlite_path"]["default"]) == "request_logs.db"
    assert "request_log_db_type" not in _CONFIG_SCHEMA
    assert "request_log_database_url" not in _CONFIG_SCHEMA
```

- [ ] **Step 5.6: 调整 `tests/test_settings.py` 中 `test_config_requires_restart` 断言**

删除以下断言：

```python
    assert "request_log_db_type" not in restart_keys
    assert "request_log_sqlite_path" not in restart_keys
    assert "request_log_database_url" not in restart_keys
```

（因为配置项已删除，这些断言失去意义；可以保留 `request_log_sqlite_path` 不在列表中的断言，但删除 PG 相关两项即可。）

- [ ] **Step 5.7: 调整 `tests/test_settings.py` 中 `test_settings_page_has_request_log_db_controls` 断言**

将测试：

```python
def test_settings_page_has_request_log_db_controls():
    """Settings page exposes request-log DB switching and lightweight fallback."""
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")
    requests_js = Path("static/js/requests.js").read_text(encoding="utf-8")

    assert "set_request_log_db_type" in html
    assert "set_request_log_sqlite_path" in html
    assert 'id="set_request_log_sqlite_path"' in html and "readonly" in html
    assert "set_request_log_database_url" in html
    assert "syncRequestLogDbMode" in html
    assert "set_save_request_headers" in html
    assert "set_save_response_headers" in html
    assert "set_save_request_body" in html
    assert "set_save_response_body" in html
    assert "loadStatsRequestLogs" in requests_js
    assert "params.set('source', 'stats')" in requests_js
```

改为：

```python
def test_settings_page_has_request_log_db_controls():
    """Settings page exposes SQLite request-log path and raw-field toggles."""
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")
    requests_js = Path("static/js/requests.js").read_text(encoding="utf-8")

    assert "set_request_log_sqlite_path" in html
    assert 'id="set_request_log_sqlite_path"' in html and "readonly" in html
    assert "set_request_log_db_type" not in html
    assert "set_request_log_database_url" not in html
    assert "syncRequestLogDbMode" not in html
    assert "set_save_request_headers" in html
    assert "set_save_response_headers" in html
    assert "set_save_request_body" in html
    assert "set_save_response_body" in html
    assert "loadStatsRequestLogs" in requests_js
    assert "params.set('source', 'stats')" in requests_js
```

- [ ] **Step 5.8: 更新 `tests/test_sqlite_hygiene.py` 的 fixture**

将 `request_logs_db` fixture 中：

```python
    result = await request_logs.init_backend(
        {
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(db_path),
        }
    )
```

改为：

```python
    result = await request_logs.init_backend(
        {
            "request_log_sqlite_path": str(db_path),
        }
    )
```

- [ ] **Step 5.9: 更新 `tests/routers/test_admin.py` 的 fixture**

将 `setup_test_db` fixture 中：

```python
    await request_logs.init_backend(
        {
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(tmp_path / "request_logs.db"),
        }
    )
```

改为：

```python
    await request_logs.init_backend(
        {
            "request_log_sqlite_path": str(tmp_path / "request_logs.db"),
        }
    )
```

- [ ] **Step 5.10: 更新 `tests/routers/test_admin_whitelist.py` 的 fixture**

与 Step 5.9 相同修改。

- [ ] **Step 5.11: 调整 `tests/routers/test_admin.py` 中 `test_update_settings_reloads_request_log_backend` 测试**

因为 `request_log_db_type` 已删除，前端不会再传该字段。但 `reload_backend` 仍会在任何 setting 更新后被 `admin.py` 调用。测试需要将请求体改为一个仍然合法的字段，例如 `request_timeout`：

```python
        resp = await client.put(
            "/admin/settings",
            json={"request_timeout": 600},
        )
```

- [ ] **Step 5.12: 运行测试**

Run: `uv run pytest tests/test_request_logs.py tests/test_settings.py tests/test_sqlite_hygiene.py tests/routers/test_admin.py tests/routers/test_admin_whitelist.py -v`
Expected: PASS（可能有些测试在 Step 1 已经失败，此时应全部通过）

- [ ] **Step 5.13: Commit**

```bash
git add tests/
git commit -m "test: 移除 PostgreSQL 相关测试与 fixture 配置"
```

---

## Task 6: 更新项目文档

**Files:**
- Modify: `README.md`
- Modify: `agents.md`
- Modify: `docs/modules.md`
- Modify: `docs/deployment.md`
- Modify: `docs/troubleshooting.md`
- Modify: `docs/architecture.md`

- [ ] **Step 6.1: 更新 `README.md`**

将第 12 行：

```markdown
- **请求记录与统计**：默认 SQLite，本地持久化，也可在前端切换请求记录到 PostgreSQL
```

改为：

```markdown
- **请求记录与统计**：请求记录使用 SQLite3，本地持久化，按月分库
```

将技术栈表格中：

```markdown
| 存储 | JSON 文件 / SQLite / PostgreSQL 请求记录 |
```

改为：

```markdown
| 存储 | JSON 文件 / SQLite3 |
```

- [ ] **Step 6.2: 更新 `agents.md`**

将第 33 行：

```markdown
| `data/request_logs.db` | SQLite 请求记录（可在设置页切到 PostgreSQL；SQLite 模式下按月分库） |
```

改为：

```markdown
| `data/request_logs.db` | SQLite3 请求记录（按月分库） |
```

- [ ] **Step 6.3: 更新 `docs/modules.md` 中 `_CONFIG_SCHEMA` 表格**

删除表格中的两行：

```markdown
| `request_log_db_type` | str | `"sqlite"` | 否 | 请求记录后端：`sqlite` 或 `postgres` |
| `request_log_database_url` | str | `""` | 否 | PostgreSQL 连接串（切换到 postgres 时使用） |
```

- [ ] **Step 6.4: 更新 `docs/modules.md` 中「请求记录」章节**

将：

```markdown
记录每条代理请求的详细信息（token 用量、延迟、错误、请求/响应头等），支持 SQLite 和 PostgreSQL 两种后端。
```

改为：

```markdown
记录每条代理请求的详细信息（token 用量、延迟、错误、请求/响应头等），仅支持 SQLite3 后端。
```

将后端实现表格：

```markdown
| 后端 | 说明 |
|------|------|
| `SQLiteRequestLogBackend` | 默认，使用 WAL 模式 + 64MB mmap |
| `PostgresRequestLogBackend` | 可选，连接池 min=1 max=5，JSONB 存储 |
```

改为：

```markdown
| 后端 | 说明 |
|------|------|
| `SQLiteRequestLogBackend` | 唯一后端，使用 WAL 模式 + 64MB mmap |
```

删除「两种后端共享相同的 `_BaseRequestLogBackend` 接口...」这句话。

- [ ] **Step 6.5: 更新 `docs/deployment.md` 删除 PostgreSQL 章节**

删除「请求记录数据库」一整个二级章节（从 `## 请求记录数据库` 到 `## 安全建议` 之前），约 343-365 行。

同时删除「故障恢复」小节中的「PostgreSQL 重连」段落：

```markdown
### PostgreSQL 重连

数据库连接断开时会自动重连（异步队列 worker 每次写入时检测连接状态），无需重启服务。
```

- [ ] **Step 6.6: 更新 `docs/troubleshooting.md` 删除 PostgreSQL 章节**

删除「数据库问题」二级章节下的「请求记录 PostgreSQL 连接失败」三级章节（约 253-270 行）。

- [ ] **Step 6.7: 更新 `docs/architecture.md` 中 `request_logs.py` 描述**

将：

```markdown
├── request_logs.py      # 请求记录：SQLite/PostgreSQL 后端 + 异步队列写入
```

改为：

```markdown
├── request_logs.py      # 请求记录：SQLite3 后端 + 异步队列写入
```

- [ ] **Step 6.8: 检查文档一致性**

Run: `grep -R "postgres\|PostgreSQL\|request_log_db_type\|request_log_database_url" README.md agents.md docs/`（或在 PowerShell 中用 `Select-String`）
Expected: 无匹配（历史 spec/plan 文件不在此范围内）

- [ ] **Step 6.9: Commit**

```bash
git add README.md agents.md docs/
git commit -m "docs: 移除 PostgreSQL 相关描述，明确仅支持 SQLite3"
```

---

## Task 7: 运行全量测试与 lint 验证

**Files:**
- All of the above

- [ ] **Step 7.1: 运行全量测试**

Run: `uv run pytest`
Expected: 全部通过

- [ ] **Step 7.2: 运行 lint**

Run: `uv run ruff check .`
Expected: 无新增告警

- [ ] **Step 7.3: 手动验证设置页**

Run: `uv run python main.py --no-reload`
打开浏览器访问 `http://localhost:55555/admin`，进入「设置」→「数据库」部分，确认：
- 没有「数据库类型」下拉框
- 没有「PostgreSQL 连接串」输入框
- 保留「SQLite 文件路径」只读输入框

（由于这是计划文档，实际验证由执行时代理完成。）

- [ ] **Step 7.4: Commit（如验证通过）**

```bash
git add -A
git commit -m "chore: 验证通过，移除 PostgreSQL 支持完成"
```

---

## Self-Review

### 1. Spec coverage

| Spec 要求 | 对应任务 |
|-----------|----------|
| 删除 `request_logs.py` 中的 PostgreSQL 后端 | Task 1 |
| 删除 `_BaseRequestLogBackend` 抽象类 | Task 1.5 / 1.6 |
| 删除 `_RAW_FIELD_SELECT_POSTGRES` 并重命名 SQLite 版本 | Task 1.3 / 1.4 |
| 添加 `BACKEND = "sqlite3"` 常量 | Task 1.2 |
| 更新 `_build_backend` 类型注解 | Task 1.8 / 1.9 |
| 删除 `config.py` 中 `request_log_db_type` / `request_log_database_url` | Task 2 |
| 删除 `_mask_db_url` | Task 2.3 |
| 前端删除数据库类型下拉框和 PG 连接串 | Task 3 |
| 删除 `syncRequestLogDbMode` 和相关读写 | Task 3 |
| `pyproject.toml` 删除 `asyncpg` 并重新生成 `uv.lock` | Task 4 |
| 测试删除 `test_postgres_backend_smoke` | Task 5.2 |
| 测试调整 reload 失败场景 | Task 5.3 |
| 文档更新 README / agents / modules / deployment / troubleshooting / architecture | Task 6 |
| 运行 pytest / ruff 验证 | Task 7 |

无遗漏。

### 2. Placeholder scan

- 无 "TBD"/"TODO"/"implement later"
- 无 "Add appropriate error handling" 类模糊描述
- 每个代码步骤包含实际代码片段
- 无 "Similar to Task N" 引用

### 3. Type consistency

- `SQLiteRequestLogBackend` 作为唯一后端类贯穿始终
- `_backend` / `_create_initialized_backend` 返回类型统一为 `SQLiteRequestLogBackend | None`
- `get_settings()` 返回普通 `dict`，不再包含 `request_log_database_url_masked`
- 前端 `loadSettings` / `saveSettings` / `_detectSettingsDirty` 不再引用已删除字段

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-28-remove-pgsql-keep-sqlite.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
