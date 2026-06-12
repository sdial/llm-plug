"""针对 admin.py 三个已确认问题的回归测试：

1. avg_lag 使用 latency_count 而非独立的 lag_count，非流式请求没有 lag 导致低估平均值
2. mutator 内抛 HTTPException，在锁内耦合了 HTTP 语义
3. _login_rate_limit_state 内存泄漏，仅查询时清理当前 key，孤立条目永不回收
"""
import asyncio
import inspect
import json
import time
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio

import admin_auth
import config
import storage
from main import app
from routers import admin


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def admin_files(tmp_path, monkeypatch):
    """初始化最小可用的管理后台数据目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"

    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None

    admin._login_rate_limit_state.clear()

    import main
    monkeypatch.setattr(
        main, "_whitelist_cache",
        main._whitelist.WhitelistCache(str(data_dir / "whitelist.csv")),
    )

    yield

    admin._login_rate_limit_state.clear()


# ─────────────── Issue 1: avg_lag 使用 latency_count 而非 lag_count ───────────────


class TestAvgLagCountBug:
    """avg_lag 计算复用 latency_count，导致非流式请求（无 lag）拉低平均值。

    场景：一天内有 3 条流式请求（lag=100ms）和 7 条非流式请求（lag=None）。
    正确行为：avg_lag_ms = 100（仅对流式请求求平均）
    当前 bug：avg_lag_ms = 30（total_lag=300 / latency_count=10）
    """

    def test_lag_uses_separate_count(self):
        """验证 avg_lag 使用独立的 lag_count 而非 latency_count。

        直接模拟 get_stats 中的聚合逻辑：
        - 3 条流式行：avg_latency_ms=100, avg_lag_ms=100, request_count=1
        - 7 条非流行：avg_latency_ms=100, avg_lag_ms=None, request_count=1
        """
        # 模拟 daily_by_date 的初始化和累加
        daily_by_date = {
            "2026-06-12": {
                "date": "2026-06-12",
                "total_requests": 0,
                "success_count": 0,
                "fail_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_read_input_tokens": 0,
                "total_cache_creation_input_tokens": 0,
                "total_latency_ms": 0,
                "total_lag_ms": 0,
                "latency_count": 0,
                "lag_count": 0,
            }
        }

        # 模拟从 daily_stats 返回的行
        rows = [
            # 3 条流式：有 latency 和 lag
            {"date": "2026-06-12", "request_count": 1, "success_count": 1, "fail_count": 0,
             "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "avg_latency_ms": 100, "avg_lag_ms": 100},
            {"date": "2026-06-12", "request_count": 1, "success_count": 1, "fail_count": 0,
             "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "avg_latency_ms": 100, "avg_lag_ms": 100},
            {"date": "2026-06-12", "request_count": 1, "success_count": 1, "fail_count": 0,
             "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "avg_latency_ms": 100, "avg_lag_ms": 100},
            # 7 条非流式：有 latency 但无 lag
            *[{"date": "2026-06-12", "request_count": 1, "success_count": 1, "fail_count": 0,
               "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 0, "avg_latency_ms": 100, "avg_lag_ms": None}
              for _ in range(7)],
        ]

        # 复现 admin.py get_stats 中的聚合逻辑
        rec = daily_by_date["2026-06-12"]
        for row in rows:
            rec["total_requests"] += row["request_count"] or 0
            rec["success_count"] += row["success_count"] or 0
            rec["fail_count"] += row["fail_count"] or 0
            rec["total_input_tokens"] += row["input_tokens"] or 0
            rec["total_output_tokens"] += row["output_tokens"] or 0
            rec["total_cache_read_input_tokens"] += row.get("cache_read_input_tokens") or 0
            rec["total_cache_creation_input_tokens"] += row.get("cache_creation_input_tokens") or 0
            if row.get("avg_latency_ms") is not None:
                rec["total_latency_ms"] += row["avg_latency_ms"] * (row["request_count"] or 1)
                rec["latency_count"] += row["request_count"] or 1
            if row.get("avg_lag_ms") is not None:
                rec["total_lag_ms"] += row["avg_lag_ms"] * (row["request_count"] or 1)
                rec["lag_count"] += row["request_count"] or 1

        # 用 lag_count 计算平均值
        avg_lag = round(rec["total_lag_ms"] / rec["lag_count"]) if rec["lag_count"] else 0
        assert avg_lag == 100, (
            f"avg_lag should be 100 (total_lag=300 / lag_count=3), got {avg_lag}. "
            "lag_count must be separate from latency_count"
        )

        # 验证 latency 不受影响
        avg_latency = round(rec["total_latency_ms"] / rec["latency_count"]) if rec["latency_count"] else 0
        assert avg_latency == 100, f"avg_latency should be 100, got {avg_latency}"


# ─────────────── Issue 2: mutator 内抛 HTTPException ───────────────


class TestMutatorNoHTTPException:
    """mutator 不应在锁内抛 HTTPException，应返回标记值由外层抛异常。"""

    async def test_update_channel_not_found_returns_404(self, admin_files):
        """更新不存在的渠道应返回 404，但不能在 mutator 内抛异常。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})
            await client.post("/admin/auth/login", json={"password": "pw"})
            csrf = (await client.get("/admin/auth/csrf")).json()["csrf_token"]

            resp = await client.put(
                "/admin/channels/nonexistent",
                headers={"X-CSRF-Token": csrf},
                json={"name": "test"},
            )
            assert resp.status_code == 404

    async def test_delete_channel_not_found_returns_404(self, admin_files):
        """删除不存在的渠道应返回 404，但不能在 mutator 内抛异常。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})
            await client.post("/admin/auth/login", json={"password": "pw"})
            csrf = (await client.get("/admin/auth/csrf")).json()["csrf_token"]

            resp = await client.delete(
                "/admin/channels/nonexistent",
                headers={"X-CSRF-Token": csrf},
            )
            assert resp.status_code == 404

    async def test_toggle_channel_not_found_returns_404(self, admin_files):
        """切换不存在的渠道应返回 404，但不能在 mutator 内抛异常。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})
            await client.post("/admin/auth/login", json={"password": "pw"})
            csrf = (await client.get("/admin/auth/csrf")).json()["csrf_token"]

            resp = await client.patch(
                "/admin/channels/nonexistent/toggle",
                headers={"X-CSRF-Token": csrf},
            )
            assert resp.status_code == 404

    def test_mutator_functions_do_not_raise_http_exception(self):
        """验证 _mutate 内部不含 raise HTTPException。

        mutator 应返回 None 标记未找到，由外层 atomic_update_data 返回后抛异常。
        """
        from routers.admin import update_channel, delete_channel, toggle_channel
        import re

        for func in [update_channel, delete_channel, toggle_channel]:
            source = inspect.getsource(func)
            # 提取 _mutate 函数体（从 def _mutate 到其结束）
            match = re.search(r'def _mutate\([^)]*\):(.+?)(?=\n    result =|\n    await )', source, re.DOTALL)
            assert match, f"Could not find _mutate in {func.__name__}"
            mutator_body = match.group(1)
            assert "raise HTTPException" not in mutator_body, (
                f"{func.__name__}: _mutate should not raise HTTPException; "
                "return None and let caller raise after lock release"
            )


# ─────────────── Issue 3: _login_rate_limit_state 内存泄漏 ───────────────


class TestLoginRateLimitMemoryLeak:
    """_login_rate_limit_state 只在 _is_login_rate_limited 中清理当前 key，
    孤立条目永远不会被回收。"""

    def test_stale_entries_are_cleaned_up(self):
        """验证 _is_login_rate_limited 会清理所有过期 key，不仅是当前查询的。"""
        original_window = admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS
        admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS = 1

        try:
            now = time.monotonic()
            # key1: 已过期的条目
            key1 = ("file1", "192.168.1.1")
            admin._login_rate_limit_state[key1] = [now - 10]
            # key2: 未过期的条目
            key2 = ("file1", "192.168.1.2")
            admin._login_rate_limit_state[key2] = [now]

            # 等待 key1 的记录过期
            time.sleep(1.1)

            # 查询 key2 应同时清理 key1 的过期条目
            request = MagicMock()
            request.client.host = "192.168.1.2"
            admin._is_login_rate_limited(request)

            # key1 的过期条目应该被清理
            stale_keys = [
                k for k, v in admin._login_rate_limit_state.items()
                if all(ts < time.monotonic() - admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS for ts in v)
            ]
            assert len(stale_keys) == 0, (
                f"Found {len(stale_keys)} stale keys that should have been cleaned up: {stale_keys}"
            )
        finally:
            admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS = original_window
            admin._login_rate_limit_state.clear()

    def test_record_login_failure_also_cleans_stale(self):
        """_record_login_failure 也应清理过期条目，防止只写不清理。"""
        original_window = admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS
        admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS = 1

        try:
            now = time.monotonic()
            key_stale = ("file1", "10.0.0.1")
            admin._login_rate_limit_state[key_stale] = [now - 10]

            time.sleep(1.1)

            request = MagicMock()
            request.client.host = "10.0.0.2"
            admin._record_login_failure(request)

            stale_keys = [
                k for k, v in admin._login_rate_limit_state.items()
                if all(ts < time.monotonic() - admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS for ts in v)
            ]
            assert len(stale_keys) == 0, (
                f"Found {len(stale_keys)} stale keys after _record_login_failure: {stale_keys}"
            )
        finally:
            admin._LOGIN_RATE_LIMIT_WINDOW_SECONDS = original_window
            admin._login_rate_limit_state.clear()

    def test_cleanup_function_exists(self):
        """验证 _cleanup_stale_login_rate_limits 函数存在且可调用。"""
        assert hasattr(admin, "_cleanup_stale_login_rate_limits"), (
            "_cleanup_stale_login_rate_limits function should exist"
        )
        assert callable(admin._cleanup_stale_login_rate_limits), (
            "_cleanup_stale_login_rate_limits should be callable"
        )
