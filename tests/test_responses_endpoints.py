import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from models.api_types import APIType
from models.channel import Channel


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
        api_type=APIType.OPENAI_RESPONSE,
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        models=["gpt-4o"],
    )


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
        return _responses_channel()

    async def fake_create_client(channel):
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

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(200, json={"id": "resp_123", "deleted": True})
    )
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

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(200, json={"object": "list", "data": []})
    )
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
    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(200, json={"input_tokens": 12})
    )

    async def fake_select(*, model=None, **kwargs):
        selected["model"] = model
        return _responses_channel()

    async def fake_create_client(channel):
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

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(200, json={"object": "response.compacted"})
    )
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
async def test_openai_response_passthrough_post_does_not_apply_capability_filter(monkeypatch):
    from proxy_core import _do_request

    captured = {}
    channel = Channel(
        id="ch_resp",
        name="Responses",
        api_type=APIType.OPENAI_RESPONSE,
        base_url="https://api.deepseek.example/v1",
        api_key="sk-test",
        models=["gpt-4o"],
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

    async def fake_create_client(ch):
        return dummy

    async def fake_record_success(channel_id):
        return None

    monkeypatch.setattr("proxy_core.create_client", fake_create_client)
    monkeypatch.setattr("proxy_core._record_request", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr("proxy_core.load_balancer.record_success", fake_record_success)

    request_body = {
        "model": "gpt-4o",
        "input": "hello",
        "parallel_tool_calls": True,
    }
    result = await _do_request(channel, request_body, APIType.OPENAI_RESPONSE, False)

    assert result["id"] == "resp_1"
    assert dummy.posts[0][1]["json"] == request_body
    assert captured["request_body"] == request_body

@pytest.mark.asyncio
async def test_openai_response_stream_passthrough_preserves_raw_sse_blocks(monkeypatch):
    from proxy_core import _do_request

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
            yield 'data: {"type":"response.completed","response":{"id":"resp_1","object":"response","status":"completed","model":"gpt-4o","output":[],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}'
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

    async def fake_record_success(channel_id):
        return None

    channel = Channel(
        id="ch_resp_stream",
        name="Responses",
        api_type=APIType.OPENAI_RESPONSE,
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        models=["gpt-4o"],
    )
    monkeypatch.setattr("proxy_core.create_stream_client", lambda channel: FakeClient())
    monkeypatch.setattr("proxy_core._record_request", lambda **kwargs: None)
    monkeypatch.setattr("proxy_core.load_balancer.record_success", fake_record_success)

    stream = await _do_request(
        channel,
        {"model": "gpt-4o", "input": "hello", "stream": True},
        APIType.OPENAI_RESPONSE,
        True,
    )
    output = "".join([chunk async for chunk in stream])

    assert "id: evt_1\nretry: 5000\nevent: response.output_text.delta\n" in output
    assert 'data: {"type":"response.output_text.delta","delta":"hi"}\n\n' in output

