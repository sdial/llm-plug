import httpx
import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy.channel_attempt import ChannelAttemptInput, NonStreamAttemptResult, attempt_channel
from proxy.endpoint_execution import EndpointExecutionInput, execute_endpoint


def _multi_endpoint_channel() -> Channel:
    return Channel(
        id="ch_multi",
        name="Multi",
        api_key="key",
        models=["model-a"],
        endpoints=[
            Endpoint(api_type=APIType.ANTHROPIC, base_url="https://anthropic.example"),
            Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://chat.example"),
        ],
    )


@pytest.mark.asyncio
async def test_endpoint_execution_isolates_nested_caller_payload(monkeypatch):
    channel = _multi_endpoint_channel()
    endpoint = channel.endpoints[1]
    payload = {"model": "model-a", "messages": [{"role": "user", "content": "original"}]}

    async def mutate_working_copy(request_data, source_type, target_api_type):
        request_data["messages"][0]["content"] = "mutated"
        raise RuntimeError("stop after observing working copy")

    monkeypatch.setattr(
        "proxy.conversion._prepare_openai_response_request_for_upstream",
        mutate_working_copy,
    )

    with pytest.raises(RuntimeError, match="working copy"):
        await execute_endpoint(
            channel,
            endpoint,
            EndpointExecutionInput(
                payload=payload,
                inbound_api_type=APIType.OPENAI_CHAT,
                requested_model="model-a",
                serving_model="model-a",
                is_stream=False,
            ),
            settings={},
            wait_budget=0,
        )

    assert payload["messages"][0]["content"] == "original"


@pytest.mark.asyncio
async def test_channel_attempt_uses_one_settings_snapshot_and_returns_actual_endpoint(monkeypatch):
    channel = _multi_endpoint_channel()
    settings_source = {"feature": {"enabled": True}}
    settings_seen = []
    endpoints_seen = []
    request = httpx.Request("POST", "https://anthropic.example")
    response = httpx.Response(500, request=request)

    def get_settings():
        return settings_source

    async def fake_execute(channel_arg, endpoint, input_arg, *, settings, wait_budget):
        assert channel_arg is channel
        assert input_arg.payload is payload
        endpoints_seen.append(endpoint)
        settings_seen.append(settings)
        if len(endpoints_seen) == 1:
            raise httpx.HTTPStatusError("first endpoint failed", request=request, response=response)
        return {"ok": True}

    payload = {"model": "model-a", "messages": [{"role": "user", "content": "hi"}]}
    monkeypatch.setattr("proxy.channel_attempt.config.get_settings", get_settings)
    monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", fake_execute)
    monkeypatch.setattr("proxy.channel_attempt.outcomes.record", lambda *args, **kwargs: None)

    result = await attempt_channel(
        channel,
        ChannelAttemptInput(
            payload=payload,
            inbound_api_type=APIType.OPENAI_RESPONSE,
            requested_model="requested-group",
            serving_model="model-a",
            is_stream=False,
        ),
        wait_budget=3.0,
    )

    assert isinstance(result, NonStreamAttemptResult)
    assert result.channel is channel
    assert result.endpoint is endpoints_seen[1]
    assert result.response == {"ok": True}
    assert settings_seen[0] is settings_seen[1]
    assert settings_seen[0] == settings_source
    assert settings_seen[0] is not settings_source
