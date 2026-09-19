import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from models.api_types import APIType
from models.channel import Channel, Endpoint


class DummyAsyncClient:
    def __init__(self, response):
        self.response = response

    async def request(self, method, url, **kwargs):
        return self.response


def _responses_channel():
    return Channel(
        id="ch_resp",
        name="Responses",
        endpoints=[Endpoint(api_type=APIType.OPENAI_RESPONSE, base_url="https://api.openai.com/v1")],
        api_key="sk-test",
        models=["gpt-4o"],
    )


def _patch_record(monkeypatch, proxy_response):
    """记账锚点迁到真实住所 outcomes（ADR-0025 D3）：按 (model, channel_id, kind) 捕获。"""
    recorded_success = []
    recorded_failure = []

    def fake_record(model, channel_id, kind, *args, **kwargs):
        if kind is proxy_response.OutcomeKind.success:
            recorded_success.append((model, channel_id))
        else:
            recorded_failure.append((model, channel_id))

    monkeypatch.setattr(proxy_response.outcomes, "record", fake_record)
    return recorded_success, recorded_failure


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
        channel = _responses_channel()
        # _select_responses_channel 契约：返回 (channel, 原生 Responses 接入点)
        return channel, channel.enabled_endpoint_for(APIType.OPENAI_RESPONSE)

    async def fake_create_client(channel):
        return dummy

    monkeypatch.setattr(proxy_response, "_select_responses_channel", fake_select)
    monkeypatch.setattr(proxy_response, "create_client", fake_create_client)


@pytest.mark.asyncio
async def test_upstream_200_records_success(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(proxy_response.httpx.Response(200, json={"id": "resp_1", "status": "completed"}))
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    recorded_success, recorded_failure = _patch_record(monkeypatch, proxy_response)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 200
    assert recorded_success == [("ch_resp", "ch_resp")]
    assert recorded_failure == []


@pytest.mark.asyncio
async def test_upstream_429_records_failure(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(
            429,
            json={"error": {"message": "Rate limited", "type": "rate_limit_error"}},
        )
    )
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    # 预算置零：无 Retry-After 的 429 会按 1s 步长重试至预算耗尽（默认 30s），
    # 测试只关心“最终记失败”，不需要真实等待
    def fake_get_setting(key, default=None):
        return 0 if key == "rate_limit_wait_seconds" else default

    monkeypatch.setattr(proxy_response.config, "get_setting", fake_get_setting)

    recorded_success, recorded_failure = _patch_record(monkeypatch, proxy_response)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 429
    assert recorded_success == []
    assert recorded_failure == [("ch_resp", "ch_resp")]


@pytest.mark.asyncio
async def test_upstream_500_records_failure(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(
            500,
            json={"error": {"message": "Internal server error", "type": "api_error"}},
        )
    )
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    recorded_success, recorded_failure = _patch_record(monkeypatch, proxy_response)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 500
    assert recorded_success == []
    assert recorded_failure == [("ch_resp", "ch_resp")]


@pytest.mark.asyncio
async def test_upstream_502_records_failure(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(proxy_response.httpx.Response(502, text="Bad Gateway"))
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    recorded_success, recorded_failure = _patch_record(monkeypatch, proxy_response)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 502
    assert recorded_success == []
    assert recorded_failure == [("ch_resp", "ch_resp")]


@pytest.mark.asyncio
async def test_upstream_400_records_success(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(
            400,
            json={"error": {"message": "Bad request", "type": "invalid_request_error"}},
        )
    )
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    recorded_success, recorded_failure = _patch_record(monkeypatch, proxy_response)

    async with AsyncClient(transport=ASGITransport(app=responses_app), base_url="http://test") as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 400
    assert recorded_success == [("ch_resp", "ch_resp")]
    assert recorded_failure == []
