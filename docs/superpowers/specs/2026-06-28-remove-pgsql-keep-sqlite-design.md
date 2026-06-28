# 移除 PostgreSQL 支持，仅保留 SQLite3 后端

**日期**: 2026-06-28  
**状态**: 已批准，待实现  
**范围**: 全项目移除 PostgreSQL / `asyncpg` 相关代码与配置，请求记录模块仅保留 SQLite3 实现。

---

## 1. 目标

- 完全移除 `request_logs.py` 中的 PostgreSQL 后端。
- 从配置体系、前端设置页、依赖、测试、文档中删除所有 PostgreSQL 相关入口。
- 在代码中显式声明：请求记录仅支持 SQLite3，不再预留其他关系型数据库扩展点。

## 2. 背景

当前项目处于开发阶段，没有生产用户。`request_logs.py` 通过 `_BaseRequestLogBackend` 抽象接口同时维护 `SQLiteRequestLogBackend` 与 `PostgresRequestLogBackend`，`config.py` 提供 `request_log_db_type`（`sqlite`/`postgres`）和 `request_log_database_url`，前端设置页支持切换，并依赖 `asyncpg` 包。

由于维护目标是单机/轻量部署，PostgreSQL 后端不再必要，且增加了依赖、测试和文档负担。

## 3. 设计决策

| 决策 | 说明 |
|------|------|
| 彻底硬删除 | 不保留 `_BaseRequestLogBackend` 抽象，也不保留 `request_log_db_type` 等配置项，避免死代码。 |
| 无向后兼容 | 不迁移旧 PG 数据、不保留降级路径；旧 `settings.json` 中的相关键会被忽略并在下次保存时清理。 |
| 显式声明 | `request_logs.py` 顶部添加 `BACKEND = "sqlite3"` 常量与注释，类名保持 `SQLiteRequestLogBackend`。 |
| 文档更新 | 改写 README、agents.md、modules.md、deployment.md、troubleshooting.md、architecture.md；历史 spec 文件保持归档。 |

## 4. 代码变更清单

### 4.1 `request_logs.py`

- 删除 `if TYPE_CHECKING: import asyncpg`。
- 删除 `_RAW_FIELD_SELECT_POSTGRES`。
- 删除 `_BaseRequestLogBackend` 抽象类。
- 删除 `PostgresRequestLogBackend` 类及其全部 SQL、连接池、JSONB 处理逻辑。
- 删除 `_build_backend` 中的数据库类型分支，改为直接构造 `SQLiteRequestLogBackend`。
- 保留 `_BaseRequestLogBackend` 原本定义的公共方法签名由 `SQLiteRequestLogBackend` 直接实现。

简化后的 `_build_backend`：

```python
def _build_backend(settings: dict | None = None) -> SQLiteRequestLogBackend:
    db_path = _get_setting(settings, "request_log_sqlite_path")
    if not db_path:
        db_path = os.path.join(config.DATA_DIR, "request_logs.db")
    return SQLiteRequestLogBackend(str(db_path))
```

### 4.2 `config.py`

- 从 `_CONFIG_SCHEMA` 删除：
  - `request_log_db_type`
  - `request_log_database_url`
- 保留 `request_log_sqlite_path`（可展示为只读路径）。
- 从 `_CONFIG_CONSTRAINTS` 删除 `request_log_db_type` 的 choices。
- 从 `get_settings()` 删除 `request_log_database_url` 脱敏逻辑和 `request_log_database_url_masked` 字段。

### 4.3 前端设置页

`static/fragments/admin/settings.html`：
- 删除「数据库类型」`<select id="set_request_log_db_type">`。
- 删除「PostgreSQL 连接串」`<input id="set_request_log_database_url">`。
- 保留 SQLite 路径只读输入框与说明文字。

`static/js/settings.js`：
- 删除 `syncRequestLogDbMode()` 函数。
- 删除 `loadSettings`、`_detectSettingsDirty`、`saveSettings` 中对 `request_log_db_type` 和 `request_log_database_url` 的读写。
- `initSettings()` 中不再调用 `syncRequestLogDbMode()`。

### 4.4 测试

- `tests/test_request_logs.py`：
  - 删除 `test_postgres_backend_smoke`。
  - 调整 `test_reload_backend_keeps_old_sqlite_backend_when_new_init_fails`，改用非法 SQLite 路径触发初始化失败，而非 postgres 配置。
- `tests/test_settings.py`：
  - 删除 `test_get_settings_masks_db_url`。
  - 调整 `test_config_defaults`、`test_config_requires_restart`、`test_settings_page_has_request_log_db_controls` 中关于已删除字段的断言。
- `tests/routers/test_admin.py`、`tests/routers/test_admin_whitelist.py`、`tests/test_sqlite_hygiene.py`：
  - 移除 fixture 中不再需要的 `request_log_db_type: "sqlite"`。

### 4.5 依赖

- `pyproject.toml` 删除 `"asyncpg>=0.29.0"`。
- 运行 `uv lock` 重新生成 `uv.lock`。

### 4.6 文档

| 文档 | 修改 |
|------|------|
| `README.md` | 「也可切换请求记录到 PostgreSQL」改为「请求记录使用 SQLite3」。 |
| `agents.md` | 移除「可在设置页切到 PostgreSQL」描述。 |
| `docs/modules.md` | 删除 `request_log_db_type`、`request_log_database_url` 配置行；`request_logs.py` 说明改为仅 SQLite。 |
| `docs/deployment.md` | 删除「创建 PostgreSQL 数据库」章节。 |
| `docs/troubleshooting.md` | 删除「请求记录 PostgreSQL 连接失败」章节。 |
| `docs/architecture.md` | `request_logs.py` 说明改为「SQLite3 后端 + 异步队列写入」。 |
| `docs/superpowers/*` | 历史 spec/plan 保持归档，不主动改写。 |

## 5. 兼容性与迁移

- 当前无生产用户，不需要数据迁移。
- 旧 `settings.json` 中若存在 `request_log_db_type` / `request_log_database_url`，`config._init_settings_sync()` 会忽略它们。
- 下次通过设置页保存配置时，`settings.json` 会被重写，旧键自然消失。

## 6. 验证计划

1. `uv run pytest` 全量通过。
2. 重点子集：
   - `uv run pytest tests/test_request_logs.py -v`
   - `uv run pytest tests/test_settings.py -v`
   - `uv run pytest tests/test_sqlite_hygiene.py -v`
   - `uv run pytest tests/routers/test_admin.py -v`
3. `uv run ruff check .` 无新增告警。
4. 启动服务后访问 `/admin` 设置页，确认数据库类型下拉框和 PostgreSQL 连接串输入框已消失。

## 7. 待实现任务

- [ ] 修改 `request_logs.py`，删除 PG 后端与抽象基类。
- [ ] 修改 `config.py`，删除相关配置项与校验。
- [ ] 修改前端 HTML/JS，移除 PG 相关控件。
- [ ] 更新 `pyproject.toml` 并重新生成 `uv.lock`。
- [ ] 调整相关测试。
- [ ] 更新项目文档。
- [ ] 运行测试与 lint 验证。
