# Admin IP 白名单实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `/admin/*` 路由增加基于 CSV 规则的 IP 白名单保护，支持路径 glob、HTTP 方法、IP/CIDR 三维匹配，热重载，并提供管理 UI。

**Architecture:** 新增独立 `whitelist.py` 模块，包含规则解析、缓存（mtime 热重载）和纯函数匹配逻辑；在 `CombinedMiddleware` 最前面插入白名单检查；`routers/admin.py` 新增 GET/PUT 端点用于读写 CSV；`static/index.html` 设置页增加「IP 白名单」子节，界面即文档。

**Tech Stack:** Python `ipaddress`、`fnmatch`、`csv` 标准库；FastAPI；Tailwind CSS（已有）。

---

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `whitelist.py` | 规则数据类、解析、缓存、校验、匹配逻辑 |
| 新建 | `tests/test_whitelist.py` | whitelist 模块单元测试 |
| 新建 | `tests/routers/test_admin_whitelist.py` | Admin API 端点测试 |
| 修改 | `main.py` | 引入 `_whitelist_cache`；在 `CombinedMiddleware` 插入检查；扩展 `_send_error` 签名 |
| 修改 | `routers/admin.py` | 新增 `GET /admin/whitelist`、`PUT /admin/whitelist` |
| 修改 | `static/index.html` | 设置页新增「IP 白名单」导航项、内容节、JS 函数 |

---

## Task 1: 创建 `whitelist.py` 核心模块

**Files:**
- Create: `whitelist.py`
- Create: `tests/test_whitelist.py`

- [ ] **Step 1: 写失败的单元测试**

新建 `tests/test_whitelist.py`，内容如下：

```python
import ipaddress
import os
import time

import pytest

import whitelist as wl


# ─── load_rules ───

def test_load_rules_file_not_found():
    assert wl.load_rules("/nonexistent/whitelist.csv") == []


def test_load_rules_empty_file(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("")
    assert wl.load_rules(str(f)) == []


def test_load_rules_skips_comments(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("# this is a comment\n# another comment\n")
    assert wl.load_rules(str(f)) == []


def test_load_rules_skips_header(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("path_pattern,methods,ip_cidr,description\n")
    assert wl.load_rules(str(f)) == []


def test_load_rules_parses_wildcard_method(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("path_pattern,methods,ip_cidr,description\n/admin/*,*,10.0.0.0/8,内网\n")
    rules = wl.load_rules(str(f))
    assert len(rules) == 1
    r = rules[0]
    assert r.path_pattern == "/admin/*"
    assert r.methods == set()          # * → empty set means all
    assert str(r.network) == "10.0.0.0/8"
    assert r.description == "内网"


def test_load_rules_parses_method_filter(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,GET|POST,127.0.0.1,本机\n")
    rules = wl.load_rules(str(f))
    assert rules[0].methods == {"GET", "POST"}


def test_load_rules_skips_invalid_cidr(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,*,not-an-ip,test\n")
    assert wl.load_rules(str(f)) == []


def test_load_rules_multiple_with_comments(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text(
        "# comment\n"
        "path_pattern,methods,ip_cidr,description\n"
        "/admin/*,*,10.1.1.0/24,内网\n"
        "\n"
        "# another comment\n"
        "/admin/stats,GET,203.0.113.5,公司\n"
    )
    rules = wl.load_rules(str(f))
    assert len(rules) == 2


# ─── check_request ───

def _make_rules():
    return [
        wl.WhitelistRule(
            path_pattern="/admin/*",
            methods=set(),
            network=ipaddress.ip_network("10.1.1.0/24"),
            description="内网",
        ),
        wl.WhitelistRule(
            path_pattern="/admin/stats",
            methods={"GET"},
            network=ipaddress.ip_network("203.0.113.5/32"),
            description="公司只读",
        ),
    ]


def test_check_no_rules_allows_all():
    ok, _ = wl.check_request([], "/admin/channels", "GET", "1.2.3.4")
    assert ok is True


def test_check_path_not_matched_allows():
    ok, _ = wl.check_request(_make_rules(), "/v1/chat/completions", "POST", "1.2.3.4")
    assert ok is True


def test_check_ip_not_in_cidr_returns_403():
    ok, reason = wl.check_request(_make_rules(), "/admin/channels", "GET", "192.168.1.1")
    assert ok is False
    assert "IP 白名单" in reason


def test_check_ip_in_cidr_allowed():
    ok, _ = wl.check_request(_make_rules(), "/admin/channels", "DELETE", "10.1.1.50")
    assert ok is True


def test_check_method_not_allowed_returns_403():
    ok, reason = wl.check_request(_make_rules(), "/admin/stats", "DELETE", "203.0.113.5")
    assert ok is False
    assert "DELETE" in reason


def test_check_method_allowed():
    ok, _ = wl.check_request(_make_rules(), "/admin/stats", "GET", "203.0.113.5")
    assert ok is True


def test_check_exact_ip_host_bits():
    """192.168.1.5 进入 192.168.1.0/24 应被允许"""
    rules = [
        wl.WhitelistRule(
            path_pattern="/admin/*",
            methods=set(),
            network=ipaddress.ip_network("192.168.1.0/24"),
            description="test",
        )
    ]
    ok, _ = wl.check_request(rules, "/admin/x", "GET", "192.168.1.5")
    assert ok is True


# ─── validate_rules_text ───

def test_validate_empty_text():
    ok, _, rules = wl.validate_rules_text("")
    assert ok is True
    assert rules == []


def test_validate_valid_text():
    text = "path_pattern,methods,ip_cidr,description\n/admin/*,*,10.0.0.0/8,内网\n"
    ok, err, rules = wl.validate_rules_text(text)
    assert ok is True
    assert err == ""
    assert len(rules) == 1


def test_validate_bad_column_count():
    ok, err, _ = wl.validate_rules_text("/admin/*,*,10.0.0.0/8\n")
    assert ok is False
    assert "4 列" in err


def test_validate_bad_cidr():
    ok, err, _ = wl.validate_rules_text("/admin/*,*,bad-ip,test\n")
    assert ok is False
    assert "bad-ip" in err


# ─── WhitelistCache ───

def test_cache_missing_file():
    cache = wl.WhitelistCache("/nonexistent/whitelist.csv")
    assert cache.get_rules() == []


def test_cache_hot_reload(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,*,10.0.0.0/8,内网\n")
    cache = wl.WhitelistCache(str(f))

    rules1 = cache.get_rules()
    assert len(rules1) == 1

    # 写入新内容并强制 mtime 变化（文件系统精度可能 1s）
    time.sleep(0.05)
    f.write_text("")
    os.utime(str(f), (time.time() + 1, time.time() + 1))

    rules2 = cache.get_rules()
    assert len(rules2) == 0


def test_cache_no_reload_if_mtime_unchanged(tmp_path):
    f = tmp_path / "whitelist.csv"
    f.write_text("/admin/*,*,10.0.0.0/8,内网\n")
    cache = wl.WhitelistCache(str(f))
    rules1 = cache.get_rules()
    rules2 = cache.get_rules()
    assert rules1 is rules2   # same list object = no reload
```

- [ ] **Step 2: 运行测试，确认全部失败**

```bash
uv run pytest tests/test_whitelist.py -v 2>&1 | head -30
```

预期：`ModuleNotFoundError: No module named 'whitelist'`

- [ ] **Step 3: 实现 `whitelist.py`**

新建 `whitelist.py`，内容如下：

```python
import csv
import ipaddress
import os
from dataclasses import dataclass, field
from fnmatch import fnmatch


@dataclass
class WhitelistRule:
    path_pattern: str
    methods: set[str]   # 空集合 = 允许所有方法
    network: ipaddress.IPv4Network | ipaddress.IPv6Network
    description: str


class WhitelistCache:
    def __init__(self, path: str) -> None:
        self._path = path
        self._mtime: float = -1.0
        self._rules: list[WhitelistRule] = []

    def get_rules(self) -> list[WhitelistRule]:
        try:
            mtime = os.stat(self._path).st_mtime
        except FileNotFoundError:
            self._rules = []
            self._mtime = -1.0
            return self._rules
        if mtime != self._mtime:
            self._rules = load_rules(self._path)
            self._mtime = mtime
        return self._rules


def load_rules(path: str) -> list[WhitelistRule]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    filtered = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    rules: list[WhitelistRule] = []
    reader = csv.reader(filtered)
    for row in reader:
        if len(row) < 4:
            continue
        path_pat, methods_str, ip_cidr, description = (col.strip() for col in row[:4])
        if path_pat == "path_pattern":
            continue
        if not path_pat or not ip_cidr:
            continue
        methods: set[str] = set()
        if methods_str and methods_str != "*":
            methods = {m.strip().upper() for m in methods_str.split("|")}
        try:
            network = ipaddress.ip_network(ip_cidr, strict=False)
        except ValueError:
            continue
        rules.append(WhitelistRule(
            path_pattern=path_pat,
            methods=methods,
            network=network,
            description=description,
        ))
    return rules


def validate_rules_text(text: str) -> tuple[bool, str, list[WhitelistRule]]:
    """校验并解析 CSV 文本。返回 (valid, error_message, parsed_rules)。"""
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    rules: list[WhitelistRule] = []
    reader = csv.reader(lines)
    for i, row in enumerate(reader):
        if not row or row[0].strip() == "path_pattern":
            continue
        if len(row) < 4:
            return False, f"第 {i + 1} 行格式错误：需要 4 列，实际 {len(row)} 列", []
        path_pat, methods_str, ip_cidr, description = (col.strip() for col in row[:4])
        if not path_pat:
            return False, f"第 {i + 1} 行：path_pattern 不能为空", []
        if not ip_cidr:
            return False, f"第 {i + 1} 行：ip_cidr 不能为空", []
        try:
            network = ipaddress.ip_network(ip_cidr, strict=False)
        except ValueError:
            return False, f"第 {i + 1} 行：无效的 IP 或 CIDR：{ip_cidr!r}", []
        methods: set[str] = set()
        if methods_str and methods_str != "*":
            methods = {m.strip().upper() for m in methods_str.split("|")}
        rules.append(WhitelistRule(
            path_pattern=path_pat,
            methods=methods,
            network=network,
            description=description,
        ))
    return True, "", rules


def check_request(
    rules: list[WhitelistRule],
    path: str,
    method: str,
    client_ip: str,
) -> tuple[bool, str]:
    """检查请求是否通过白名单。返回 (allow, reason)；允许时 reason 为空字符串。"""
    path_rules = [r for r in rules if fnmatch(path, r.path_pattern)]
    if not path_rules:
        return True, ""
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False, "无法确定客户端 IP 地址"
    ip_rules = [r for r in path_rules if addr in r.network]
    if not ip_rules:
        return False, "不在 IP 白名单范围内"
    method_upper = method.upper()
    for r in ip_rules:
        if not r.methods or method_upper in r.methods:
            return True, ""
    return False, f"该 IP 不允许使用 {method.upper()} 方法"
```

- [ ] **Step 4: 运行测试，确认全部通过**

```bash
uv run pytest tests/test_whitelist.py -v
```

预期：所有测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add whitelist.py tests/test_whitelist.py
git commit -m "feat: add whitelist module with rule parsing, cache, and matching logic"
```

---

## Task 2: 在 `CombinedMiddleware` 接入白名单检查

**Files:**
- Modify: `main.py`
- Create: `tests/routers/test_admin_whitelist.py`（仅中间件部分）

- [ ] **Step 1: 写失败的中间件集成测试**

新建 `tests/routers/test_admin_whitelist.py`，加入以下测试（仅中间件部分，API 测试在 Task 3 补充）：

```python
import ipaddress

import pytest
import pytest_asyncio
import httpx

import request_logs
import stats
import whitelist as wl
from main import app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(tmp_path, monkeypatch):
    await stats.init_db(str(tmp_path / "stats.db"))
    await request_logs.init_backend(
        {
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(tmp_path / "request_logs.db"),
        }
    )
    yield
    await stats.close_pool()
    await request_logs.close_backend()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestWhitelistMiddleware:
    async def test_no_rules_allows_admin(self, client, monkeypatch):
        """白名单为空时放行所有请求"""
        import main
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: [])
        resp = await client.get("/admin/channels")
        assert resp.status_code != 403

    async def test_matching_ip_allows_request(self, client, monkeypatch):
        """IP 匹配白名单规则时放行"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods=set(),
                network=ipaddress.ip_network("127.0.0.1/32"),
                description="test",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        resp = await client.get("/admin/channels")
        assert resp.status_code != 403

    async def test_non_matching_ip_blocks_admin(self, client, monkeypatch):
        """IP 不在白名单时返回 403"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods=set(),
                network=ipaddress.ip_network("10.0.0.0/8"),
                description="内网",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        resp = await client.get("/admin/channels")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["type"] == "ip_whitelist_error"
        assert "IP 白名单" in body["error"]["message"]

    async def test_method_not_allowed_returns_403(self, client, monkeypatch):
        """方法不在白名单时返回 403"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods={"GET"},
                network=ipaddress.ip_network("127.0.0.1/32"),
                description="test",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        resp = await client.delete("/admin/channels/nonexistent")
        assert resp.status_code == 403
        assert "DELETE" in resp.json()["error"]["message"]

    async def test_non_admin_path_not_blocked(self, client, monkeypatch):
        """白名单规则只针对 /admin/*，其他路径不受影响"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods=set(),
                network=ipaddress.ip_network("10.0.0.0/8"),
                description="内网",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        # 根路径重定向，不应被 403
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code != 403
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/routers/test_admin_whitelist.py::TestWhitelistMiddleware -v 2>&1 | head -30
```

预期：`AttributeError: module 'main' has no attribute '_whitelist_cache'`

- [ ] **Step 3: 修改 `main.py`**

**3a. 在文件顶部 `import` 区域末尾（`from storage import ...` 之后）添加：**

```python
import whitelist as _whitelist
```

**3b. 在 `_PROXY_PATHS` 常量定义之后、`_api_key_index` 之前添加模块级缓存：**

```python
_DATA_DIR = Path(__file__).parent / "data"
_whitelist_cache = _whitelist.WhitelistCache(str(_DATA_DIR / "whitelist.csv"))
```

**3c. 修改 `_send_error` 方法签名，增加可选的 `error_type` 参数：**

将：
```python
    async def _send_error(self, send: Send, status: int, message: str) -> None:
        error_body = json.dumps({"error": {"message": message, "type": "auth_error"}}).encode()
```

改为：
```python
    async def _send_error(self, send: Send, status: int, message: str, error_type: str = "auth_error") -> None:
        error_body = json.dumps({"error": {"message": message, "type": error_type}}).encode()
```

**3d. 在 `CombinedMiddleware.__call__` 中，紧接 `method = scope["method"]` / `path = scope["path"]` 两行之后插入白名单检查：**

将：
```python
        method = scope["method"]
        path = scope["path"]

        # Only process proxy API requests
        if method != "POST" or path not in _PROXY_PATHS:
```

改为：
```python
        method = scope["method"]
        path = scope["path"]

        # IP 白名单检查（对所有 HTTP 请求生效）
        client_ip = (scope.get("client") or ("", 0))[0]
        _wl_rules = _whitelist_cache.get_rules()
        _wl_allowed, _wl_reason = _whitelist.check_request(_wl_rules, path, method, client_ip)
        if not _wl_allowed:
            await self._send_error(send, 403, _wl_reason, "ip_whitelist_error")
            return

        # Only process proxy API requests
        if method != "POST" or path not in _PROXY_PATHS:
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
uv run pytest tests/routers/test_admin_whitelist.py::TestWhitelistMiddleware -v
```

预期：5 个测试全部 PASS。

- [ ] **Step 5: 跑全量测试，确认无回归**

```bash
uv run pytest --ignore=tests/test_e2e.py --ignore=tests/test_responses_e2e.py -x -q
```

预期：所有测试 PASS，无新失败。

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: integrate IP whitelist check into CombinedMiddleware"
```

---

## Task 3: Admin API 端点（GET / PUT `/admin/whitelist`）

**Files:**
- Modify: `routers/admin.py`
- Modify: `tests/routers/test_admin_whitelist.py`（追加 API 测试）

- [ ] **Step 1: 追加 API 测试到 `tests/routers/test_admin_whitelist.py`**

在文件末尾追加：

```python
class TestWhitelistAPI:
    @pytest_asyncio.fixture(autouse=True)
    async def patch_whitelist_path(self, tmp_path, monkeypatch):
        """每个测试使用独立临时目录，白名单检查全部放行"""
        import routers.admin as admin_router
        import main
        monkeypatch.setattr(admin_router, "WHITELIST_PATH", tmp_path / "whitelist.csv")
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: [])

    async def test_get_whitelist_no_file(self, client):
        resp = await client.get("/admin/whitelist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == ""
        assert data["rule_count"] == 0

    async def test_put_whitelist_saves_and_returns_count(self, client):
        content = (
            "path_pattern,methods,ip_cidr,description\n"
            "/admin/*,*,10.1.1.0/24,内网\n"
            "/admin/*,*,127.0.0.1,本机\n"
        )
        resp = await client.put("/admin/whitelist", json={"content": content})
        assert resp.status_code == 200
        data = resp.json()
        assert data["rule_count"] == 2

    async def test_get_whitelist_returns_saved_content(self, client):
        content = "# comment\n/admin/*,*,127.0.0.1,本机\n"
        await client.put("/admin/whitelist", json={"content": content})
        resp = await client.get("/admin/whitelist")
        assert resp.json()["content"] == content
        assert resp.json()["rule_count"] == 1

    async def test_put_whitelist_invalid_cidr_returns_400(self, client):
        resp = await client.put(
            "/admin/whitelist", json={"content": "/admin/*,*,not-an-ip,test\n"}
        )
        assert resp.status_code == 400
        assert "not-an-ip" in resp.json()["detail"]

    async def test_put_whitelist_bad_column_count_returns_400(self, client):
        resp = await client.put(
            "/admin/whitelist", json={"content": "/admin/*,*\n"}
        )
        assert resp.status_code == 400
        assert "4 列" in resp.json()["detail"]

    async def test_put_whitelist_empty_clears_rules(self, client):
        await client.put("/admin/whitelist", json={"content": "/admin/*,*,127.0.0.1,test\n"})
        resp = await client.put("/admin/whitelist", json={"content": ""})
        assert resp.status_code == 200
        assert resp.json()["rule_count"] == 0
```

- [ ] **Step 2: 运行 API 测试，确认失败**

```bash
uv run pytest tests/routers/test_admin_whitelist.py::TestWhitelistAPI -v 2>&1 | head -20
```

预期：`404 Not Found`（端点尚未存在）

- [ ] **Step 3: 在 `routers/admin.py` 添加端点**

**3a. 在文件顶部 `LOGS_DIR` / `STATIC_DIR` 两行后面追加：**

```python
DATA_DIR = Path(__file__).parent.parent / "data"
WHITELIST_PATH = DATA_DIR / "whitelist.csv"
```

**3b. 在文件末尾（`/restart` 端点之后）追加：**

```python
# ============ IP 白名单 ============

import whitelist as _whitelist_mod


@router.get("/whitelist")
async def get_whitelist():
    """获取白名单 CSV 原始文本及有效规则数"""
    if not WHITELIST_PATH.exists():
        return {"content": "", "rule_count": 0}
    content = WHITELIST_PATH.read_text(encoding="utf-8")
    rules = _whitelist_mod.load_rules(str(WHITELIST_PATH))
    return {"content": content, "rule_count": len(rules)}


@router.put("/whitelist")
async def update_whitelist(body: dict):
    """校验并保存白名单 CSV，热重载自动生效"""
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content 必须是字符串")
    valid, error, rules = _whitelist_mod.validate_rules_text(content)
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WHITELIST_PATH.write_text(content, encoding="utf-8")
    return {"message": f"已保存 {len(rules)} 条规则", "rule_count": len(rules)}
```

- [ ] **Step 4: 运行 API 测试，确认通过**

```bash
uv run pytest tests/routers/test_admin_whitelist.py -v
```

预期：全部 PASS（中间件 5 个 + API 6 个）。

- [ ] **Step 5: 跑全量测试**

```bash
uv run pytest --ignore=tests/test_e2e.py --ignore=tests/test_responses_e2e.py -x -q
```

预期：全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add routers/admin.py tests/routers/test_admin_whitelist.py
git commit -m "feat: add GET/PUT /admin/whitelist endpoints"
```

---

## Task 4: Admin UI — 设置页「IP 白名单」子节

**Files:**
- Modify: `static/index.html`

此任务包含 4 个独立改动点，按顺序进行。

- [ ] **Step 1: 在桌面端 tab 导航添加「IP 白名单」按钮**

定位到（约第 278 行）：
```html
            <button onclick="switchTab('settings')" id="tab_settings" class="px-4 py-2.5 text-sm font-medium tab-inactive">设置</button>
```

在其**后面**插入：
```html
            <button onclick="switchTab('whitelist')" id="tab_whitelist" class="px-4 py-2.5 text-sm font-medium tab-inactive">IP 白名单</button>
```

- [ ] **Step 2: 在移动端下拉菜单添加选项**

定位到（约第 288 行）：
```html
                <option value="settings">设置</option>
```

在其**后面**插入：
```html
                <option value="whitelist">IP 白名单</option>
```

- [ ] **Step 3: 在 `switchTab` 函数中注册新 tab**

定位到（约第 1043–1044 行）：
```javascript
  const tabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings'];
  const panelMap = { channels: 'channelsTab', apikeys: 'apikeysTab', lb: 'lbTab', stats: 'statsTab', requests: 'requestsTab', settings: 'settingsTab' };
```

改为：
```javascript
  const tabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings', 'whitelist'];
  const panelMap = { channels: 'channelsTab', apikeys: 'apikeysTab', lb: 'lbTab', stats: 'statsTab', requests: 'requestsTab', settings: 'settingsTab', whitelist: 'whitelistTab' };
```

同时，定位到（约第 1062 行）：
```javascript
  if (tab === 'settings') { switchSettingsSection('server'); loadSettings(); }
```

在其**后面**插入：
```javascript
  if (tab === 'whitelist') { loadWhitelist(); }
```

- [ ] **Step 4: 在 `initTabFromHash` 函数中注册新 tab**

定位到（约第 1070 行）：
```javascript
            const validTabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings'];
```

改为：
```javascript
            const validTabs = ['channels', 'apikeys', 'lb', 'stats', 'requests', 'settings', 'whitelist'];
```

- [ ] **Step 5: 在 `</div><!-- max-w-6xl -->` 前添加白名单 tab 面板**

定位到其他 tab 面板所在的 `<div class="max-w-6xl mx-auto px-4 py-5 sm:py-8">` 区域末尾，在关闭 `</div>` 之前追加：

```html
    <!-- ======== IP 白名单 Tab ======== -->
    <div id="whitelistTab" class="hidden">
      <div class="max-w-2xl">
        <!-- 标题 -->
        <div class="mb-5">
          <h2 class="text-base font-semibold text-ink-900">IP 白名单</h2>
          <p class="text-sm text-ink-600 mt-0.5">控制哪些 IP 可以访问管理接口。白名单为空时，不做任何限制。</p>
        </div>

        <!-- 规则说明（界面即文档） -->
        <div class="card p-5 mb-4">
          <h3 class="text-sm font-semibold text-ink-900 mb-3">规则格式说明</h3>
          <div class="space-y-3 text-sm text-ink-700 leading-6">
            <p>每行一条规则，用 <span class="font-mono bg-surface-100 px-1 rounded text-xs">,</span> 分隔 <strong>4 列</strong>。<span class="font-mono bg-surface-100 px-1 rounded text-xs">#</span> 开头的行为注释，空行忽略。</p>
            <p>多行之间是「或」关系：请求命中<strong>任意一行</strong>即放行。同一行内路径、方法、IP 三者<strong>同时满足</strong>才算命中。</p>
            <div class="overflow-x-auto rounded-lg border border-surface-200">
              <table class="w-full text-xs">
                <thead class="bg-surface-50">
                  <tr>
                    <th class="px-3 py-2 text-left font-medium text-ink-600 border-b border-surface-200">列</th>
                    <th class="px-3 py-2 text-left font-medium text-ink-600 border-b border-surface-200">示例</th>
                    <th class="px-3 py-2 text-left font-medium text-ink-600 border-b border-surface-200">说明</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-surface-100">
                  <tr>
                    <td class="px-3 py-2 font-mono text-ink-900">path_pattern</td>
                    <td class="px-3 py-2 font-mono text-ink-700">/admin/*</td>
                    <td class="px-3 py-2 text-ink-600">路径，支持 <code class="font-mono bg-surface-100 px-0.5 rounded">*</code> 通配符，<code class="font-mono bg-surface-100 px-0.5 rounded">/admin/*</code> 覆盖所有管理接口</td>
                  </tr>
                  <tr>
                    <td class="px-3 py-2 font-mono text-ink-900">methods</td>
                    <td class="px-3 py-2 font-mono text-ink-700">* 或 GET|POST</td>
                    <td class="px-3 py-2 text-ink-600"><code class="font-mono bg-surface-100 px-0.5 rounded">*</code> 表示不限方法；多个方法用竖线 <code class="font-mono bg-surface-100 px-0.5 rounded">|</code> 分隔</td>
                  </tr>
                  <tr>
                    <td class="px-3 py-2 font-mono text-ink-900">ip_cidr</td>
                    <td class="px-3 py-2 font-mono text-ink-700">192.168.1.0/24</td>
                    <td class="px-3 py-2 text-ink-600">精确 IP 或 CIDR 网段，支持 IPv4 和 IPv6</td>
                  </tr>
                  <tr>
                    <td class="px-3 py-2 font-mono text-ink-900">description</td>
                    <td class="px-3 py-2 font-mono text-ink-700">家庭内网</td>
                    <td class="px-3 py-2 text-ink-600">备注说明，会出现在 403 错误信息中</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <pre class="bg-surface-50 border border-surface-200 rounded-lg p-3 text-xs font-mono overflow-x-auto leading-5"># 注释：以 # 开头的行会被忽略
path_pattern,methods,ip_cidr,description
/admin/*,*,10.1.1.0/24,家庭内网
/admin/*,*,127.0.0.1,本机
/admin/stats,GET,203.0.113.5,公司只读</pre>
            <div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-ink-700 leading-5">
              <span class="font-semibold">注意：</span>系统当前使用直连 IP（TCP 连接地址）进行匹配。若服务部署在 nginx/caddy 等反向代理后面，所有请求的来源 IP 都会是代理本身（通常 127.0.0.1），白名单将以代理 IP 为准。请在代理层做访问控制，或等后续版本支持 <code class="font-mono bg-amber-100 px-0.5 rounded">X-Forwarded-For</code> 后再在此配置。
            </div>
          </div>
        </div>

        <!-- 编辑区 -->
        <div class="card p-5">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm font-semibold text-ink-900">编辑规则</h3>
            <span id="whitelist_rule_count" class="text-xs text-ink-400"></span>
          </div>
          <textarea id="whitelist_content"
            class="w-full font-mono text-sm border border-surface-200 rounded-lg px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 resize-y"
            rows="10"
            placeholder="# 暂无规则，白名单为空时不做任何限制&#10;path_pattern,methods,ip_cidr,description&#10;/admin/*,*,127.0.0.1,本机"></textarea>
          <div id="whitelist_error" class="hidden mt-2 text-sm text-red-600 rounded-lg bg-red-50 px-3 py-2"></div>
          <div class="flex justify-end mt-3">
            <button onclick="saveWhitelist()" id="whitelist_save_btn" class="btn-primary text-sm px-4 py-2">保存</button>
          </div>
        </div>
      </div>
    </div>
```

- [ ] **Step 6: 在 JS 区域末尾（`</script>` 前）追加 JS 函数**

定位到文件末尾 `</script>` 标签，在其**前面**插入：

```javascript
// ========== IP 白名单 ==========

async function loadWhitelist() {
  try {
    const res = await fetch('/admin/whitelist');
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('whitelist_content').value = data.content || '';
    const countEl = document.getElementById('whitelist_rule_count');
    countEl.textContent = data.rule_count > 0 ? `${data.rule_count} 条有效规则` : '暂无规则';
  } catch (e) {
    console.error('loadWhitelist error', e);
  }
}

async function saveWhitelist() {
  const content = document.getElementById('whitelist_content').value;
  const errorEl = document.getElementById('whitelist_error');
  const btn = document.getElementById('whitelist_save_btn');
  errorEl.classList.add('hidden');

  // 前端格式粗检：非注释非空行必须恰好 4 列
  const lines = content.split('\n').filter(l => l.trim() && !l.trim().startsWith('#'));
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === 'path_pattern,methods,ip_cidr,description') continue;
    const parts = line.split(',');
    if (parts.length < 4) {
      errorEl.textContent = `第 ${i + 1} 行格式错误：需要 4 列，实际 ${parts.length} 列`;
      errorEl.classList.remove('hidden');
      return;
    }
  }

  btn.disabled = true;
  try {
    const res = await fetch('/admin/whitelist', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.detail || '保存失败';
      errorEl.classList.remove('hidden');
      return;
    }
    const countEl = document.getElementById('whitelist_rule_count');
    countEl.textContent = data.rule_count > 0 ? `${data.rule_count} 条有效规则` : '暂无规则';
    const original = btn.textContent;
    btn.textContent = '已保存 ✓';
    setTimeout(() => { btn.textContent = original; }, 1500);
  } catch (e) {
    errorEl.textContent = '网络错误，请重试';
    errorEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
}
```

- [ ] **Step 7: 在浏览器中验证**

启动服务：
```bash
uv run uvicorn main:app --host 0.0.0.0 --port 55555 --reload
```

打开 `http://localhost:55555`，点击「IP 白名单」tab，检查：
1. 说明区正确显示，表格和示例代码可读
2. 编辑区默认为空（或显示已有 CSV）
3. 输入有效规则后点保存，显示「已保存 ✓」和规则数
4. 输入格式错误（如缺列）点保存，显示错误提示
5. 刷新页面后规则仍在（验证持久化）

- [ ] **Step 8: 全量测试**

```bash
uv run pytest --ignore=tests/test_e2e.py --ignore=tests/test_responses_e2e.py -x -q
```

预期：全部 PASS。

- [ ] **Step 9: Commit**

```bash
git add static/index.html
git commit -m "feat: add IP whitelist management UI to admin panel"
```
