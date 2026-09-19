"""[DONE] / EOF 收尾输出统一（ADR-0015 D3）的行为 golden：think flush 残余字节钉死。

驱动 `_do_stream_request` 整体（骨架级 golden，允许 FakeStreamResponse+FakeClient 存量
fixture 风格），断言收尾段输出的字节级形状：

- [DONE] 正常收尾：ThinkFilter 残余以目标格式合法事件输出，随后终止行；
- EOF 无 [DONE] 兜底：同构残余输出 + 终行兜底；
- Anthropic 目标 EOF 残余必须以合法 content_block_delta（text_delta，复用最近
  text 块 index 的 M2 语义）输出，而非 Chat 形状裸 data: 行（M2 回归钉死）。
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


# 上游序列：正文 chunk 后紧跟未闭合的 💭 think 块（残余只能靠 flush 吐出）
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

_CHAT_RESIDUAL_CHUNK_WIRE = (
    'data: {"id": "chatcmpl-stream", "object": "chat.completion.chunk", "created": 0, "model": "test-model", '
    '"choices": [{"index": 0, "delta": {"content": "💭secret"}, "finish_reason": null}]}\n\n'
)

_ANTHROPIC_RESIDUAL_DELTA_WIRE = (
    'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "💭secret"}}\n\n'
)


async def _collect(coro_gen):
    return [chunk async for chunk in coro_gen]


# ─── [DONE] 正常收尾 ───


@pytest.mark.asyncio
async def test_done_phase_chat_think_flush_residual_bytes():
    """收到 [DONE] 时 ThinkFilter 残余以 Chat chunk 输出，随后是 [DONE] 终止行（字节钉死）。"""
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
    # 残余 chunk：字节级形状（flush("💭sec"+"ret") → "💭secret"）
    assert outputs[1] == _CHAT_RESIDUAL_CHUNK_WIRE
    # 终止行兜底
    assert outputs[-1] == "data: [DONE]\n\n"
    # 💭 think 块内部不再额外泄漏（残余以 flush 单事件输出）
    assert len(outputs) == 3


# ─── EOF 无 [DONE] 兜底 ───


@pytest.mark.asyncio
async def test_eof_phase_chat_think_flush_residual_bytes():
    """上游 EOF 无 [DONE]：残余 chunk 与 [DONE] 路径同构，终止行兜底（字节钉死）。"""
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
    assert outputs[1] == _CHAT_RESIDUAL_CHUNK_WIRE
    assert outputs[-1] == "data: [DONE]\n\n"
    assert len(outputs) == 3


@pytest.mark.asyncio
async def test_eof_phase_anthropic_think_flush_residual_is_legal_content_block_delta():
    """M2 语义：Anthropic 目标 EOF 残余以合法 content_block_delta（text_delta）输出，
    复用最近 text 块 index，并以 message_stop 补协议终止（字节钉死）。"""
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
    # 残余：合法 Anthropic content_block_delta，index 复用最近的 text 块 index=0
    assert outputs[1] == _ANTHROPIC_RESIDUAL_DELTA_WIRE
    # EOF 直通路径终行兜底：补 message_stop 协议终止事件
    assert outputs[-1] == 'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    # 不得出现 Chat 形状裸 data: 行（M2 回归）
    assert not any(line.startswith("data: ") and "chatcmpl-stream" in line for line in outputs)
    assert len(outputs) == 3
