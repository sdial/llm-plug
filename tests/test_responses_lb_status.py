import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from models.api_types import APIType
from models.channel import Channel


class DummyAsyncClient:
    def __init__(self, response):
        self.response = response

    async def request(self, method, url, **kwargs):
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


@pytest.mark.asyncio
async def test_upstream_200_records_success(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(
        proxy_response.httpx.Response(200, json={"id": "resp_1", "status": "completed"})
    )
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    recorded_success = []
    recorded_failure = []

    async def fake_record_success(channel_id):
        recorded_success.append(channel_id)

    async def fake_record_failure(channel_id):
        recorded_failure.append(channel_id)

    monkeypatch.setattr(
        proxy_response.load_balancer, "record_success", fake_record_success
    )
    monkeypatch.setattr(
        proxy_response.load_balancer, "record_failure", fake_record_failure
    )

    async with AsyncClient(
        transport=ASGITransport(app=responses_app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 200
    assert recorded_success == ["ch_resp"]
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

    recorded_success = []
    recorded_failure = []

    async def fake_record_success(channel_id):
        recorded_success.append(channel_id)

    async def fake_record_failure(channel_id):
        recorded_failure.append(channel_id)

    monkeypatch.setattr(
        proxy_response.load_balancer, "record_success", fake_record_success
    )
    monkeypatch.setattr(
        proxy_response.load_balancer, "record_failure", fake_record_failure
    )

    async with AsyncClient(
        transport=ASGITransport(app=responses_app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 429
    assert recorded_success == []
    assert recorded_failure == ["ch_resp"]


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

    recorded_success = []
    recorded_failure = []

    async def fake_record_success(channel_id):
        recorded_success.append(channel_id)

    async def fake_record_failure(channel_id):
        recorded_failure.append(channel_id)

    monkeypatch.setattr(
        proxy_response.load_balancer, "record_success", fake_record_success
    )
    monkeypatch.setattr(
        proxy_response.load_balancer, "record_failure", fake_record_failure
    )

    async with AsyncClient(
        transport=ASGITransport(app=responses_app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 500
    assert recorded_success == []
    assert recorded_failure == ["ch_resp"]


@pytest.mark.asyncio
async def test_upstream_502_records_failure(monkeypatch, responses_app):
    from routers import proxy_response

    dummy = DummyAsyncClient(proxy_response.httpx.Response(502, text="Bad Gateway"))
    _patch_forwarding(monkeypatch, proxy_response, dummy)

    recorded_success = []
    recorded_failure = []

    async def fake_record_success(channel_id):
        recorded_success.append(channel_id)

    async def fake_record_failure(channel_id):
        recorded_failure.append(channel_id)

    monkeypatch.setattr(
        proxy_response.load_balancer, "record_success", fake_record_success
    )
    monkeypatch.setattr(
        proxy_response.load_balancer, "record_failure", fake_record_failure
    )

    async with AsyncClient(
        transport=ASGITransport(app=responses_app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 502
    assert recorded_success == []
    assert recorded_failure == ["ch_resp"]


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

    recorded_success = []
    recorded_failure = []

    async def fake_record_success(channel_id):
        recorded_success.append(channel_id)

    async def fake_record_failure(channel_id):
        recorded_failure.append(channel_id)

    monkeypatch.setattr(
        proxy_response.load_balancer, "record_success", fake_record_success
    )
    monkeypatch.setattr(
        proxy_response.load_balancer, "record_failure", fake_record_failure
    )

    async with AsyncClient(
        transport=ASGITransport(app=responses_app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/responses/resp_1")

    assert resp.status_code == 400
    assert recorded_success == ["ch_resp"]
    assert recorded_failure == []
