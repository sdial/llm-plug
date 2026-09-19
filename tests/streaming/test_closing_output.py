"""[DONE] / EOF 收尾输出统一（ADR-0015 D3）的行为 golden。

驱动 `_do_stream_request` 整体（骨架级 golden，允许 FakeStreamResponse+FakeClient 存量
fixture 风格），断言收尾段输出的字节级形状：

- [DONE] 正常收尾：未闭合思考块残余被丢弃，随后输出终止行；
- EOF 无 [DONE] 兜底：同样丢弃未闭合思考块，并补齐目标协议终止事件。
"""

from unittest.mock import patch

import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy.stream_executor import _do_stream_request

# ─── fixtures ───


class FakeStreamResponse:
    status_code = 200
    is_error = False
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, *args, **kwargs):
        return FakeStreamResponse(self._lines)

    async def aclose(self):
        return None


def _chat_channel(channel_id: str) -> Channel:
    return Channel(
        id=channel_id,
        name="Test",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://upstream.example.com")],
        api_key="sk-test",
        models=["test-model"],
    )


def _anthropic_channel(channel_id: str) -> Channel:
    return Channel(
        id=channel_id,
        name="Test",
        endpoints=[Endpoint(api_type=APIType.ANTHROPIC, base_url="https://upstream.example.com")],
        api_key="sk-test",
        models=["test-model"],
    )


# 上游序列：正文 chunk 后紧跟未闭合的 💭 think 块（收尾时必须丢弃）
_CHAT_LINES_WITH_UNCLOSED_THINK = [
    'data: {"id":"c1","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',  # noqa: E501
    "",
    'data: {"id":"c2","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{"content":"💭sec"},"finish_reason":null}]}',  # noqa: E501
    "",
    'data: {"id":"c3","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{"content":"ret"},"finish_reason":null}]}',
    "",
]

_ANTHROPIC_LINES_WITH_UNCLOSED_THINK = [
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
    "",
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"💭sec"}}',
    "",
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ret"}}',
    "",
]


async def _collect(coro_gen):
    return [chunk async for chunk in coro_gen]


# ─── [DONE] 正常收尾 ───


@pytest.mark.asyncio
async def test_done_phase_chat_discards_unclosed_think():
    """收到 [DONE] 时丢弃未闭合思考块，随后是 [DONE] 终止行。"""
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([*_CHAT_LINES_WITH_UNCLOSED_THINK, "data: [DONE]"])),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_chat_channel("ch_done_residual"),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
                need_think_filter=True,
            )
        )

    assert "Hello" in outputs[0]
    assert outputs[-1] == "data: [DONE]\n\n"
    assert "secret" not in "".join(outputs)
    assert len(outputs) == 2


# ─── EOF 无 [DONE] 兜底 ───


@pytest.mark.asyncio
async def test_eof_phase_chat_discards_unclosed_think():
    """上游 EOF 无 [DONE]：丢弃未闭合思考块，并补齐终止行。"""
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(list(_CHAT_LINES_WITH_UNCLOSED_THINK))),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_chat_channel("ch_eof_residual"),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
                need_think_filter=True,
            )
        )

    assert "Hello" in outputs[0]
    assert outputs[-1] == "data: [DONE]\n\n"
    assert "secret" not in "".join(outputs)
    assert len(outputs) == 2


@pytest.mark.asyncio
async def test_eof_phase_anthropic_discards_unclosed_think():
    """Anthropic 目标 EOF 丢弃未闭合思考块，并以 message_stop 补协议终止。"""
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(list(_ANTHROPIC_LINES_WITH_UNCLOSED_THINK))),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_anthropic_channel("ch_eof_anthropic_residual"),
                url="https://upstream.example.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
                need_think_filter=True,
            )
        )

    assert "Hello" in outputs[0]
    assert outputs[-1] == 'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    assert "secret" not in "".join(outputs)
    assert not any(line.startswith("data: ") and "chatcmpl-stream" in line for line in outputs)
    assert len(outputs) == 2
