"""REVIEW.md 中已确认的管理端安全/可靠性问题的回归测试。

测试断言**当前（有缺陷）的行为**：进程不重启、cookie 缺少 Secure 标志、
速率限制按 TCP-level client.host 计数等。每个测试的 docstring 描述具体问题。
当问题被修复后，相关测试会失败，提示修改者同时更新测试以匹配新行为。
"""

import asyncio
import inspect
import json

import httpx
import pytest

import admin_auth
import config
import storage
from main import app
from routers import admin


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def admin_files(tmp_path, monkeypatch):
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
    storage._channels_lock = asyncio.Lock()
    storage._keys_lock = asyncio.Lock()

    # 重置登录速率限制状态以避免跨测试污染
    admin._login_attempts.clear()

    import main

    main._whitelist_cache = main._whitelist.WhitelistCache(
        str(data_dir / "whitelist.csv")
    )
    yield


# ─────────────────────────── N1 ───────────────────────────


class TestN1RestartSilentlyFails:
    """N1: /admin/restart 静默失效，仅返回 200，不会真的终止进程。"""

    async def test_restart_endpoint_returns_200_synchronously(self, admin_files):
        """Bug N1: 调用 /admin/restart 同步返回 200, 不会 kill 当前进程。

        关键不变量：响应返回时, 进程还活着。如果代码改为同步 os.kill / SIGTERM,
        这个测试也能继续工作（FastAPI handler 不在同一线程内被 kill 之前会先 ack）。
        但当前 bug 是后台 task 内的 SystemExit 永远不会终止进程, 表现是
        即便后台 task 跑完, 服务依然存活。我们这里只断言 handler 返回 200,
        其余 bug 通过下面的源码检查覆盖。
        """
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})
            await client.post("/admin/auth/login", json={"password": "pw"})
            csrf = (await client.get("/admin/auth/csrf")).json()["csrf_token"]

            resp = await client.post(
                "/admin/restart",
                headers={"X-CSRF-Token": csrf},
                json={"confirm": True},
            )

        assert resp.status_code == 200
        assert resp.json() == {"message": "服务正在重启"}

    def test_restart_handler_uses_unreferenced_create_task(self):
        """Bug N1: asyncio.create_task 的返回值没被持有, 不在 _background_tasks 集合中。"""
        source = inspect.getsource(admin.restart_server)
        assert "asyncio.create_task(_shutdown_after_response())" in source
        # 关键 bug：task 创建后立刻被丢弃
        assert "_background_tasks.add" not in source
        # SystemExit 在 task 内部抛出, 会被 asyncio 异常机制吞掉
        assert "SystemExit" in source
        # 没有 os.kill / signal.SIGTERM 之类真正能终止进程的调用
        assert "os.kill" not in source
        assert "SIGTERM" not in source


# ─────────────────────────── N3 ───────────────────────────


class TestN3DnsRebindingSsrf:
    """N3: 管理端出站请求在 transport 层重复校验 DNS 解析结果。"""

    async def test_fetch_models_rejects_rebound_private_address_before_request(
        self, admin_files, monkeypatch
    ):
        """校验阶段为公网、请求阶段变为内网时，应在 transport 层拒绝。"""
        calls = 0
        request_reached_network = False

        def fake_getaddrinfo(host, port, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [(None, None, None, "", ("93.184.216.34", 443))]
            return [(None, None, None, "", ("127.0.0.1", 443))]

        monkeypatch.setattr(admin.socket, "getaddrinfo", fake_getaddrinfo)

        class FakeTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal request_reached_network
                request_reached_network = True
                return httpx.Response(200, json={"data": []}, request=request)

        monkeypatch.setattr(admin.httpx, "AsyncHTTPTransport", lambda: FakeTransport())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})
            await client.post("/admin/auth/login", json={"password": "pw"})
            csrf = (await client.get("/admin/auth/csrf")).json()["csrf_token"]

            resp = await client.post(
                "/admin/channels/fetch-models",
                headers={"X-CSRF-Token": csrf},
                json={
                    "base_url": "https://evil-but-public-now.example.com",
                    "models_url": "",
                    "api_key": "sk-x",
                    "api_type": "openai-chat-completions",
                },
            )

        assert resp.status_code == 400
        assert "内网或本机地址" in resp.json()["detail"]
        assert request_reached_network is False


# ─────────────────────────── N5 ───────────────────────────


class TestN5RateLimitByTcpClientHost:
    """N5: 登录速率限制按 request.client.host 计数, 反代后所有用户共享。"""

    def test_rate_limit_key_does_not_consult_forwarded_headers(self):
        """Bug N5: _client_ip 不读 X-Forwarded-For / X-Real-IP。"""
        source = inspect.getsource(admin._client_ip)
        assert "request.client.host" in source
        assert "X-Forwarded-For" not in source
        assert "X-Real-IP" not in source
        assert "x-forwarded-for" not in source.lower() or "X-Forwarded-For" in source

    async def test_failed_logins_from_distinct_xff_share_same_bucket(self, admin_files):
        """Bug N5: 模拟两个不同 X-Forwarded-For 头, 因为底层 client.host 相同, 共享速率桶。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})

            # "用户 A" 用 6 次错误密码, 然后 "用户 B" 再试一次, 应该已被限流
            for _ in range(6):
                await client.post(
                    "/admin/auth/login",
                    json={"password": "wrong"},
                    headers={"X-Forwarded-For": "203.0.113.10"},
                )
            # 用户 B 完全无辜, 但 IP 桶按 TCP-level client.host 计数, 已耗尽
            for _ in range(5):
                await client.post(
                    "/admin/auth/login",
                    json={"password": "wrong"},
                    headers={"X-Forwarded-For": "198.51.100.20"},
                )
            victim = await client.post(
                "/admin/auth/login",
                json={"password": "pw"},
                headers={"X-Forwarded-For": "198.51.100.20"},
            )
        # 当前 bug: 即便密码正确, "用户 B" 也被限流
        assert victim.status_code == 429


# ─────────────────────────── N6 ───────────────────────────


class TestN6SessionCookieMissingSecureFlag:
    """N6: admin_session cookie 没有 Secure 标志。"""

    def test_build_session_cookie_omits_secure_flag(self):
        cookie = admin_auth.build_session_cookie("dummy-token")
        # 当前 bug: 缺少 Secure 标志
        assert "Secure" not in cookie
        # 仍然带 HttpOnly + SameSite=Lax 作为参考
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie

    def test_build_cleared_session_cookie_omits_secure_flag(self):
        cookie = admin_auth.build_cleared_session_cookie()
        assert "Secure" not in cookie

    async def test_login_set_cookie_header_lacks_secure_flag(self, admin_files):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})
            login = await client.post("/admin/auth/login", json={"password": "pw"})

        set_cookie = login.headers["set-cookie"]
        # 当前 bug: 即使部署在 HTTPS 反代后, Set-Cookie 也不会带 Secure
        assert "Secure" not in set_cookie


# ─────────────────────────── N9 ───────────────────────────


class TestN9LoginRateLimitInMemory:
    """N9: 登录速率限制状态保存在内存, 进程重启即清空。"""

    def test_rate_limit_state_lives_in_module_level_dict_not_disk(self):
        """Bug N9: state 是一个普通的模块级 dict, 没有任何持久化。"""
        source = inspect.getsource(admin)
        assert "_login_attempts: dict[" in source
        # 不存在任何针对速率限制状态的写盘逻辑
        assert "login_attempts.json" not in source
        assert "_login_attempts) " not in source or "json.dump" not in source

    async def test_clearing_in_memory_state_immediately_resets_limit(self, admin_files):
        """Bug N9: 直接清空 dict 就能重置攻击者的失败窗口, 等价于一次进程重启。"""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/admin/auth/setup", json={"password": "pw"})

            for _ in range(12):
                await client.post("/admin/auth/login", json={"password": "wrong"})

            # 命中限流
            limited = await client.post("/admin/auth/login", json={"password": "pw"})

            # 模拟 "进程重启": 清空内存 state
            admin._login_attempts.clear()

            after_restart = await client.post(
                "/admin/auth/login", json={"password": "pw"}
            )

        assert limited.status_code == 429
        # 当前 bug: 一次内存清空就让登录恢复, 攻击者借助 docker restart/OOM 可重置
        assert after_restart.status_code == 200
