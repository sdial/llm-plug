"""票据04：多接入点 fetch-models 与按接入点连通性测试。

锁定两个接缝：
- POST /admin/channels/{channel_id}/fetch-models —— 并发拉取全部启用接入点并合并去重
- POST /admin/channels/{channel_id}/test —— 按接入点返回独立成败/耗时/错误
"""

import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

import config
import storage
from main import app
from tests.admin_auth_utils import login_admin


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://upstream.invalid"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


def _make_fake_client(route):
    """构造类级 httpx.AsyncClient 替身：记录每次调用并按 route(url) 出牌。"""

    class FakeAsyncClient:
        calls: list[tuple] = []

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def aclose(self):
            return None

        async def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs.get("headers"), kwargs.get("json")))
            return route(url)

        async def post(self, url, **kwargs):
            self.calls.append(("POST", url, kwargs.get("headers"), kwargs.get("json")))
            return route(url)

    return FakeAsyncClient


def _models_payload(model_names):
    return {"data": [{"id": name} for name in model_names]}


@pytest.fixture
def endpoints_channels_file(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"

    channels_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "id": "ch_multi",
                        "name": "Multi",
                        "api_key": "sk-multi",
                        "models": ["gpt-4o", "claude-3-5"],
                        "socks5_proxy": None,
                        "endpoints": [
                            {
                                "api_type": "openai-chat-completions",
                                "base_url": "https://chat.example.com",
                            },
                            {
                                "api_type": "anthropic",
                                "base_url": "https://anth.example.com",
                            },
                        ],
                    },
                    {
                        "id": "ch_single",
                        "name": "Single",
                        "api_key": "sk-single",
                        "models": ["gpt-4o-mini"],
                        "socks5_proxy": None,
                        "endpoints": [
                            {
                                "api_type": "openai-chat-completions",
                                "base_url": "https://single.example.com",
                            }
                        ],
                    },
                    {
                        "id": "ch_all_disabled",
                        "name": "AllDisabled",
                        "api_key": "sk-off",
                        "models": ["m"],
                        "socks5_proxy": None,
                        "endpoints": [
                            {
                                "api_type": "openai-chat-completions",
                                "base_url": "https://off.example.com",
                                "enabled": False,
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()
    import middleware.whitelist_middleware as wmod
    import whitelist as _whitelist_mod

    monkeypatch.setattr(
        wmod,
        "_whitelist_cache",
        _whitelist_mod.WhitelistCache(str(data_dir / "whitelist.csv")),
    )
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None
    yield channels_path
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None


# ─── fetch-models（按渠道 id）─────────────────────────────────────────────


@pytest.mark.anyio
async def test_form_fetch_models_uses_saved_key_for_unchanged_endpoint(endpoints_channels_file, monkeypatch):
    """编辑态密钥框为空时，模型拉取仍应使用服务端保存的渠道密钥。"""

    FakeAsyncClient = _make_fake_client(lambda url: FakeResponse(payload=_models_payload(["gpt-4o"])))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        response = await client.post(
            "/admin/channels/fetch-models",
            json={
                "channel_id": "ch_multi",
                "base_url": "https://chat.example.com",
                "models_url": None,
                "api_key": None,
                "api_type": "openai-chat-completions",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"models": ["gpt-4o"]}
    assert FakeAsyncClient.calls[0][2]["Authorization"] == "Bearer sk-multi"


@pytest.mark.anyio
async def test_form_fetch_models_does_not_send_saved_key_to_changed_endpoint(endpoints_channels_file):
    """未保存的地址变更不能借用保存的 Key，避免向任意 URL 泄露渠道凭据。"""

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        response = await client.post(
            "/admin/channels/fetch-models",
            json={
                "channel_id": "ch_multi",
                "base_url": "https://changed.example.com",
                "models_url": None,
                "api_key": None,
                "api_type": "openai-chat-completions",
            },
        )

    assert response.status_code == 400
    assert "已保存的接入点地址不一致" in response.json()["detail"]


@pytest.mark.anyio
async def test_fetch_models_merges_and_dedups_across_endpoints(endpoints_channels_file, monkeypatch):
    def route(url):
        if "chat.example.com" in url:
            return FakeResponse(payload=_models_payload(["b-model", "a-model", "c-model"]))
        if "anth.example.com" in url:
            return FakeResponse(payload=_models_payload(["b-model", "d-model"]))
        return FakeResponse(status_code=404, payload={}, text="not found")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        # 先建好 ASGI 测试客户端再打桩，避免测试载体自身被替换
        FakeAsyncClient = _make_fake_client(route)
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        resp = await client.post("/admin/channels/ch_multi/fetch-models")

    assert resp.status_code == 200
    body = resp.json()
    # 独立真值：{a,b,c,d} 排序去重
    assert body["models"] == ["a-model", "b-model", "c-model", "d-model"]
    assert len(body["results"]) == 2
    by_type = {r["api_type"]: r for r in body["results"]}
    assert by_type["openai-chat-completions"]["success"] is True
    assert by_type["openai-chat-completions"]["count"] == 3
    assert by_type["anthropic"]["success"] is True
    assert by_type["anthropic"]["count"] == 2
    fetched_urls = [url for _, url, _, _ in FakeAsyncClient.calls]
    assert "https://chat.example.com/v1/models" in fetched_urls
    assert "https://anth.example.com/v1/models" in fetched_urls


@pytest.mark.anyio
async def test_fetch_models_partial_failure_keeps_healthy_results(endpoints_channels_file, monkeypatch):
    def route(url):
        if "chat.example.com" in url:
            return FakeResponse(payload=_models_payload(["gpt-4o"]))
        # anthropic 接入点坏掉：非 200
        return FakeResponse(status_code=503, payload={"oops": True}, text="down")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        # 先建好 ASGI 测试客户端再打桩，避免测试载体自身被替换
        FakeAsyncClient = _make_fake_client(route)
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        resp = await client.post("/admin/channels/ch_multi/fetch-models")

    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == ["gpt-4o"]
    by_type = {r["api_type"]: r for r in body["results"]}
    assert by_type["openai-chat-completions"]["success"] is True
    assert by_type["openai-chat-completions"]["count"] == 1
    assert by_type["anthropic"]["success"] is False
    assert "503" in by_type["anthropic"]["error"]


@pytest.mark.anyio
async def test_fetch_models_uses_per_endpoint_auth_headers(endpoints_channels_file, monkeypatch):
    def route(url):
        return FakeResponse(payload=_models_payload(["m1"]))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        # 先建好 ASGI 测试客户端再打桩，避免测试载体自身被替换
        FakeAsyncClient = _make_fake_client(route)
        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        await client.post("/admin/channels/ch_multi/fetch-models")

    headers_by_url = {url: headers for _, url, headers, _ in FakeAsyncClient.calls}
    assert headers_by_url["https://chat.example.com/v1/models"]["Authorization"] == "Bearer sk-multi"
    assert headers_by_url["https://anth.example.com/v1/models"]["x-api-key"] == "sk-multi"


@pytest.mark.anyio
async def test_fetch_models_unknown_channel_returns_404(endpoints_channels_file):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_missing/fetch-models")

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_fetch_models_no_enabled_endpoint_returns_400(endpoints_channels_file):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_all_disabled/fetch-models")

    assert resp.status_code == 400


# ─── 渠道连通性测试（按接入点）────────────────────────────────────────────


def _healthy_chat_dict():
    return {"choices": [{"message": {"content": "pong"}}]}


def _healthy_anthropic_dict():
    return {"content": [{"type": "text", "text": "bonjour"}]}


def _patch_execute_endpoint(monkeypatch, fake):
    """替换管理端探测所依赖的 Endpoint Execution。"""
    monkeypatch.setattr("routers.admin.channels.execute_endpoint", fake)


def _make_fake_execute_endpoint(route_by_url):
    """构造 Endpoint Execution 替身：记录每次调用并按 route(url) 出牌。

    route_by_url(url, request_data, target_api_type) → dict | Exception
    返回 dict 时作为上游响应体；返回 Exception 时抛给调用方模拟失败。
    """

    calls: list[tuple] = []

    async def fake(channel, endpoint, input, *, settings, wait_budget):
        calls.append((channel, endpoint, input))
        result = route_by_url(endpoint.base_url, input.payload, input.inbound_api_type)
        if isinstance(result, BaseException):
            raise result
        return result

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


@pytest.mark.anyio
async def test_default_tests_every_enabled_endpoint(endpoints_channels_file, monkeypatch):
    def route(url, request_data, target_api_type):
        if "chat.example.com" in url:
            return _healthy_chat_dict()
        if "anth.example.com" in url:
            return _healthy_anthropic_dict()
        raise AssertionError(f"unexpected url: {url}")

    fake_execute = _make_fake_execute_endpoint(route)

    # 同时打两个别名，确保无论实现走哪条路径都命中桩
    _patch_execute_endpoint(monkeypatch, fake_execute)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_multi/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["results"]) == 2
    by_type = {r["api_type"]: r for r in body["results"]}
    for entry in body["results"]:
        assert entry["success"] is True
        assert entry["message"] == "测试通过"
        assert isinstance(entry["latency_ms"], int)
        assert entry["model"] == "gpt-4o"  # 缺省取渠道模型列表首个
    assert by_type["openai-chat-completions"]["reply"] == "pong"
    assert by_type["anthropic"]["reply"] == "bonjour"

    # 走生产发送栈：校验 Endpoint Execution 调用而非裸 httpx
    assert len(fake_execute.calls) == 2
    by_url = {endpoint.base_url: (channel, input) for channel, endpoint, input in fake_execute.calls}
    assert "https://chat.example.com" in by_url
    assert "https://anth.example.com" in by_url
    for base_url in ("https://chat.example.com", "https://anth.example.com"):
        _, input = by_url[base_url]
        assert input.payload["max_tokens"] == 5
        assert input.request_source == "admin_test"
        assert input.payload["model"] == "gpt-4o"
    # 渠道级鉴权与 socks5 由 Endpoint Execution 内部经 create_client 承载，不在此层校验


@pytest.mark.anyio
async def test_api_type_query_tests_single_endpoint(endpoints_channels_file, monkeypatch):
    def route(url, request_data, target_api_type):
        if "anth.example.com" in url:
            return _healthy_anthropic_dict()
        raise AssertionError(f"不应探测其他接入点: {url}")

    fake_execute = _make_fake_execute_endpoint(route)
    _patch_execute_endpoint(monkeypatch, fake_execute)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_multi/test?api_type=anthropic")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["results"]) == 1
    assert body["results"][0]["api_type"] == "anthropic"
    assert len(fake_execute.calls) == 1
    # 校验目标过滤：仅 anthropic 接入点被调用
    called_base = fake_execute.calls[0][1].base_url
    assert "anth.example.com" in called_base


@pytest.mark.anyio
async def test_unknown_api_type_query_returns_404(endpoints_channels_file):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_multi/test?api_type=openai-response")

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_single_endpoint_channel_matches_legacy_shape(endpoints_channels_file, monkeypatch):
    def route(url, request_data, target_api_type):
        if "single.example.com" in url:
            return _healthy_chat_dict()
        raise AssertionError(f"unexpected url: {url}")

    fake_execute = _make_fake_execute_endpoint(route)
    _patch_execute_endpoint(monkeypatch, fake_execute)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_single/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["results"]) == 1
    entry = body["results"][0]
    assert {"api_type", "success", "message", "latency_ms", "model", "reply"} <= set(entry)
    assert entry["success"] is True
    assert entry["reply"] == "pong"
    assert len(fake_execute.calls) == 1
    assert fake_execute.calls[0][2].request_source == "admin_test"


@pytest.mark.anyio
async def test_channel_dispatches_to_endpoint_execution_seam(endpoints_channels_file, monkeypatch):
    """管理端探测直连 Endpoint Execution，正常返回与异常均不被中间适配层改写。"""

    async def run_probe():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await login_admin(client)
            return await client.post("/admin/channels/ch_single/test")

    # 成功分支：桩抛出的异常原样传播 / 正常结果原样返回
    ok_calls = []

    async def ok_execute(channel, endpoint, input, *, settings, wait_budget):
        ok_calls.append(input.request_source)
        return _healthy_chat_dict()

    monkeypatch.setattr("routers.admin.channels.execute_endpoint", ok_execute)
    resp = await run_probe()
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert ok_calls == ["admin_test"]

    # 异常分支：桩抛 ConnectError 必须向上传播（被 test_channel catch 成失败结果），
    # 而不是被包装吞掉后真打网络
    bad_calls = []

    async def bad_execute(channel, endpoint, input, *, settings, wait_budget):
        bad_calls.append(input.request_source)
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("routers.admin.channels.execute_endpoint", bad_execute)
    resp = await run_probe()
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["results"][0]["success"] is False
    assert "请求失败" in body["results"][0]["message"]
    assert bad_calls == ["admin_test"]


@pytest.mark.anyio
async def test_quota_blocked_shortcircuits_without_endpoint_execution(endpoints_channels_file, monkeypatch):
    """quota 窗口屏蔽中的渠道：短路返回全部失败结果（消息标明屏蔽原因），执行器不被调用（Ticket 04）。"""
    from proxy import outcomes

    outcomes.reset()
    try:
        outcomes.record(
            "gpt-4o",
            "ch_multi",
            outcomes.OutcomeKind.quota_window,
            reset_at=time.time() + 3600,
            code="quota-exceeded",
        )
        fake_execute = AsyncMock(return_value={"choices": [{"message": {"content": "ping"}}]})
        monkeypatch.setattr("routers.admin.channels.execute_endpoint", fake_execute)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await login_admin(client)
            resp = await client.post("/admin/channels/ch_multi/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert len(body["results"]) == 2
        for r in body["results"]:
            assert r["success"] is False
            assert "配额" in r["message"] or "屏蔽" in r["message"]
            assert r["latency_ms"] is None
        fake_execute.assert_not_awaited()
    finally:
        outcomes.reset()


@pytest.mark.anyio
async def test_failure_does_not_trigger_health_failure_accounting(endpoints_channels_file, monkeypatch):
    """回归核心：测试失败绝不触发失败记账 / LB 冷却降级（成功行 success 记账为既定决策放行）。"""
    from proxy import outcomes

    outcomes.reset()
    try:

        async def fake_execute(channel, endpoint, input, *, settings, wait_budget):
            raise httpx.ConnectError("connection refused")

        calls = []

        def spy_record(*a, **kw):
            calls.append((a, kw))

        # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：失败记账零触碰
        monkeypatch.setattr(outcomes, "record", spy_record)
        # 发不出发送栈都走同样的 test_channel 编排（失败实例由顶层显式桩注入）
        monkeypatch.setattr("routers.admin.channels.execute_endpoint", fake_execute)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await login_admin(client)
            resp = await client.post("/admin/channels/ch_multi/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        for r in body["results"]:
            assert r["success"] is False
            assert "请求失败" in r["message"]
        # 失败记账零触碰：outcomes.record 从未被调用
        assert calls == []
        # 健康度状态机干净：不熔断不降级（失败事件未入环）
        assert outcomes.is_healthy("gpt-4o", "ch_multi") is True
        assert outcomes.is_degraded("gpt-4o", "ch_multi") is False
    finally:
        outcomes.reset()


@pytest.mark.anyio
async def test_each_endpoint_logs_admin_test_row_with_real_stack(endpoints_channels_file, monkeypatch, tmp_path):
    """真实发送栈：每个接入点成败均自动落库 request_source='admin_test'，test_channel 不自补记（Ticket 04）。"""
    import client as client_mod
    import proxy.endpoint_execution as nse
    import request_logs
    from proxy import outcomes

    outcomes.reset()
    await request_logs.close_backend()
    try:
        result = await request_logs.init_backend({"request_log_sqlite_path": str(tmp_path / "logs.db")})
        assert result["available"] is True

        class UpstreamResp:
            is_error = False

            def __init__(self, status_code, payload, headers=None):
                self.status_code = status_code
                self._payload = payload
                self.is_error = status_code >= 400
                self.headers = headers or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {self.status_code}",
                        request=httpx.Request("POST", "https://up.invalid"),
                        response=httpx.Response(self.status_code),
                    )

            def json(self):
                return self._payload

            @property
            def text(self):
                return json.dumps(self._payload)

        async def fake_create(channel, *, endpoint=None):
            class FakeClient:
                async def post(self, url, json=None, headers=None):
                    if "chat.example.com" in url:
                        return UpstreamResp(200, {"choices": [{"message": {"content": "pong"}}]}, {"x-rid": "1"})
                    return UpstreamResp(500, {"error": "boom"})

                async def aclose(self):
                    pass

                @property
                def is_closed(self):
                    return False

            return FakeClient()

        monkeypatch.setattr(client_mod, "create_client", fake_create)
        monkeypatch.setattr(nse, "create_client", fake_create)

        async def noop_budget(*a, **kw):
            pass

        monkeypatch.setattr(nse, "acquire_send_budget", noop_budget)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await login_admin(client)
            resp = await client.post("/admin/channels/ch_multi/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert len(body["results"]) == 2

        await request_logs.drain_queue()
        listed = await request_logs.list_requests(request_source="admin_test")
        rows = {r["api_type"]: r for r in listed["items"]}
        assert set(rows) == {"openai-chat-completions", "anthropic"}
        assert rows["openai-chat-completions"]["success"] is True
        assert rows["anthropic"]["success"] is False
        assert rows["anthropic"]["error_msg"]
        assert all(r["request_source"] == "admin_test" for r in listed["items"])
        # 成功行由发送栈自然 success 记账放行；失败零污染健康度
        assert outcomes.is_degraded("gpt-4o", "ch_multi") is False
    finally:
        outcomes.reset()
        await request_logs.close_backend()


@pytest.mark.anyio
async def test_broken_endpoint_isolated_from_healthy_one(endpoints_channels_file, monkeypatch):
    def route(url, request_data, target_api_type):
        if "chat.example.com" in url:
            return _healthy_chat_dict()
        # 模拟连接失败：抛 ConnectError 进失败分支
        return httpx.ConnectError("connection refused")

    fake_execute = _make_fake_execute_endpoint(route)
    _patch_execute_endpoint(monkeypatch, fake_execute)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_multi/test")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    by_type = {r["api_type"]: r for r in body["results"]}
    assert by_type["openai-chat-completions"]["success"] is True
    assert by_type["openai-chat-completions"]["reply"] == "pong"
    assert by_type["anthropic"]["success"] is False
    assert "请求失败" in by_type["anthropic"]["message"]
    assert isinstance(by_type["anthropic"]["latency_ms"], int)
    assert len(fake_execute.calls) == 2


@pytest.mark.anyio
async def test_model_outside_channel_fails_all_results(endpoints_channels_file):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        resp = await client.post("/admin/channels/ch_multi/test?model=nope")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert len(body["results"]) == 2
    for entry in body["results"]:
        assert entry["success"] is False
        assert "不在此渠道的模型列表中" in entry["message"]
