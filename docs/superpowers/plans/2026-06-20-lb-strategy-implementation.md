# 负载均衡策略实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 按 `docs/superpowers/specs/2026-06-20-lb-strategy-design.md` 实现 `round_robin`、`backup`、`sticky` 三种全局负载均衡策略，并保证默认行为兼容、sticky 安全脱敏、配置热更新和设置页可操作。

**架构：** 保持 `balancer/load_balancer.py` 作为策略核心和全局单例，不引入新服务层。`config.py` 负责 settings schema、约束和热更新；`proxy_core.py` 在选择渠道时把已存在的 `client_ip`、`api_key_id`、`client_headers` 传给 balancer；`static/fragments/admin/settings.html` 与 `static/js/settings.js` 扩展负载均衡设置 UI。

**技术栈：** Python 3、FastAPI、Pydantic model `Channel`、pytest / pytest-asyncio、原生 HTML + Tailwind + vanilla JS。

---

## 文件结构

- 修改：`balancer/load_balancer.py`
  - 职责：维护渠道健康状态、全局 LB 配置、策略选择、sticky key 摘要、HRW 选择和 sticky LRU TTL 缓存。
- 修改：`config.py`
  - 职责：新增 `lb_strategy`、`sticky_ttl`、`sticky_cache_max_entries` 配置 schema、约束校验和热更新传递。
- 修改：`proxy_core.py`
  - 职责：在单模型和模型组 fallback 的 `select_channel()` 调用中传入请求上下文。
- 修改：`main.py`
  - 职责：确保 `CombinedMiddleware` 把 `client_ip` 写入 `scope["state"]`，保留已有 `api_key_id` 写入。
- 修改：`static/fragments/admin/settings.html`
  - 职责：在负载均衡 Tab 增加策略、sticky TTL、sticky 缓存容量控件和说明文案。
- 修改：`static/js/settings.js`
  - 职责：加载、脏状态检测、保存新增 LB 设置；根据策略显示/隐藏 sticky 字段。
- 新增：`tests/balancer/test_load_balancer_strategies.py`
  - 职责：覆盖 backup、sticky、key 脱敏、HRW、TTL/LRU、配置切换和并发锁行为。
- 修改：`tests/test_settings.py`
  - 职责：覆盖配置 schema、约束、热更新调用和设置页字段存在性。
- 修改：`tests/test_proxy_core.py`
  - 职责：覆盖 `proxy_core` 选择渠道时传递 `client_ip`、`api_key_id`、`client_headers`。
- 新增或修改：`tests/test_admin_frontend_regressions.py`
  - 职责：如果已有该文件则追加源码断言；否则新建，用低成本断言覆盖 `static/js/settings.js` 的新增字段和动态显示函数。

---

### 任务 1：为 LoadBalancer 增加策略配置骨架

**文件：**
- 修改：`balancer/load_balancer.py`
- 新增：`tests/balancer/test_load_balancer_strategies.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/balancer/test_load_balancer_strategies.py` 新建以下内容：

```python
import asyncio

import pytest

from balancer.load_balancer import LoadBalancer
from models.channel import Channel


def make_channel(
    id: str,
    *,
    weight: int = 1,
    priority: int = 1,
    enabled: bool = True,
) -> Channel:
    return Channel(
        id=id,
        name=f"Channel {id}",
        api_type="openai-chat-completions",
        base_url="http://example.com",
        api_key="key",
        models=["gpt-4"],
        enabled=enabled,
        weight=weight,
        priority=priority,
    )


@pytest.mark.asyncio
async def test_update_config_sets_strategy_and_sticky_limits():
    lb = LoadBalancer()

    await lb.update_config(
        max_fail_count=3,
        cooldown_seconds=12,
        strategy="sticky",
        sticky_ttl=600,
        sticky_cache_max_entries=321,
    )

    assert lb._max_fail_count == 3
    assert lb._cooldown_seconds == 12.0
    assert lb._strategy == "sticky"
    assert lb._sticky_ttl == 600.0
    assert lb._sticky_cache_max_entries == 321


@pytest.mark.asyncio
async def test_update_config_rejects_unknown_strategy():
    lb = LoadBalancer()

    with pytest.raises(ValueError, match="lb_strategy"):
        await lb.update_config(strategy="random")
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/balancer/test_load_balancer_strategies.py::test_update_config_sets_strategy_and_sticky_limits tests/balancer/test_load_balancer_strategies.py::test_update_config_rejects_unknown_strategy -q
```

预期：FAIL。第一条失败应包含 `TypeError: LoadBalancer.update_config() got an unexpected keyword argument 'strategy'`。

- [ ] **步骤 3：编写最少实现代码**

在 `balancer/load_balancer.py` 中增加常量和初始化字段，保留现有行为：

```python
VALID_STRATEGIES = {"round_robin", "backup", "sticky"}

class LoadBalancer:
    def __init__(self):
        self._health: dict[str, ChannelHealth] = defaultdict(ChannelHealth)
        self._lock = asyncio.Lock()
        self._max_fail_count: int = 5
        self._cooldown_seconds: float = 60.0
        self._strategy: str = "round_robin"
        self._sticky_ttl: float = 1800.0
        self._sticky_cache_max_entries: int = 10000
```

把 `update_config()` 签名改为：

```python
async def update_config(
    self,
    max_fail_count: int = 5,
    cooldown_seconds: int = 60,
    strategy: str = "round_robin",
    sticky_ttl: int = 1800,
    sticky_cache_max_entries: int = 10000,
):
    """热更新配置参数"""
    normalized_strategy = str(strategy).lower()
    if normalized_strategy not in VALID_STRATEGIES:
        raise ValueError(f"lb_strategy must be one of {sorted(VALID_STRATEGIES)}, got {strategy!r}")
    async with self._lock:
        self._max_fail_count = max_fail_count
        self._cooldown_seconds = float(cooldown_seconds)
        self._strategy = normalized_strategy
        self._sticky_ttl = float(sticky_ttl)
        self._sticky_cache_max_entries = int(sticky_cache_max_entries)
```

- [ ] **步骤 4：运行测试验证通过**

运行同一步骤 2 命令。

预期：2 passed。

- [ ] **步骤 5：运行现有锁测试**

运行：

```bash
uv run pytest tests/balancer/test_load_balancer.py::test_update_config_waits_for_balancer_lock -q
```

预期：PASS，证明扩展后的 `update_config()` 仍等待 `_lock`。

- [ ] **步骤 6：Commit**

```bash
git add balancer/load_balancer.py tests/balancer/test_load_balancer_strategies.py
git commit -m "test: cover load balancer strategy config"
```

---

### 任务 2：实现 backup 策略和共享优先级分组

**文件：**
- 修改：`balancer/load_balancer.py`
- 修改：`tests/balancer/test_load_balancer_strategies.py`

- [ ] **步骤 1：编写失败的测试**

追加测试：

```python
@pytest.mark.asyncio
async def test_backup_selects_highest_priority_then_weight_then_id():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_low = make_channel("low", priority=5, weight=100)
    ch_b = make_channel("b", priority=1, weight=5)
    ch_a = make_channel("a", priority=1, weight=5)
    ch_heavy = make_channel("heavy", priority=1, weight=10)

    selected = await lb.select_channel([ch_low, ch_b, ch_a, ch_heavy])

    assert selected.id == "heavy"


@pytest.mark.asyncio
async def test_backup_uses_id_as_stable_tiebreaker():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_b = make_channel("b", priority=1, weight=5)
    ch_a = make_channel("a", priority=1, weight=5)

    selected = await lb.select_channel([ch_b, ch_a])

    assert selected.id == "a"


@pytest.mark.asyncio
async def test_backup_falls_to_same_priority_next_before_lower_priority():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_a = make_channel("a", priority=1, weight=10)
    ch_b = make_channel("b", priority=1, weight=5)
    ch_low = make_channel("low", priority=10, weight=100)

    selected = await lb.select_channel([ch_a, ch_b, ch_low], exclude_ids={"a"})

    assert selected.id == "b"
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/balancer/test_load_balancer_strategies.py::test_backup_selects_highest_priority_then_weight_then_id tests/balancer/test_load_balancer_strategies.py::test_backup_uses_id_as_stable_tiebreaker tests/balancer/test_load_balancer_strategies.py::test_backup_falls_to_same_priority_next_before_lower_priority -q
```

预期：至少第一条 FAIL。现有 `select_channel()` 仍使用 SWRR 或单组原顺序，不会按 backup 排序。

- [ ] **步骤 3：编写最少实现代码**

在 `balancer/load_balancer.py` 中提取可用渠道和最高优先级组，更新 `select_channel()` 分支：

```python
async def select_channel(
    self,
    channels: list[Channel],
    exclude_ids: set[str] | None = None,
    client_ip: str | None = None,
    api_key_id: str | None = None,
    client_headers: dict[str, str] | None = None,
) -> Optional[Channel]:
    exclude_ids = exclude_ids or set()
    async with self._lock:
        top_group = self._get_top_priority_group(channels, exclude_ids)
        if not top_group:
            return None

        if self._strategy == "backup":
            return self._backup_select(top_group)
        return self._weighted_round_robin(top_group) if len(top_group) > 1 else top_group[0]


def _get_top_priority_group(self, channels: list[Channel], exclude_ids: set[str]) -> list[Channel]:
    available = [
        ch
        for ch in channels
        if ch.enabled
        and ch.id not in exclude_ids
        and self._health[ch.id].is_healthy(self._max_fail_count, self._cooldown_seconds)
    ]
    if not available:
        return []
    min_priority = min(ch.priority for ch in available)
    return [ch for ch in available if ch.priority == min_priority]


def _backup_select(self, channels: list[Channel]) -> Channel:
    return sorted(channels, key=lambda ch: (-ch.weight, ch.id))[0]
```

保留 `_weighted_round_robin()` 原实现。

- [ ] **步骤 4：运行测试验证通过**

运行同一步骤 2 命令。

预期：3 passed。

- [ ] **步骤 5：运行现有优先级与 SWRR 回归**

运行：

```bash
uv run pytest tests/balancer/test_load_balancer_health.py::TestSelectChannelPriority tests/balancer/test_load_balancer.py::test_weighted_round_robin_fairness tests/balancer/test_load_balancer.py::test_weighted_round_robin_balanced tests/balancer/test_load_balancer.py::test_weighted_round_robin_weight_distribution -q
```

预期：全部 PASS，默认 `round_robin` 行为未变。

- [ ] **步骤 6：Commit**

```bash
git add balancer/load_balancer.py tests/balancer/test_load_balancer_strategies.py
git commit -m "feat: add backup load balancing strategy"
```

---

### 任务 3：实现 sticky key 脱敏构造

**文件：**
- 修改：`balancer/load_balancer.py`
- 修改：`tests/balancer/test_load_balancer_strategies.py`

- [ ] **步骤 1：编写失败的测试**

追加测试：

```python
import hashlib


def test_build_session_fingerprint_prefers_x_session_id_and_hashes_value():
    lb = LoadBalancer()

    fingerprint = lb._build_session_fingerprint(
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={
            "X-Session-ID": "session-secret",
            "x-claude-code-session-id": "claude-session",
            "Authorization": "Bearer raw-secret",
            "x-api-key": "raw-api-key",
            "User-Agent": "agent",
        },
    )

    assert fingerprint == hashlib.sha256(b'{"session":"session-secret"}').hexdigest()
    assert "session-secret" not in fingerprint
    assert "raw-secret" not in fingerprint
    assert "raw-api-key" not in fingerprint


def test_build_session_fingerprint_uses_structured_non_sensitive_fallback():
    lb = LoadBalancer()

    fingerprint = lb._build_session_fingerprint(
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={
            "Authorization": "Bearer raw-secret",
            "x-api-key": "raw-api-key",
            "User-Agent": "agent|None",
        },
    )

    expected = hashlib.sha256(
        b'{"api_key_id":"key-name","client_ip":"10.0.0.5","user_agent":"agent|None"}'
    ).hexdigest()
    assert fingerprint == expected
    assert "raw-secret" not in fingerprint
    assert "raw-api-key" not in fingerprint
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/balancer/test_load_balancer_strategies.py::test_build_session_fingerprint_prefers_x_session_id_and_hashes_value tests/balancer/test_load_balancer_strategies.py::test_build_session_fingerprint_uses_structured_non_sensitive_fallback -q
```

预期：FAIL，报错包含 `AttributeError: 'LoadBalancer' object has no attribute '_build_session_fingerprint'`。

- [ ] **步骤 3：编写最少实现代码**

在 `balancer/load_balancer.py` 顶部添加 imports：

```python
import hashlib
import json
```

增加方法：

```python
def _normalize_headers(self, client_headers: dict[str, str] | None) -> dict[str, str]:
    if not client_headers:
        return {}
    return {str(k).lower(): str(v) for k, v in client_headers.items()}


def _build_session_fingerprint(
    self,
    *,
    client_ip: str | None,
    api_key_id: str | None,
    client_headers: dict[str, str] | None,
) -> str:
    headers = self._normalize_headers(client_headers)
    explicit_session = headers.get("x-session-id") or headers.get("x-claude-code-session-id")
    if explicit_session:
        canonical = json.dumps(
            {"session": explicit_session[:512]},
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        canonical = json.dumps(
            {
                "api_key_id": api_key_id or "",
                "client_ip": client_ip or "",
                "user_agent": headers.get("user-agent", ""),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

- [ ] **步骤 4：运行测试验证通过**

运行同一步骤 2 命令。

预期：2 passed。

- [ ] **步骤 5：Commit**

```bash
git add balancer/load_balancer.py tests/balancer/test_load_balancer_strategies.py
git commit -m "feat: add safe sticky session fingerprint"
```

---

### 任务 4：实现 sticky HRW 选择并保持 priority 分层

**文件：**
- 修改：`balancer/load_balancer.py`
- 修改：`tests/balancer/test_load_balancer_strategies.py`

- [ ] **步骤 1：编写失败的测试**

追加测试：

```python
@pytest.mark.asyncio
async def test_sticky_same_session_selects_same_channel():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b"), make_channel("c")]

    first = await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"user-agent": "agent"},
    )
    second = await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"user-agent": "agent"},
    )

    assert second.id == first.id


@pytest.mark.asyncio
async def test_sticky_never_crosses_priority_when_high_priority_available():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    high_a = make_channel("high-a", priority=1)
    high_b = make_channel("high-b", priority=1)
    low = make_channel("low", priority=10, weight=1000)

    for i in range(50):
        selected = await lb.select_channel(
            [low, high_a, high_b],
            client_ip=f"10.0.0.{i}",
            api_key_id="key-name",
            client_headers={"user-agent": f"agent-{i}"},
        )
        assert selected.id in {"high-a", "high-b"}


@pytest.mark.asyncio
async def test_sticky_exclude_id_reselects_within_same_priority():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]
    first = await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    second = await lb.select_channel(
        channels,
        exclude_ids={first.id},
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    assert second.id != first.id
    assert second.id in {"a", "b"}
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/balancer/test_load_balancer_strategies.py::test_sticky_same_session_selects_same_channel tests/balancer/test_load_balancer_strategies.py::test_sticky_never_crosses_priority_when_high_priority_available tests/balancer/test_load_balancer_strategies.py::test_sticky_exclude_id_reselects_within_same_priority -q
```

预期：FAIL。至少 `test_sticky_never_crosses_priority_when_high_priority_available` 或 `test_sticky_exclude_id_reselects_within_same_priority` 会暴露 sticky 尚未实现。

- [ ] **步骤 3：编写最少实现代码**

在 `balancer/load_balancer.py` 顶部添加：

```python
import math
```

增加 HRW 方法：

```python
def _sticky_select_by_hrw(self, session_key: str, candidates: list[Channel]) -> Channel:
    best_channel: Optional[Channel] = None
    best_score: float | None = None
    for channel in candidates:
        digest = hashlib.sha256(f"{session_key}:{channel.id}".encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], "big") / 2**64
        value = max(value, 1e-12)
        score = -math.log(value) / max(channel.weight, 1)
        if best_score is None or score < best_score:
            best_score = score
            best_channel = channel
    return best_channel
```

更新 `select_channel()` 分支：

```python
if self._strategy == "backup":
    return self._backup_select(top_group)
if self._strategy == "sticky":
    session_key = self._build_session_fingerprint(
        client_ip=client_ip,
        api_key_id=api_key_id,
        client_headers=client_headers,
    )
    return self._sticky_select_by_hrw(session_key, top_group)
return self._weighted_round_robin(top_group) if len(top_group) > 1 else top_group[0]
```

- [ ] **步骤 4：运行测试验证通过**

运行同一步骤 2 命令。

预期：3 passed。

- [ ] **步骤 5：Commit**

```bash
git add balancer/load_balancer.py tests/balancer/test_load_balancer_strategies.py
git commit -m "feat: add sticky HRW channel selection"
```

---

### 任务 5：实现 sticky TTL + LRU 缓存和配置切换清理

**文件：**
- 修改：`balancer/load_balancer.py`
- 修改：`tests/balancer/test_load_balancer_strategies.py`

- [ ] **步骤 1：编写失败的测试**

追加测试：

```python
import time


@pytest.mark.asyncio
async def test_sticky_cache_stores_only_fingerprint_not_raw_secrets():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]

    await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={
            "authorization": "Bearer raw-secret",
            "x-api-key": "raw-api-key",
            "x-session-id": "session-secret",
        },
    )

    assert len(lb._sticky_cache) == 1
    cached_key = next(iter(lb._sticky_cache.keys()))
    assert "raw-secret" not in cached_key
    assert "raw-api-key" not in cached_key
    assert "session-secret" not in cached_key
    assert len(cached_key) == 64


@pytest.mark.asyncio
async def test_sticky_cache_entry_is_ignored_when_channel_excluded():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]
    first = await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    second = await lb.select_channel(
        channels,
        exclude_ids={first.id},
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    assert second.id != first.id


@pytest.mark.asyncio
async def test_sticky_cache_lru_eviction_respects_max_entries():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky", sticky_cache_max_entries=2)
    channels = [make_channel("a"), make_channel("b")]

    for i in range(3):
        await lb.select_channel(
            channels,
            client_ip=f"10.0.0.{i}",
            api_key_id="key-name",
            client_headers={"x-session-id": f"session-{i}"},
        )

    assert len(lb._sticky_cache) == 2


@pytest.mark.asyncio
async def test_update_config_clears_sticky_cache_when_strategy_or_ttl_changes():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    await lb.select_channel(
        [make_channel("a"), make_channel("b")],
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    assert lb._sticky_cache

    await lb.update_config(strategy="round_robin")
    assert not lb._sticky_cache

    await lb.update_config(strategy="sticky")
    await lb.select_channel(
        [make_channel("a"), make_channel("b")],
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    assert lb._sticky_cache

    await lb.update_config(strategy="sticky", sticky_ttl=900)
    assert not lb._sticky_cache
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/balancer/test_load_balancer_strategies.py::test_sticky_cache_stores_only_fingerprint_not_raw_secrets tests/balancer/test_load_balancer_strategies.py::test_sticky_cache_entry_is_ignored_when_channel_excluded tests/balancer/test_load_balancer_strategies.py::test_sticky_cache_lru_eviction_respects_max_entries tests/balancer/test_load_balancer_strategies.py::test_update_config_clears_sticky_cache_when_strategy_or_ttl_changes -q
```

预期：FAIL，报错包含 `_sticky_cache` 不存在或缓存未清理。

- [ ] **步骤 3：编写最少实现代码**

在 `balancer/load_balancer.py` 顶部添加：

```python
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
```

新增 dataclass：

```python
@dataclass
class StickyCacheEntry:
    channel_id: str
    last_active_at: float
```

在 `__init__()` 中添加：

```python
self._sticky_cache: OrderedDict[str, StickyCacheEntry] = OrderedDict()
```

更新 `update_config()`：在锁内计算配置是否变化，变化时清空 sticky 缓存：

```python
clear_sticky_cache = (
    normalized_strategy != self._strategy
    or float(sticky_ttl) != self._sticky_ttl
    or int(sticky_cache_max_entries) != self._sticky_cache_max_entries
)
self._max_fail_count = max_fail_count
self._cooldown_seconds = float(cooldown_seconds)
self._strategy = normalized_strategy
self._sticky_ttl = float(sticky_ttl)
self._sticky_cache_max_entries = int(sticky_cache_max_entries)
if clear_sticky_cache:
    self._sticky_cache.clear()
```

新增缓存方法：

```python
def _sticky_select_cached(self, session_key: str, candidates: list[Channel]) -> Channel:
    now = time.time()
    candidate_by_id = {ch.id: ch for ch in candidates}
    entry = self._sticky_cache.get(session_key)
    if entry and now - entry.last_active_at < self._sticky_ttl and entry.channel_id in candidate_by_id:
        entry.last_active_at = now
        self._sticky_cache.move_to_end(session_key)
        return candidate_by_id[entry.channel_id]
    if entry:
        self._sticky_cache.pop(session_key, None)

    selected = self._sticky_select_by_hrw(session_key, candidates)
    self._sticky_cache[session_key] = StickyCacheEntry(selected.id, now)
    self._sticky_cache.move_to_end(session_key)
    self._trim_sticky_cache(now)
    return selected


def _trim_sticky_cache(self, now: float) -> None:
    expired = []
    for key, entry in self._sticky_cache.items():
        if len(expired) >= 100:
            break
        if now - entry.last_active_at >= self._sticky_ttl:
            expired.append(key)
    for key in expired:
        self._sticky_cache.pop(key, None)
    while len(self._sticky_cache) > self._sticky_cache_max_entries:
        self._sticky_cache.popitem(last=False)
```

更新 `select_channel()` sticky 分支调用 `_sticky_select_cached()`。

- [ ] **步骤 4：运行测试验证通过**

运行同一步骤 2 命令。

预期：4 passed。

- [ ] **步骤 5：运行完整 balancer 测试**

运行：

```bash
uv run pytest tests/balancer -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add balancer/load_balancer.py tests/balancer/test_load_balancer_strategies.py
git commit -m "feat: add sticky cache management"
```

---

### 任务 6：接入配置 schema、约束和热更新

**文件：**
- 修改：`config.py`
- 修改：`tests/test_settings.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_settings.py` 中追加或扩展设置 schema 测试：

```python
def test_lb_strategy_settings_schema_defaults():
    from config import _CONFIG_SCHEMA

    assert _CONFIG_SCHEMA["lb_strategy"]["default"] == "round_robin"
    assert _CONFIG_SCHEMA["lb_strategy"]["requires_restart"] is False
    assert _CONFIG_SCHEMA["sticky_ttl"]["default"] == 1800
    assert _CONFIG_SCHEMA["sticky_cache_max_entries"]["default"] == 10000


@pytest.mark.asyncio
async def test_update_settings_validates_lb_strategy(monkeypatch):
    import config

    config._settings = {key: schema["default"] for key, schema in config._CONFIG_SCHEMA.items()}

    with pytest.raises(ValueError, match="lb_strategy"):
        await config.update_settings({"lb_strategy": "random"})


@pytest.mark.asyncio
async def test_apply_lb_settings_passes_strategy_and_sticky_values(monkeypatch):
    import config

    calls = []

    class FakeBalancer:
        async def update_config(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("balancer.load_balancer.load_balancer", FakeBalancer())
    config._settings = {
        **{key: schema["default"] for key, schema in config._CONFIG_SCHEMA.items()},
        "max_fail_count": 7,
        "cooldown_seconds": 88,
        "lb_strategy": "sticky",
        "sticky_ttl": 600,
        "sticky_cache_max_entries": 1234,
    }

    await config._apply_lb_settings()

    assert calls == [{
        "max_fail_count": 7,
        "cooldown_seconds": 88,
        "strategy": "sticky",
        "sticky_ttl": 600,
        "sticky_cache_max_entries": 1234,
    }]
```

如果 `tests/test_settings.py` 尚未导入 `pytest`，在文件顶部添加 `import pytest`。

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_settings.py::test_lb_strategy_settings_schema_defaults tests/test_settings.py::test_update_settings_validates_lb_strategy tests/test_settings.py::test_apply_lb_settings_passes_strategy_and_sticky_values -q
```

预期：FAIL，第一条应因 `_CONFIG_SCHEMA` 缺少 `lb_strategy` 失败。

- [ ] **步骤 3：编写最少实现代码**

在 `config.py` 的 `_CONFIG_SCHEMA` 中 `cooldown_seconds` 后加入：

```python
"lb_strategy": {"type": "str", "default": "round_robin", "requires_restart": False},
"sticky_ttl": {"type": "int", "default": 1800, "requires_restart": False},
"sticky_cache_max_entries": {"type": "int", "default": 10000, "requires_restart": False},
```

在 `_CONFIG_CONSTRAINTS` 中加入：

```python
"lb_strategy": {"choices": ("round_robin", "backup", "sticky")},
"sticky_ttl": {"min": 60, "max": 86400},
"sticky_cache_max_entries": {"min": 100, "max": 1000000},
```

更新 `_apply_lb_settings()`：

```python
await load_balancer.update_config(
    max_fail_count=_settings.get("max_fail_count", 5),
    cooldown_seconds=_settings.get("cooldown_seconds", 60),
    strategy=_settings.get("lb_strategy", "round_robin"),
    sticky_ttl=_settings.get("sticky_ttl", 1800),
    sticky_cache_max_entries=_settings.get("sticky_cache_max_entries", 10000),
)
```

- [ ] **步骤 4：运行测试验证通过**

运行同一步骤 2 命令。

预期：3 passed。

- [ ] **步骤 5：运行相关 settings 测试**

运行：

```bash
uv run pytest tests/test_settings.py -q
```

预期：全部 PASS。

- [ ] **步骤 6：Commit**

```bash
git add config.py tests/test_settings.py
git commit -m "feat: add load balancing settings"
```

---

### 任务 7：在 proxy_core 传递选择渠道上下文

**文件：**
- 修改：`proxy_core.py`
- 修改：`tests/test_proxy_core.py`

- [ ] **步骤 1：编写失败的测试**

在 `tests/test_proxy_core.py` 中追加测试。使用现有 helper 风格；如果文件已有 `_make_channel` 可复用，则复用，否则在测试内构造 `Channel`：

```python
@pytest.mark.asyncio
async def test_single_model_select_channel_receives_request_context(monkeypatch):
    from unittest.mock import AsyncMock

    from models.api_types import APIType
    from models.channel import Channel
    import proxy_core

    channel = Channel(
        id="ch_ctx",
        name="Context Channel",
        api_type="openai-chat-completions",
        base_url="http://example.com",
        api_key="key",
        models=["gpt-4"],
        enabled=True,
        weight=1,
        priority=1,
    )
    captured = {}

    async def fake_get_channels_for_model(model):
        return [channel]

    async def fake_select_channel(channels, exclude_ids=None, **kwargs):
        captured.update(kwargs)
        return channel

    async def fake_do_request(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(proxy_core, "_get_channels_for_model", fake_get_channels_for_model)
    monkeypatch.setattr(proxy_core.load_balancer, "select_channel", fake_select_channel)
    monkeypatch.setattr(proxy_core, "_do_request", fake_do_request)
    monkeypatch.setattr(proxy_core.storage, "get_model_group_by_name", AsyncMock(return_value=None))

    await proxy_core.proxy_request(
        "gpt-4",
        {"model": "gpt-4"},
        APIType.OPENAI_CHAT,
        client_headers={"x-session-id": "s1"},
        api_key_id="key-name",
        client_ip="10.0.0.5",
    )

    assert captured == {
        "client_ip": "10.0.0.5",
        "api_key_id": "key-name",
        "client_headers": {"x-session-id": "s1"},
    }
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_proxy_core.py::test_single_model_select_channel_receives_request_context -q
```

预期：FAIL，`captured == {}` 或缺少参数。

- [ ] **步骤 3：编写最少实现代码**

在 `proxy_core.py` 两处 `select_channel()` 调用增加参数。

单模型路径：

```python
selected = await load_balancer.select_channel(
    channels,
    exclude_ids=all_tried,
    client_ip=client_ip,
    api_key_id=api_key_id,
    client_headers=client_headers,
)
```

模型组路径：

```python
selected = await load_balancer.select_channel(
    channels,
    exclude_ids=tried_channels,
    client_ip=client_ip,
    api_key_id=api_key_id,
    client_headers=client_headers,
)
```

- [ ] **步骤 4：运行测试验证通过**

运行同一步骤 2 命令。

预期：PASS。

- [ ] **步骤 5：为模型组路径补红灯测试**

追加测试：

```python
@pytest.mark.asyncio
async def test_model_group_select_channel_receives_request_context(monkeypatch):
    from unittest.mock import AsyncMock

    from models.api_types import APIType
    from models.channel import Channel
    from models.model_group import ModelGroup
    import proxy_core

    channel = Channel(
        id="ch_ctx_group",
        name="Context Group Channel",
        api_type="openai-chat-completions",
        base_url="http://example.com",
        api_key="key",
        models=["gpt-4"],
        enabled=True,
        weight=1,
        priority=1,
    )
    group = ModelGroup(id="grp", name="group-model", models=["gpt-4"], enabled=True)
    captured = {}

    async def fake_get_channels_for_model(model):
        return [channel]

    async def fake_select_channel(channels, exclude_ids=None, **kwargs):
        captured.update(kwargs)
        return channel

    async def fake_do_request(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(proxy_core, "_get_channels_for_model", fake_get_channels_for_model)
    monkeypatch.setattr(proxy_core.load_balancer, "select_channel", fake_select_channel)
    monkeypatch.setattr(proxy_core, "_do_request", fake_do_request)
    monkeypatch.setattr(proxy_core.storage, "get_model_group_by_name", AsyncMock(return_value=group))

    await proxy_core.proxy_request(
        "group-model",
        {"model": "group-model"},
        APIType.OPENAI_CHAT,
        client_headers={"x-session-id": "s1"},
        api_key_id="key-name",
        client_ip="10.0.0.5",
    )

    assert captured == {
        "client_ip": "10.0.0.5",
        "api_key_id": "key-name",
        "client_headers": {"x-session-id": "s1"},
    }
```

- [ ] **步骤 6：运行模型组测试验证失败再通过**

如果步骤 3 已经改了两处，测试可能直接 PASS。若直接 PASS，说明模型组路径已被同步覆盖；记录为本任务第二个回归覆盖。运行：

```bash
uv run pytest tests/test_proxy_core.py::test_model_group_select_channel_receives_request_context -q
```

预期：PASS。

- [ ] **步骤 7：运行相关 proxy_core 测试**

运行：

```bash
uv run pytest tests/test_proxy_core.py::test_single_model_select_channel_receives_request_context tests/test_proxy_core.py::test_model_group_select_channel_receives_request_context -q
```

预期：2 passed。

- [ ] **步骤 8：Commit**

```bash
git add proxy_core.py tests/test_proxy_core.py
git commit -m "feat: pass sticky context to load balancer"
```

---

### 任务 8：确保 CombinedMiddleware 写入 client_ip

**文件：**
- 修改：`main.py`
- 修改：`tests/routers/test_proxy_base.py`

- [ ] **步骤 1：编写失败的测试**

优先在 `tests/routers/test_proxy_base.py` 使用现有 TestClient 路由测试模式追加测试，patch `routers.proxy_base.proxy_request` 捕获 `client_ip`。示例：

```python
def test_proxy_request_receives_client_ip_from_request(client, monkeypatch):
    captured = {}

    async def fake_proxy_request(*args, **kwargs):
        captured.update(kwargs)
        from models.channel import Channel
        channel = Channel(
            id="ch",
            name="Channel",
            api_type="openai-chat-completions",
            base_url="http://example.com",
            api_key="key",
            models=["gpt-4"],
        )
        return {"id": "chatcmpl-test", "choices": []}, channel

    monkeypatch.setattr("routers.proxy_base.proxy_request", fake_proxy_request)

    response = client.post("/v1/chat/completions", json={"model": "gpt-4", "messages": []})

    assert response.status_code == 200
    assert captured["client_ip"]
```

如果该文件 fixture 不是同步 `client`，按现有测试风格调整为 `async_client` 或 `e2e_client`。

- [ ] **步骤 2：运行测试验证当前行为**

运行具体测试：

```bash
uv run pytest tests/routers/test_proxy_base.py::test_proxy_request_receives_client_ip_from_request -q
```

预期：如果当前路由已通过 `request.client.host` 传入，测试可能 PASS。若 PASS，本任务只需补充 `scope["state"]["client_ip"]` 的 middleware 级测试或源码断言；若 FAIL，按步骤 3 修。

- [ ] **步骤 3：补充或修复 middleware 写入**

在 `main.py` 中 `client_ip = (scope.get("client") or ("", 0))[0]` 后、`scope.setdefault("state", {})` 后确保写入：

```python
scope.setdefault("state", {})
scope["state"]["client_ip"] = client_ip
```

如果已有 `scope.setdefault("state", {})` 在后面，移动或合并，避免重复初始化覆盖。

- [ ] **步骤 4：运行测试验证通过**

运行步骤 2 命令。

预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add main.py tests/routers/test_proxy_base.py
git commit -m "feat: expose client ip in proxy state"
```

---

### 任务 9：设置页 UI 和前端保存逻辑

**文件：**
- 修改：`static/fragments/admin/settings.html`
- 修改：`static/js/settings.js`
- 新增或修改：`tests/test_admin_frontend_regressions.py`

- [ ] **步骤 1：编写失败的源码回归测试**

如果 `tests/test_admin_frontend_regressions.py` 不存在，新建文件。加入：

```python
from pathlib import Path


def test_settings_page_contains_lb_strategy_controls():
    html = Path("static/fragments/admin/settings.html").read_text(encoding="utf-8")

    assert 'id="set_lb_strategy"' in html
    assert 'id="set_sticky_ttl"' in html
    assert 'id="set_sticky_cache_max_entries"' in html
    assert 'id="sticky_lb_options"' in html
    assert 'value="round_robin"' in html
    assert 'value="backup"' in html
    assert 'value="sticky"' in html


def test_settings_js_loads_saves_and_toggles_lb_strategy_controls():
    js = Path("static/js/settings.js").read_text(encoding="utf-8")

    assert "syncLbStrategyMode" in js
    assert "set_lb_strategy" in js
    assert "set_sticky_ttl" in js
    assert "set_sticky_cache_max_entries" in js
    assert "data.lb_strategy" in js
    assert "data.sticky_ttl" in js
    assert "data.sticky_cache_max_entries" in js
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
uv run pytest tests/test_admin_frontend_regressions.py::test_settings_page_contains_lb_strategy_controls tests/test_admin_frontend_regressions.py::test_settings_js_loads_saves_and_toggles_lb_strategy_controls -q
```

预期：FAIL，缺少对应控件和 JS 标识。

- [ ] **步骤 3：修改 settings.html**

在负载均衡 Tab 的说明和失败阈值之前插入策略控件：

```html
<div class="grid grid-cols-1 sm:grid-cols-2 gap-5 mb-5">
  <div>
    <label class="block text-sm font-medium text-ink-900 mb-1.5">负载均衡策略</label>
    <select id="set_lb_strategy" data-section="lb" onchange="syncLbStrategyMode()" class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
      <option value="round_robin">加权轮询（默认）</option>
      <option value="backup">顺序备份</option>
      <option value="sticky">会话粘性</option>
    </select>
    <p id="lb_strategy_help" class="text-xs text-ink-400 mt-1">同优先级内按权重轮询分发流量，低优先级渠道作为备份。</p>
  </div>
</div>
<div id="sticky_lb_options" class="grid grid-cols-1 sm:grid-cols-2 gap-5 mb-5 hidden">
  <div>
    <label class="block text-sm font-medium text-ink-900 mb-1.5">粘性有效期</label>
    <input type="number" id="set_sticky_ttl" min="60" data-section="lb" class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
    <p class="text-xs text-ink-400 mt-1">单位：秒，超过此时长无请求的会话会重新分配渠道</p>
  </div>
  <div>
    <label class="block text-sm font-medium text-ink-900 mb-1.5">粘性缓存容量</label>
    <input type="number" id="set_sticky_cache_max_entries" min="100" data-section="lb" class="settings-input w-full text-sm border border-surface-200 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-brand-500/30 focus:border-brand-500 bg-white">
    <p class="text-xs text-ink-400 mt-1">达到上限后按 LRU 淘汰最久未访问的会话</p>
  </div>
</div>
```

- [ ] **步骤 4：修改 settings.js**

新增函数：

```javascript
function syncLbStrategyMode() {
  const strategyEl = document.getElementById('set_lb_strategy');
  const stickyOptions = document.getElementById('sticky_lb_options');
  const help = document.getElementById('lb_strategy_help');
  if (!strategyEl || !stickyOptions || !help) return;
  const strategy = strategyEl.value || 'round_robin';
  stickyOptions.classList.toggle('hidden', strategy !== 'sticky');
  const descriptions = {
    round_robin: '同优先级内按权重轮询分发流量，低优先级渠道作为备份。',
    backup: '按优先级和权重排序，始终使用最靠前的健康渠道，失败后才切换。',
    sticky: '同一会话优先路由到同一渠道，用于复用上游缓存；会话标识会先脱敏再参与路由。',
  };
  help.textContent = descriptions[strategy] || descriptions.round_robin;
}
```

在 `initSettings()` 中调用 `syncLbStrategyMode()`。

在 `_detectSettingsDirty()` 中加入：

```javascript
const lbStrategy = document.getElementById('set_lb_strategy')?.value || 'round_robin';
if (lbStrategy !== (orig.lb_strategy || 'round_robin')) _settingsDirtySections.add('lb');
const stickyTtl = parseInt(document.getElementById('set_sticky_ttl')?.value) || 1800;
if (stickyTtl !== (orig.sticky_ttl ?? 1800)) _settingsDirtySections.add('lb');
const stickyCacheMax = parseInt(document.getElementById('set_sticky_cache_max_entries')?.value) || 10000;
if (stickyCacheMax !== (orig.sticky_cache_max_entries ?? 10000)) _settingsDirtySections.add('lb');
```

在 `loadSettings()` 中设置：

```javascript
document.getElementById('set_lb_strategy').value = data.lb_strategy || 'round_robin';
document.getElementById('set_sticky_ttl').value = data.sticky_ttl ?? 1800;
document.getElementById('set_sticky_cache_max_entries').value = data.sticky_cache_max_entries ?? 10000;
syncLbStrategyMode();
```

在 `saveSettings()` 中加入：

```javascript
const lbStrategy = document.getElementById('set_lb_strategy').value || 'round_robin';
if (lbStrategy !== (orig.lb_strategy || 'round_robin')) data.lb_strategy = lbStrategy;
const stickyTtl = parseInt(document.getElementById('set_sticky_ttl').value) || 1800;
if (stickyTtl !== (orig.sticky_ttl ?? 1800)) data.sticky_ttl = stickyTtl;
const stickyCacheMax = parseInt(document.getElementById('set_sticky_cache_max_entries').value) || 10000;
if (stickyCacheMax !== (orig.sticky_cache_max_entries ?? 10000)) data.sticky_cache_max_entries = stickyCacheMax;
```

在文件底部 `Object.assign(window, {` 代码块中，改成以下完整导出列表：

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

- [ ] **步骤 5：运行测试验证通过**

运行步骤 2 命令。

预期：2 passed。

- [ ] **步骤 6：运行 JS 语法检查**

运行：

```bash
node --check static/js/settings.js
```

预期：exit 0，无语法错误。

- [ ] **步骤 7：Commit**

```bash
git add static/fragments/admin/settings.html static/js/settings.js tests/test_admin_frontend_regressions.py
git commit -m "feat: add load balancing settings UI"
```

---

### 任务 10：补齐集成验证和 lint

**文件：**
- 修改：按前面任务遗留修复决定

- [ ] **步骤 1：运行负载均衡完整测试**

运行：

```bash
uv run pytest tests/balancer -q
```

预期：全部 PASS。

- [ ] **步骤 2：运行设置与代理上下文测试**

运行：

```bash
uv run pytest tests/test_settings.py tests/test_proxy_core.py::test_single_model_select_channel_receives_request_context tests/test_proxy_core.py::test_model_group_select_channel_receives_request_context tests/test_admin_frontend_regressions.py -q
```

预期：全部 PASS。

- [ ] **步骤 3：运行 Python 编译检查**

运行：

```bash
python -m py_compile balancer/load_balancer.py config.py proxy_core.py main.py
```

预期：exit 0。

- [ ] **步骤 4：运行 ruff**

运行：

```bash
uv run ruff check balancer/load_balancer.py config.py proxy_core.py main.py tests/balancer/test_load_balancer_strategies.py tests/test_settings.py tests/test_proxy_core.py tests/test_admin_frontend_regressions.py
```

预期：exit 0。若有 lint 错误，只做最小修复并重跑本步骤。

- [ ] **步骤 5：运行 JS 语法检查**

运行：

```bash
node --check static/js/settings.js
```

预期：exit 0。

- [ ] **步骤 6：运行最终相关回归**

运行：

```bash
uv run pytest tests/balancer tests/test_settings.py tests/test_proxy_core.py tests/routers/test_proxy_base.py tests/test_admin_frontend_regressions.py -q
```

预期：全部 PASS。

- [ ] **步骤 7：检查 diff**

运行：

```bash
git diff -- balancer/load_balancer.py config.py proxy_core.py main.py static/fragments/admin/settings.html static/js/settings.js tests/balancer/test_load_balancer_strategies.py tests/test_settings.py tests/test_proxy_core.py tests/routers/test_proxy_base.py tests/test_admin_frontend_regressions.py
```

检查点：

- `round_robin` 默认路径仍使用现有 SWRR 行为。
- `sticky` 只在最高优先级健康组内选择。
- `_sticky_cache` key 是 SHA-256 摘要，不含原始 header。
- `proxy_core.py` 只增加上下文传参，不改变 `_do_request()` 和 fallback 错误处理。
- 设置页没有嵌套卡片，新增控件都在负载均衡 Tab 内。

- [ ] **步骤 8：最终 Commit**

如果前面每个任务都已 commit，本步骤只提交验证中产生的修复：

```bash
git add balancer/load_balancer.py config.py proxy_core.py main.py static/fragments/admin/settings.html static/js/settings.js tests/balancer/test_load_balancer_strategies.py tests/test_settings.py tests/test_proxy_core.py tests/routers/test_proxy_base.py tests/test_admin_frontend_regressions.py
git commit -m "test: verify load balancing strategy integration"
```

如果没有验证修复产生，跳过 commit，并在最终汇报中说明没有额外变更。
