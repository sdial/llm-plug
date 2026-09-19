from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy.outcomes import OutcomeKind
from rate_limiter import rate_limiter


class DummyAsyncClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.response._request is None:
            self.response.request = httpx.Request(method, url)
        return self.response


class DummyPostClient:
    def __init__(self, response):
        self.response = response
        self.posts = []

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.response._request is None:
            self.response.request = httpx.Request("POST", url)
        return self.response


def _responses_channel():
    return Channel(
        id="ch_resp",
        name="Responses",
        api_key="sk-test",
        models=["gpt-4o"],
        endpoints=[Endpoint(api_type=APIType.OPENAI_RESPONSE, base_url="https://api.openai.com/v1")],
    )


def _responses_channel_with_endpoint():
    """_select_responses_channel 新契约：返回 (channel, 原生 Responses 接入点)"""
    channel = _responses_channel()
    return channel, channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)


@pytest.fixture
def responses_app(monkeypatch):
    from routers import proxy_response
    from routers.proxy_response import router

    monkeypatch.setattr(
        proxy_response,
        "check_proxy_authorization",
        lambda authorization, request_state=None: True,
    )
    app = FastAPI()
    app.include_router(router)
    return app


def _patch_forwarding(monkeypatch, proxy_response, dummy):
    async def fake_select(**kwargs):
        return _responses_channel_with_endpoint()

    async def fake_create_client(channel, *, endpoint=None):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)


def test_get_response_endpoint_exists():
    from routers.proxy_response import router

    get_routes = [r for r in router.routes if "GET" in getattr(r, "methods", set())]
    assert get_routes
    assert any("response_id" in str(r.path) for r in get_routes)


def test_delete_response_endpoint_exists():
    from routers.proxy_response import router

    delete_routes = [r for r in router.routes if "DELETE" in getattr(r, "methods", set())]
    assert delete_routes
    assert any("response_id" in str(r.path) for r in delete_routes)


@pytest.mark.asyncio
async def test_get_response_is_forwarded_to_upstream(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(
            200,
            json={"id": "resp_123", "object": "response", "status": "completed"},
        )
    )
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_123?include[]=output")

    assert resp.status_code == 200
    assert resp.json()["id"] == "resp_123"
    assert dummy.calls[0][0] == "GET"
    assert dummy.calls[0][1] == "https://api.openai.com/v1/responses/resp_123?include%5B%5D=output"


@pytest.mark.asyncio
async def test_delete_response_is_forwarded_to_upstream(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(proxy_response.httpx.Response(200, json={"id": "resp_123", "deleted": True}))
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.delete("/v1/responses/resp_123")

    assert resp.status_code == 200
    assert resp.json() == {"id": "resp_123", "deleted": True}
    assert dummy.calls[0][0] == "DELETE"
    assert dummy.calls[0][1] == "https://api.openai.com/v1/responses/resp_123"


@pytest.mark.asyncio
async def test_get_response_forwards_upstream_404(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(
            404,
            json={"error": {"message": "No response", "type": "invalid_request_error"}},
        )
    )
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_missing")

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "No response"


@pytest.mark.asyncio
async def test_cancel_response_is_forwarded_to_upstream(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(
            200,
            json={"id": "resp_123", "object": "response", "status": "cancelled"},
        )
    )
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.post("/v1/responses/resp_123/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert dummy.calls[0][0] == "POST"
    assert dummy.calls[0][1] == "https://api.openai.com/v1/responses/resp_123/cancel"


@pytest.mark.asyncio
async def test_list_response_input_items_is_forwarded_to_upstream(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(proxy_response.httpx.Response(200, json={"object": "list", "data": []}))
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_123/input_items?limit=1")

    assert resp.status_code == 200
    assert resp.json() == {"object": "list", "data": []}
    assert dummy.calls[0][0] == "GET"
    assert dummy.calls[0][1] == "https://api.openai.com/v1/responses/resp_123/input_items?limit=1"


@pytest.mark.asyncio
async def test_count_response_input_tokens_uses_model_channel(monkeypatch, responses_app):
    from routers import proxy_response

    selected = {}
    dummy = DummyAsyncClient(proxy_response.httpx.Response(200, json={"input_tokens": 12}))

    async def fake_select(*, model=None, **kwargs):
        selected["model"] = model
        return _responses_channel_with_endpoint()

    async def fake_create_client(channel, *, endpoint=None):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/responses/input_tokens",
            json={"model": "gpt-4o", "input": "hello"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"input_tokens": 12}
    assert selected["model"] == "gpt-4o"
    assert dummy.calls[0][0] == "POST"
    assert dummy.calls[0][1] == "https://api.openai.com/v1/responses/input_tokens"
    assert dummy.calls[0][2]["json"] == {"model": "gpt-4o", "input": "hello"}


@pytest.mark.asyncio
async def test_compact_response_is_forwarded_to_upstream(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(proxy_response.httpx.Response(200, json={"object": "response.compacted"}))
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/responses/compact",
            json={"model": "gpt-4o", "input": "long context"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"object": "response.compacted"}
    assert dummy.calls[0][0] == "POST"
    assert dummy.calls[0][1] == "https://api.openai.com/v1/responses/compact"


@pytest.mark.asyncio
async def test_openai_response_passthrough_post_does_not_apply_capability_filter(
    monkeypatch,
):
    from tests.proxy.endpoint_execution_test_utils import execute_single_endpoint

    captured = {}
    channel = Channel(
        id="ch_resp",
        name="Responses",
        api_key="sk-test",
        models=["gpt-4o"],
        endpoints=[Endpoint(api_type=APIType.OPENAI_RESPONSE, base_url="https://api.deepseek.example/v1")],
    )
    dummy = DummyPostClient(
        httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": "gpt-4o",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        )
    )

    async def fake_create_client(ch, *, endpoint=None):
        return dummy

    monkeypatch.setattr("proxy.endpoint_execution.create_client", fake_create_client)
    monkeypatch.setattr("proxy.endpoint_execution._record_request", lambda **kwargs: captured.update(kwargs))
    # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：静默 success 记账
    monkeypatch.setattr("proxy.outcomes.record", lambda *args, **kwargs: None)

    request_body = {
        "model": "gpt-4o",
        "input": "hello",
        "parallel_tool_calls": True,
    }
    result = await execute_single_endpoint(channel, request_body, APIType.OPENAI_RESPONSE, False)

    assert result["id"] == "resp_1"
    assert dummy.posts[0][1]["json"] == request_body
    assert captured["request_body"] == request_body


@pytest.mark.asyncio
async def test_openai_response_stream_passthrough_preserves_raw_sse_blocks(monkeypatch):
    from tests.proxy.endpoint_execution_test_utils import execute_single_endpoint

    class FakeStreamResponse:
        status_code = 200
        is_error = False
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield "id: evt_1"
            yield "retry: 5000"
            yield "event: response.output_text.delta"
            yield 'data: {"type":"response.output_text.delta","delta":"hi"}'
            yield ""
            yield "event: response.completed"
            yield (
                'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
                '"status":"completed","model":"gpt-4o","output":[],'
                '"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}'
            )
            yield ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def stream(self, *args, **kwargs):
            return FakeStreamResponse()

        async def aclose(self):
            return None

    channel = Channel(
        id="ch_resp_stream",
        name="Responses",
        api_key="sk-test",
        models=["gpt-4o"],
        endpoints=[Endpoint(api_type=APIType.OPENAI_RESPONSE, base_url="https://api.openai.com/v1")],
    )
    monkeypatch.setattr("proxy.stream_executor.create_stream_client", lambda channel: FakeClient())
    monkeypatch.setattr("proxy.stream_executor._record_request", lambda **kwargs: None)
    # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：静默 success 记账
    monkeypatch.setattr("proxy.outcomes.record", lambda *args, **kwargs: None)

    stream = await execute_single_endpoint(
        channel,
        {"model": "gpt-4o", "input": "hello", "stream": True},
        APIType.OPENAI_RESPONSE,
        True,
    )
    output = "".join([chunk async for chunk in stream])

    assert "id: evt_1\nretry: 5000\nevent: response.output_text.delta\n" in output
    assert 'data: {"type":"response.output_text.delta","delta":"hi"}\n\n' in output


# ─── 限速适配 ───


@pytest.fixture
def responses_rate_limit_app(monkeypatch):
    from routers import proxy_response
    from routers.proxy_response import router

    monkeypatch.setattr(
        proxy_response,
        "check_proxy_authorization",
        lambda authorization, request_state=None: True,
    )
    app = FastAPI()
    app.include_router(router)
    yield app
    rate_limiter._windows.clear()


def _responses_channel_with_rpm(rpm: int | None = None):
    return Channel(
        id="ch_resp_rl",
        name="Responses RL",
        api_key="sk-test",
        models=["gpt-4o"],
        rate_limit_rpm=rpm,
        endpoints=[Endpoint(api_type=APIType.OPENAI_RESPONSE, base_url="https://api.openai.com/v1")],
    )


class CountingDummyClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.index = 0

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        resp = self.responses[self.index]
        if self.index + 1 < len(self.responses):
            self.index += 1
        if resp._request is None:
            resp.request = httpx.Request(method, url)
        return resp


@pytest.mark.asyncio
async def test_auxiliary_endpoint_acquires_rate_limit(monkeypatch, responses_rate_limit_app):
    from routers import proxy_response

    rate_limiter._windows.clear()
    channel = _responses_channel_with_rpm(rpm=1)
    # 预占唯一额度
    await rate_limiter.acquire(channel.id, 1, wait_timeout=0)

    dummy = CountingDummyClient([httpx.Response(200, json={"id": "resp_123", "object": "response"})])

    async def fake_select(**kwargs):
        return channel, channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)

    async def fake_create_client(ch, *, endpoint=None):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)

    def fake_get_setting(key, default=None):
        return 0 if key == "rate_limit_wait_seconds" else default

    monkeypatch.setattr(proxy_response.config, "get_setting", fake_get_setting)
    record = MagicMock()
    monkeypatch.setattr(proxy_response.outcomes, "record", record)

    async with AsyncClient(transport=ASGITransport(app=responses_rate_limit_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_123")

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"
    # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：键不变（GET 透传 model=None → 渠道兜底虚拟键）
    assert [c.args for c in record.call_args_list] == [("ch_resp_rl", "ch_resp_rl", OutcomeKind.rate_limit_exhausted)]
    assert len(dummy.calls) == 0


@pytest.mark.asyncio
async def test_auxiliary_endpoint_retries_429_with_retry_after(monkeypatch, responses_rate_limit_app):
    from routers import proxy_response

    rate_limiter._windows.clear()
    channel = _responses_channel_with_rpm(rpm=1000)
    dummy = CountingDummyClient(
        [
            httpx.Response(
                429,
                headers={"retry-after": "0.01"},
                json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
            ),
            httpx.Response(200, json={"id": "resp_123", "object": "response"}),
        ]
    )

    async def fake_select(**kwargs):
        return channel, channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)

    async def fake_create_client(ch, *, endpoint=None):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)

    def fake_get_setting(key, default=None):
        return 30 if key == "rate_limit_wait_seconds" else default

    monkeypatch.setattr(proxy_response.config, "get_setting", fake_get_setting)
    record = MagicMock()
    monkeypatch.setattr(proxy_response.outcomes, "record", record)

    async with AsyncClient(transport=ASGITransport(app=responses_rate_limit_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_123")

    assert resp.status_code == 200
    assert resp.json()["id"] == "resp_123"
    assert len(dummy.calls) == 2
    assert [c.args for c in record.call_args_list] == [("ch_resp_rl", "ch_resp_rl", OutcomeKind.success)]


@pytest.mark.asyncio
async def test_auxiliary_endpoint_retry_after_zero_applies_floor(monkeypatch, responses_rate_limit_app):
    """上游持续 429 + Retry-After: 0 必须取下限 0.1s 并扣预算，
    预算耗尽后透传 429；否则预算恒不减少、同渠道无限重试活锁。"""
    from routers import proxy_response

    rate_limiter._windows.clear()
    channel = _responses_channel_with_rpm(rpm=1000)
    # 预算 0.3s / 下限 0.1s → 重试两三次后预算耗尽，429 原样透传；
    # 若下限缺失（Retry-After: 0 不扣预算）则会无限重试
    dummy = CountingDummyClient(
        [
            httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
            )
        ]
    )

    async def fake_select(**kwargs):
        return channel, channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)

    async def fake_create_client(ch, *, endpoint=None):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)

    def fake_get_setting(key, default=None):
        return 0.3 if key == "rate_limit_wait_seconds" else default

    monkeypatch.setattr(proxy_response.config, "get_setting", fake_get_setting)
    record = MagicMock()
    monkeypatch.setattr(proxy_response.outcomes, "record", record)

    async with AsyncClient(transport=ASGITransport(app=responses_rate_limit_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_123")

    assert resp.status_code == 429
    assert resp.json()["error"]["message"] == "rate limited"
    assert len(dummy.calls) <= 5
    # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：键不变（GET 透传 model=None → 渠道兜底虚拟键）
    assert [c.args for c in record.call_args_list] == [("ch_resp_rl", "ch_resp_rl", OutcomeKind.rate_limit_exhausted)]


@pytest.mark.asyncio
async def test_auxiliary_endpoint_returns_429_when_budget_exhausted(monkeypatch, responses_rate_limit_app):
    from routers import proxy_response

    rate_limiter._windows.clear()
    channel = _responses_channel_with_rpm(rpm=1000)
    dummy = CountingDummyClient(
        [
            httpx.Response(
                429,
                headers={"retry-after": "5"},
                json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
            ),
        ]
    )

    async def fake_select(**kwargs):
        return channel, channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)

    async def fake_create_client(ch, *, endpoint=None):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)

    def fake_get_setting(key, default=None):
        return 0 if key == "rate_limit_wait_seconds" else default

    monkeypatch.setattr(proxy_response.config, "get_setting", fake_get_setting)
    record = MagicMock()
    monkeypatch.setattr(proxy_response.outcomes, "record", record)

    async with AsyncClient(transport=ASGITransport(app=responses_rate_limit_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_123")

    assert resp.status_code == 429
    assert resp.json()["error"]["message"] == "rate limited"
    # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：键不变（GET 透传 model=None → 渠道兜底虚拟键）
    assert [c.args for c in record.call_args_list] == [("ch_resp_rl", "ch_resp_rl", OutcomeKind.rate_limit_exhausted)]
    assert len(dummy.calls) == 1


@pytest.mark.asyncio
async def test_auxiliary_endpoint_window_quota_429_fails_fast_and_blocks(monkeypatch, responses_rate_limit_app, tmp_path):
    import config as _config

    monkeypatch.setattr(_config, "DATA_DIR", str(tmp_path))
    import quota_limits
    from proxy import outcomes

    outcomes.reset()
    quota_limits.load()
    from routers import proxy_response

    rate_limiter._windows.clear()
    channel = _responses_channel_with_rpm(rpm=1000)
    # 注意：reset 时间必须用未来的绝对时间戳（格式与方舟一致），
    # 否则 is_blocked 会把过期条目直接判为 False
    import time as _time

    future = _time.strftime("%Y-%m-%d %H:%M:%S %z", _time.localtime(_time.time() + 3600))
    dummy = CountingDummyClient(
        [
            httpx.Response(
                429,
                json={
                    "error": {
                        "code": "AccountQuotaExceeded",
                        "message": f"It will reset at {future}.",
                    }
                },
            ),
        ]
    )

    async def fake_select(**kwargs):
        return channel, channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)

    async def fake_create_client(ch, *, endpoint=None):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)
    # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：委托真实 record（is_blocked 依赖
    # quota_window 写穿），只按 kind 侦察——窗口级限速不记成功/失败
    record_kinds = []
    real_record = outcomes.record

    def spy_record(model, channel_id, kind, *args, **kwargs):
        record_kinds.append(kind)
        return real_record(model, channel_id, kind, *args, **kwargs)

    monkeypatch.setattr(outcomes, "record", spy_record)

    async with AsyncClient(transport=ASGITransport(app=responses_rate_limit_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_123")

    # 429 + 原始 body 原样透传，只打一次上游，渠道被硬限制（outcomes 内存视图），不记成功/失败
    assert resp.status_code == 429
    assert "AccountQuotaExceeded" in resp.text
    assert outcomes.is_blocked("ch_resp_rl") is True
    assert len(dummy.calls) == 1
    assert record_kinds == [OutcomeKind.quota_window]
