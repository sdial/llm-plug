"""错误事件三格式输出统一（ADR-0015 D3 第 4 对）的行为 golden。

驱动 `_do_stream_request` 整体（骨架级 golden，允许 FakeStreamResponse+FakeClient 存量
fixture 风格），钉死两个调用点（non-SSE 解析失败、流式异常处理器）× 三种目标格式的
字节级输出形状：

- non-SSE 解析失败：上游返回非 JSON 文本 → 按目标格式产出错误事件 + 协议终止事件；
- 流中异常：converter 路径下无效 JSON chunk 抛 ConverterError（已有真实输出，不走
  首包前预检）→ 按目标格式产出错误事件 + 协议终止事件。

错误消息约定（现状钉死）：non-SSE 解析失败三格式统一 "Upstream returned unparseable
response"；异常处理器 Anthropic 目标用 str(e)、Responses/Chat 目标加 "流式传输错误: " 前缀。
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
    headers = {"content-type": "text/plain"}

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


def _channel(channel_id: str, api_type: APIType) -> Channel:
    return Channel(
        id=channel_id,
        name="Test",
        endpoints=[Endpoint(api_type=api_type, base_url="https://upstream.example.com")],
        api_key="sk-test",
        models=["test-model"],
    )


class PassThroughConverter:
    """首个 chunk 原样透传（产出真实输出），后续无效 JSON chunk 由执行器抛 ConverterError。"""

    def convert_stream_chunk(self, chunk, source_type):
        return [chunk]

    def finalize_stream(self, source_type):
        return []

    def convert_response(self, response, source_type):
        return response


# 上游返回的非 SSE 非 JSON 文本（首行即非 data: 非 : 注释 → non_sse_stream_body 兜底）
_NON_SSE_BODY_LINE = "this is not json and not sse"

# converter 路径：先真实输出一个 chunk，再投喂无效 JSON 触发 ConverterError
_CHAT_LINES = [
    'data: {"id":"c1","object":"chat.completion.chunk","model":"test-model","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',  # noqa: E501
    "",
    "data: not-json",
    "",
]
_ANTHROPIC_LINES = [
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
    "",
    "data: not-json",
    "",
]
_RESPONSES_LINES = [
    'data: {"type":"response.output_text.delta","delta":"Hello"}',
    "",
    "data: not-json",
    "",
]

_RESPONSES_IMAGE_AFTER_TEXT_LINES = [
    'data: {"type":"response.output_text.delta","delta":"Hello"}',
    "",
    'data: {"type":"response.image_generation_call.partial_image","partial_image_b64":"AA=="}',
    "",
]


async def _collect(coro_gen):
    return [chunk async for chunk in coro_gen]


# ─── non-SSE 解析失败（调用点 1）───


@pytest.mark.asyncio
async def test_non_sse_parse_failure_chat_error_bytes():
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([_NON_SSE_BODY_LINE])),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_channel("ch_nonsse_chat", APIType.OPENAI_CHAT),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
            )
        )

    assert outputs == [
        'data: {"error": {"message": "Upstream returned unparseable response", "type": "api_error"}}\n\n',
        "data: [DONE]\n\n",
    ]


@pytest.mark.asyncio
async def test_non_sse_parse_failure_anthropic_error_bytes():
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([_NON_SSE_BODY_LINE])),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_channel("ch_nonsse_anthropic", APIType.ANTHROPIC),
                url="https://upstream.example.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
        )

    assert outputs == [
        'event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "Upstream returned unparseable response"}}\n\n',
        'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]


@pytest.mark.asyncio
async def test_non_sse_parse_failure_responses_error_bytes():
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([_NON_SSE_BODY_LINE])),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_channel("ch_nonsse_responses", APIType.OPENAI_RESPONSE),
                url="https://upstream.example.com/v1/responses",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-response",
                target_api_type=APIType.OPENAI_RESPONSE,
            )
        )

    assert outputs == [
        'event: error\ndata: {"type": "error", "error": {"message": "Upstream returned unparseable response", "type": "api_error"}}\n\n',
        "event: response.failed\n"
        'data: {"type": "response.failed", "response": {'
        '"id": "", "object": "response", "status": "failed", "model": "test-model", '
        '"output": [], '
        '"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}'
        "}}\n\n",
    ]


# ─── 流中异常处理器（调用点 2）───


@pytest.mark.asyncio
async def test_mid_stream_exception_chat_error_bytes():
    """converter 路径下无效 JSON chunk 抛 ConverterError：错误 chunk（带前缀消息）+ [DONE]（字节钉死）。"""
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(list(_CHAT_LINES))),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_channel("ch_exc_chat", APIType.OPENAI_CHAT),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=PassThroughConverter(),
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
            )
        )

    # 首个 chunk 已作为真实输出透传
    assert "Hello" in outputs[0]
    # 错误消息：Responses/Chat 目标加 "流式传输错误: " 前缀（现状钉死）
    assert outputs[-2:] == [
        'data: {"error": {"message": "流式传输错误: 流式 chunk 不是有效 JSON: not-json", "type": "api_error"}}\n\n',
        "data: [DONE]\n\n",
    ]
    assert len(outputs) == 3


@pytest.mark.asyncio
async def test_mid_stream_exception_anthropic_error_bytes():
    """Anthropic 目标：错误消息为 str(e) 原文（无前缀，现状钉死）+ message_stop。"""
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(list(_ANTHROPIC_LINES))),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_channel("ch_exc_anthropic", APIType.ANTHROPIC),
                url="https://upstream.example.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=PassThroughConverter(),
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
        )

    assert "Hello" in outputs[0]
    assert outputs[-2:] == [
        'event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "流式 chunk 不是有效 JSON: not-json"}}\n\n',
        'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]
    assert len(outputs) == 3


@pytest.mark.asyncio
async def test_mid_stream_exception_responses_error_bytes():
    """Responses 目标：response.failed 终止事件跟随错误事件（字节钉死）。"""
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(list(_RESPONSES_LINES))),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_channel("ch_exc_responses", APIType.OPENAI_RESPONSE),
                url="https://upstream.example.com/v1/responses",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=PassThroughConverter(),
                source_type="openai-response",
                target_api_type=APIType.OPENAI_RESPONSE,
            )
        )

    assert "Hello" in outputs[0]
    assert outputs[-2:] == [
        'event: error\ndata: {"type": "error", "error": {"message": "流式传输错误: 流式 chunk 不是有效 JSON: not-json", "type": "api_error"}}\n\n',
        "event: response.failed\n"
        'data: {"type": "response.failed", "response": {'
        '"id": "", "object": "response", "status": "failed", "model": "test-model", '
        '"output": [], '
        '"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}'
        "}}\n\n",
    ]
    assert len(outputs) == 3


@pytest.mark.asyncio
async def test_mid_stream_incompatible_output_records_partial_response_without_health_penalty():
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(list(_RESPONSES_IMAGE_AFTER_TEXT_LINES))),
        patch("proxy.stream_executor._record_request") as record_request,
        patch("proxy.stream_executor.outcomes.record") as record_outcome,
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_channel("ch_incompatible_output", APIType.OPENAI_RESPONSE),
                url="https://upstream.example.com/v1/responses",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=PassThroughConverter(),
                source_type="openai-response",
                target_api_type=APIType.OPENAI_CHAT,
            )
        )

    assert "Hello" in outputs[0]
    assert "partial_response=true" in outputs[-2]
    assert outputs[-1] == "data: [DONE]\n\n"
    assert "partial_response=true" in record_request.call_args.kwargs["error_msg"]
    record_outcome.assert_not_called()
