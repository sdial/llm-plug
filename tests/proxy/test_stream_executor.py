"""Context Shaping 流式请求链测试。"""

from unittest.mock import patch

import pytest

import config
from models.api_types import APIType
from models.channel import Channel, Endpoint
from tests.proxy.endpoint_execution_test_utils import execute_single_endpoint

CTX_ON = {
    "context_shaping_strip_ansi": True,
}


class FakeStreamResponse:
    status_code = 200
    is_error = False
    headers = {"content-type": "text/event-stream"}

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        yield 'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"}}]}'
        yield ""
        yield "data: [DONE]"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeClient:
    def __init__(self, captured: dict):
        self._captured = captured

    def stream(self, method, url, *, json=None, headers=None):
        self._captured["json"] = json
        return FakeStreamResponse()

    async def aclose(self):
        return None


def _chat_channel():
    return Channel(
        id="ch_1",
        name="T",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.example.com")],
        api_key="sk-test",
        models=["gpt-4o"],
    )


@pytest.mark.anyio
async def test_stream_path_records_context_shaping_stats(monkeypatch):
    monkeypatch.setattr(config, "_settings", dict(CTX_ON))
    request_data = {
        "model": "gpt-4o",
        "stream": True,
        "messages": [{"role": "tool", "tool_call_id": "call_1", "content": "\x1b[31mred\x1b[0m"}],
    }
    captured = {}
    shaping_calls = []
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(captured)),
        patch("proxy.endpoint_execution.stats.record_request"),
        patch(
            "proxy.endpoint_execution.stats.record_context_shaping_action",
            side_effect=lambda **kw: shaping_calls.append(kw),
        ),
    ):
        stream = await execute_single_endpoint(_chat_channel(), request_data, APIType.OPENAI_CHAT, is_stream=True)
        outputs = "".join([chunk async for chunk in stream])

    assert "ok" in outputs
    assert "\x1b[" not in str(captured["json"])
    assert len(shaping_calls) == 1
    assert shaping_calls[0]["feature"] == "strip_ansi"
    assert shaping_calls[0]["after_chars"] < shaping_calls[0]["before_chars"]


@pytest.mark.anyio
async def test_stream_path_no_hit_no_context_shaping_record(monkeypatch):
    monkeypatch.setattr(config, "_settings", dict(CTX_ON))
    captured_calls = []
    request_data = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient({})),
        patch("proxy.endpoint_execution.stats.record_request"),
        patch(
            "proxy.endpoint_execution.stats.record_context_shaping_action",
            side_effect=lambda **kw: captured_calls.append(kw),
        ),
    ):
        stream = await execute_single_endpoint(_chat_channel(), request_data, APIType.OPENAI_CHAT, is_stream=True)
        async for _ in stream:
            pass

    assert captured_calls == []
