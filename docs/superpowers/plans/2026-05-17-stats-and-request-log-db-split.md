# 统计库与请求记录库拆分实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将稳定统计存储固定到 SQLite，并将调试请求记录存储拆成可热切换的 SQLite/PostgreSQL backend。

**架构：** `stats.py` 只负责统计 SQLite，包括轻量原始统计行和汇总表。新增 `request_logs.py` 负责请求记录 backend，按设置选择 SQLite 或 PostgreSQL，并按四个开关保存原始 Header/Body。管理 API 和前端请求页默认读请求记录库，PG 不可用时提供显式入口读取统计库轻量记录。

**技术栈：** FastAPI、asyncio worker queue、sqlite3、asyncpg、pytest、原生 HTML + Tailwind 管理页。

---

## 文件结构

- 修改：`config.py`
  - 移除旧 `database_url` 配置语义。
  - 新增统计 SQLite 路径、请求记录库类型、请求记录 SQLite 路径、请求记录 PG URL、四个 raw 字段保存开关。
  - 所有新增请求记录配置均不需要重启。
- 重写：`stats.py`
  - 固定使用 SQLite。
  - 提供统计库初始化、写入、日/小时汇总刷新、统计查询、轻量请求列表查询。
  - 保留 `record_request()` 作为非阻塞入队函数。
- 创建：`request_logs.py`
  - 封装请求记录 SQLite/PostgreSQL backend。
  - 提供 `init_backend()`、`reload_backend()`、`record_request()`、`list_requests()`、`get_request_field()`、`close_backend()`。
  - 写入失败只记录 warning。
- 修改：`proxy_core.py`
  - 请求完成后同时调用 `stats.record_request()` 和 `request_logs.record_request()`。
  - 统计写入只传轻量字段，请求记录写入传完整字段并由 `request_logs` 应用四个保存开关。
- 修改：`main.py`
  - lifespan 初始化统计库和请求记录 backend。
  - 停止时关闭两个模块资源。
- 修改：`routers/admin.py`
  - 统计接口读取 `stats.py`。
  - 请求列表默认读取 `request_logs.py`。
  - `source=stats` 时读取 `stats.py` 轻量请求记录。
  - raw 字段详情接口只读取 `request_logs.py`。
  - 设置保存后触发请求记录 backend 热切换。
- 修改：`static/index.html`
  - 设置页新增请求记录数据库配置和四个复选项。
  - 请求记录页处理 backend 不可用错误，显示“查看轻量请求记录”入口。
  - 轻量模式隐藏 Header/Body 链接并显示来源提示。
- 修改：`tests/test_settings.py`
  - 更新新配置默认值和无需重启断言。
- 创建：`tests/test_stats_sqlite.py`
  - 覆盖统计 SQLite 初始化、写入、聚合刷新、轻量列表。
- 创建：`tests/test_request_logs.py`
  - 覆盖请求记录 SQLite backend、保存开关、PG 初始化失败不影响调用方。
- 修改或替换：`tests/test_stats_pg.py`
  - 删除旧 PG 统计测试，或将仍有价值的 PG 请求记录测试迁移到 `tests/test_request_logs.py` 并用 `TEST_DATABASE_URL` 条件跳过。
- 修改：`tests/routers/test_admin.py`
  - 覆盖 `GET /admin/requests?source=stats` 和请求记录 backend 错误响应。

## 任务 1：配置模型重构

**文件：**
- 修改：`config.py`
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写失败的配置测试**

在 `tests/test_settings.py` 中更新 `test_config_defaults`：

```python
def test_config_defaults():
    from config import _CONFIG_SCHEMA

    assert _CONFIG_SCHEMA["stats_sqlite_path"]["default"].endswith("stats.db")
    assert _CONFIG_SCHEMA["request_log_db_type"]["default"] == "sqlite"
    assert _CONFIG_SCHEMA["request_log_sqlite_path"]["default"].endswith("request_logs.db")
    assert _CONFIG_SCHEMA["request_log_database_url"]["default"] == ""
    assert _CONFIG_SCHEMA["save_request_headers"]["default"] is False
    assert _CONFIG_SCHEMA["save_response_headers"]["default"] is False
    assert _CONFIG_SCHEMA["save_request_body"]["default"] is False
    assert _CONFIG_SCHEMA["save_response_body"]["default"] is False
    assert "database_url" not in _CONFIG_SCHEMA
```

更新 `test_config_requires_restart`：

```python
def test_config_requires_restart():
    from config import _CONFIG_SCHEMA

    restart_keys = [k for k, v in _CONFIG_SCHEMA.items() if v.get("requires_restart")]
    assert "host" in restart_keys
    assert "port" in restart_keys
    assert "log_level" in restart_keys
    assert "request_log_db_type" not in restart_keys
    assert "request_log_database_url" not in restart_keys
    assert "request_log_sqlite_path" not in restart_keys
    assert "stats_sqlite_path" not in restart_keys
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_settings.py::test_config_defaults tests/test_settings.py::test_config_requires_restart -v`

预期：FAIL，旧配置仍包含 `database_url`，新配置项不存在。

- [ ] **步骤 3：修改 `config.py`**

将 `_CONFIG_SCHEMA` 中的 `database_url` 替换为：

```python
"stats_sqlite_path": {
    "type": "str",
    "default": os.path.join(DATA_DIR, "stats.db"),
    "requires_restart": False,
    "env": "STATS_SQLITE_PATH",
},
"request_log_db_type": {
    "type": "str",
    "default": "sqlite",
    "requires_restart": False,
    "env": "REQUEST_LOG_DB_TYPE",
},
"request_log_sqlite_path": {
    "type": "str",
    "default": os.path.join(DATA_DIR, "request_logs.db"),
    "requires_restart": False,
    "env": "REQUEST_LOG_SQLITE_PATH",
},
"request_log_database_url": {
    "type": "str",
    "default": "",
    "requires_restart": False,
    "env": "REQUEST_LOG_DATABASE_URL",
},
"save_request_headers": {"type": "bool", "default": False, "requires_restart": False, "env": "SAVE_REQUEST_HEADERS"},
"save_response_headers": {"type": "bool", "default": False, "requires_restart": False, "env": "SAVE_RESPONSE_HEADERS"},
"save_request_body": {"type": "bool", "default": False, "requires_restart": False, "env": "SAVE_REQUEST_BODY"},
"save_response_body": {"type": "bool", "default": False, "requires_restart": False, "env": "SAVE_RESPONSE_BODY"},
```

更新 `get_settings()`，对 `request_log_database_url` 脱敏，并返回 `request_log_database_url_masked`。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_settings.py -v`

预期：PASS。

## 任务 2：统计 SQLite 模块

**文件：**
- 重写：`stats.py`
- 创建：`tests/test_stats_sqlite.py`

- [ ] **步骤 1：编写失败测试**

创建 `tests/test_stats_sqlite.py`：

```python
from datetime import date

import pytest

import stats

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def stats_db(tmp_path, monkeypatch):
    db_path = tmp_path / "stats.db"
    monkeypatch.setattr(stats, "_db_path", str(db_path), raising=False)
    await stats.init_db(str(db_path))
    yield db_path
    await stats.close_pool()


async def test_init_db_creates_raw_and_summary_tables(stats_db):
    tables = await stats._list_tables_for_test()
    assert {"request_stats_raw", "daily_stats", "hourly_stats"} <= set(tables)


async def test_record_request_writes_lightweight_raw_row(stats_db):
    stats.record_request(
        channel_id="ch_1",
        channel_name="Test",
        model="gpt-4",
        is_stream=False,
        input_tokens=10,
        output_tokens=5,
        latency_ms=123,
        success=True,
        request_headers={"X-App": "ignored"},
        request_body={"messages": []},
    )
    await stats.drain_queue()
    result = await stats.list_requests(page=1, page_size=10)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["model"] == "gpt-4"
    assert "request_headers" not in item
    assert "request_body" not in item


async def test_refresh_stats_populates_daily_summary(stats_db):
    stats.record_request(
        channel_id="ch_1",
        channel_name="Test",
        model="gpt-4",
        is_stream=False,
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        success=True,
        api_key_id="key_1",
    )
    await stats.drain_queue()
    result = await stats.aggregate_daily_stats(date.today(), date.today())
    assert result["updated_rows"] >= 1
    rows = await stats.get_daily_stats(days=1)
    assert rows
    assert rows[0]["request_count"] == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_stats_sqlite.py -v`

预期：FAIL，`stats.py` 仍依赖 PostgreSQL，测试辅助函数不存在。

- [ ] **步骤 3：实现 SQLite 统计模块**

重写 `stats.py`：

- 使用 `sqlite3` 标准库，不新增依赖。
- 所有 SQL 操作用 `asyncio.to_thread()` 包装，避免阻塞事件循环。
- 保留非阻塞队列 worker。
- `record_request()` 入队轻量字段，忽略四个 raw 字段。
- `list_requests()` 查询 `request_stats_raw`。
- `get_request_field()` 对统计库返回 `None`。
- `aggregate_daily_stats()` 和 `refresh_stats()` 从 `request_stats_raw` 写入 `daily_stats`。
- `get_daily_stats()`、`get_daily_stats_from_requests()`、`get_overall_stats()`、`get_today_stats()` 读取 SQLite。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_stats_sqlite.py -v`

预期：PASS。

## 任务 3：请求记录 backend

**文件：**
- 创建：`request_logs.py`
- 创建：`tests/test_request_logs.py`

- [ ] **步骤 1：编写失败测试**

创建 `tests/test_request_logs.py`：

```python
import pytest

import request_logs

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sqlite_request_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(request_logs, "_backend", None, raising=False)
    db_path = tmp_path / "request_logs.db"
    await request_logs.init_backend({"request_log_db_type": "sqlite", "request_log_sqlite_path": str(db_path)})
    yield
    await request_logs.close_backend()


async def test_sqlite_backend_writes_and_lists_request_logs(sqlite_request_logs, monkeypatch):
    monkeypatch.setattr(request_logs, "_get_save_flags", lambda: {
        "save_request_headers": True,
        "save_response_headers": False,
        "save_request_body": True,
        "save_response_body": False,
    })
    request_logs.record_request(
        channel_id="ch_1",
        channel_name="Test",
        model="gpt-4",
        is_stream=False,
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        success=True,
        request_headers={"X-App": "Test"},
        response_headers={"X-Resp": "Hidden"},
        request_body={"messages": []},
        response_body={"choices": []},
    )
    await request_logs.drain_queue()
    result = await request_logs.list_requests(page=1, page_size=10)
    assert result["total"] == 1
    req_id = result["items"][0]["id"]
    assert await request_logs.get_request_field(req_id, "request_headers") == {"data": {"X-App": "Test"}}
    assert await request_logs.get_request_field(req_id, "response_headers") == {"data": None}


async def test_invalid_backend_keeps_module_unavailable(monkeypatch):
    monkeypatch.setattr(request_logs, "_backend", None, raising=False)
    result = await request_logs.init_backend({"request_log_db_type": "postgres", "request_log_database_url": ""})
    assert result["available"] is False
    listed = await request_logs.list_requests()
    assert listed["available"] is False
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_request_logs.py -v`

预期：FAIL，`request_logs.py` 不存在。

- [ ] **步骤 3：实现 `request_logs.py`**

实现：

- `SQLiteRequestLogBackend`
- `PostgresRequestLogBackend`
- `init_backend(settings: dict | None = None)`
- `reload_backend(settings: dict | None = None)`
- `close_backend()`
- `record_request(...)`
- `drain_queue()`
- `list_requests(...)`
- `get_request_field(request_id, field)`

SQLite 表 `request_logs` 使用与设计文档一致的字段。PostgreSQL backend 使用 `asyncpg`，表名同为 `request_logs`。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_request_logs.py -v`

预期：PASS。

## 任务 4：接入代理生命周期和写入路径

**文件：**
- 修改：`main.py`
- 修改：`proxy_core.py`
- 修改：`tests/test_lifespan.py`
- 修改：`tests/test_proxy_core.py`

- [ ] **步骤 1：编写失败测试**

在 `tests/test_proxy_core.py` 增加测试，monkeypatch `stats.record_request` 和 `request_logs.record_request`，验证非流式成功请求会调用两者。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_proxy_core.py -v`

预期：FAIL，`request_logs.record_request` 尚未接入。

- [ ] **步骤 3：修改生命周期**

`main.py`：

- 导入 `request_logs`。
- lifespan 中 `await stats.init_db()` 后调用 `await request_logs.init_backend()`。
- shutdown 中调用 `await request_logs.close_backend()`。

`proxy_core.py`：

- 导入 `request_logs`。
- 每个现有 `stats.record_request(...)` 调用后，追加同参数的 `request_logs.record_request(...)`。
- `stats.record_request(...)` 可继续收到完整参数，由统计模块忽略 raw 字段，减少调用点分叉。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_proxy_core.py tests/test_lifespan.py -v`

预期：PASS。

## 任务 5：管理 API 拆分

**文件：**
- 修改：`routers/admin.py`
- 修改：`tests/routers/test_admin.py`

- [ ] **步骤 1：编写失败测试**

在 `tests/routers/test_admin.py` 增加：

```python
async def test_list_requests_source_stats_uses_lightweight_stats(client, monkeypatch):
    async def fake_stats_list_requests(**kwargs):
        return {"items": [{"id": 1, "model": "gpt-4"}], "total": 1, "page": 1, "page_size": 10, "source": "stats"}

    monkeypatch.setattr("routers.admin.stats_list_requests", fake_stats_list_requests)
    resp = await client.get("/admin/requests?source=stats")
    assert resp.status_code == 200
    assert resp.json()["source"] == "stats"
```

增加请求记录 backend 不可用测试：

```python
async def test_list_requests_returns_unavailable_when_request_log_backend_down(client, monkeypatch):
    async def fake_request_log_list_requests(**kwargs):
        return {"available": False, "error": "PostgreSQL unavailable", "items": [], "total": 0, "page": 1, "page_size": 10}

    monkeypatch.setattr("routers.admin.request_log_list_requests", fake_request_log_list_requests)
    resp = await client.get("/admin/requests")
    assert resp.status_code == 503
    assert "PostgreSQL unavailable" in resp.json()["detail"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/routers/test_admin.py -v`

预期：FAIL，`source` 参数和 request log backend 尚未接入。

- [ ] **步骤 3：修改 `routers/admin.py`**

- 将统计函数 import 别名为 `stats_*`。
- 从 `request_logs` import 请求记录函数并别名为 `request_log_*`。
- `GET /admin/requests` 增加 `source: str | None = Query(default=None)`。
- `source == "stats"` 时调用 `stats.list_requests(...)` 并在响应中加 `"source": "stats"`。
- 默认调用 `request_logs.list_requests(...)`，若返回 `available=False`，抛出 `HTTPException(503, detail=error)`。
- raw 字段详情接口调用 `request_logs.get_request_field()`。
- `PUT /admin/settings` 在 `_config.update_settings()` 成功后调用 `request_logs.reload_backend()`，失败则返回错误并保留旧 backend。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/routers/test_admin.py -v`

预期：PASS。

## 任务 6：前端设置与请求页 fallback

**文件：**
- 修改：`static/index.html`
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写失败测试**

在 `tests/test_settings.py` 增加静态 HTML 断言：

```python
def test_settings_page_has_request_log_db_controls():
    html = Path("static/index.html").read_text(encoding="utf-8")

    assert "set_request_log_db_type" in html
    assert "set_request_log_sqlite_path" in html
    assert "set_request_log_database_url" in html
    assert "set_save_request_headers" in html
    assert "set_save_response_headers" in html
    assert "set_save_request_body" in html
    assert "set_save_response_body" in html
    assert "loadStatsRequestLogs" in html
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_settings.py::test_settings_page_has_request_log_db_controls -v`

预期：FAIL，前端控件不存在。

- [ ] **步骤 3：修改 `static/index.html`**

- 数据库设置区改成“请求记录数据库”。
- 增加 SQLite/PG 选择、两个路径/URL 输入、四个复选框。
- `loadSettings()` 填充新字段。
- `saveSettings()` 提交新字段。
- 请求列表加载函数捕获 503，显示错误块和“查看轻量请求记录”按钮。
- 新增 `loadStatsRequestLogs()`，调用 `/admin/requests?source=stats`。
- 轻量模式隐藏 `openJsonInNewTab()` 的四个入口，并展示轻量模式提示。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_settings.py -v`

预期：PASS。

## 任务 7：全量清理与验证

**文件：**
- 修改：`tests/test_stats_pg.py`
- 修改：`README.md` 或 `AGENTS.md`，如需要更新命令或架构说明

- [ ] **步骤 1：处理旧 PG 统计测试**

删除或替换 `tests/test_stats_pg.py` 中与旧 `stats.py` PG 实现绑定的测试。保留 PostgreSQL 请求记录 backend 测试时，迁移到 `tests/test_request_logs.py`，并用 `TEST_DATABASE_URL` 条件跳过。

- [ ] **步骤 2：运行格式和测试**

运行：

```bash
uv run ruff check .
uv run pytest
```

预期：全部 PASS。

- [ ] **步骤 3：手动验证热切换**

启动服务：

```bash
uv run python main.py --no-reload
```

在管理页完成：

- 设置请求记录库为 SQLite，保存后请求记录页可打开。
- 设置请求记录库为无效 PostgreSQL URL，保存后不影响统计页。
- 请求记录页显示错误和轻量记录入口。
- 点击轻量记录入口后，表格从统计库加载基础记录，并不显示 raw 字段链接。

## 自检

- 设计文档中的统计库固定 SQLite 已覆盖：任务 2、任务 4、任务 5。
- 请求记录库可切换已覆盖：任务 3、任务 5、任务 6。
- 四个保存开关已覆盖：任务 1、任务 3、任务 6。
- PG 不可用时显式错误和轻量入口已覆盖：任务 5、任务 6。
- 不迁移旧数据已覆盖：任务 7 删除旧 PG 统计测试，不做兼容路径。
