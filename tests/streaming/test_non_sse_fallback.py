"""non-SSE JSON 兜底接缝（ADR-0015 D0 接缝 3）的行为 golden：字节级钉死。

覆盖两层面：

- 骨架级 golden：驱动 `_do_stream_request` 整体（允许 FakeStreamResponse+FakeClient
  存量 fixture 风格），断言三目标（Chat / Anthropic / Responses）non-SSE 整块与解析
  失败场景的输出字节序列与记账捕获结果；
- 接缝直测：直接 import `_emit_non_sse_fallback_events` /
  `_NonSseUsageResult`（私有接缝，不进 `__all__`），钉死 usage 归一语义
  （Anthropic 语义 input_tokens 补加缓存两项的归一）。
"""

import json
from unittest.mock import patch

import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy.stream_executor import _do_stream_request, _emit_non_sse_fallback_events, _NonSseUsageResult

# ─── fixtures（与 test_closing_output 同风格的骨架级 fixture）───


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


def _responses_channel(channel_id: str) -> Channel:
    return Channel(
        id=channel_id,
        name="Test",
        endpoints=[Endpoint(api_type=APIType.OPENAI_RESPONSE, base_url="https://upstream.example.com")],
        api_key="sk-test",
        models=["test-model"],
    )


def _sse(d) -> str:
    """与 format_sse_for_list 同构的 data-only wire 形状（无 event 行）。"""
    return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"


def _sse_event(event_type: str, d) -> str:
    """与 yield_anthropic_event 同构的 event:+data: wire 形状。"""
    return f"event: {event_type}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"


async def _collect(gen):
    return [chunk async for chunk in gen]


# ─── Chat 目标：整块 chat.completion 拆 chunk 序列 ───

_CHAT_NON_SSE_BODY = json.dumps(
    {
        "id": "chatcmpl-nonsse",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
)

_CHAT_CHUNK_BASE = {"id": "chatcmpl-nonsse", "object": "chat.completion.chunk", "created": 1700000000, "model": "test-model"}


@pytest.mark.asyncio
async def test_chat_target_non_sse_json_bytes_and_usage_capture():
    captured = {}
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([_CHAT_NON_SSE_BODY])),
        patch("proxy.stream_executor._record_request", lambda **record: captured.update(record)),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_chat_channel("ch_nonsse_chat2"),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
            )
        )

    assert outputs == [
        _sse({**_CHAT_CHUNK_BASE, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _sse({**_CHAT_CHUNK_BASE, "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]}),
        _sse({**_CHAT_CHUNK_BASE, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        _sse({**_CHAT_CHUNK_BASE, "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}),
        "data: [DONE]\n\n",
    ]
    # usage/finish_reason 捕获结果与现状语义一致
    assert captured["input_tokens"] == 5
    assert captured["output_tokens"] == 2
    assert captured["finish_reason"] == "stop"
    assert captured["success"] is True


@pytest.mark.asyncio
async def test_chat_target_non_sse_json_think_filter_swallows_marked_content():
    """非 SSE 拆块中的 think 过滤：💭 标记内容块被吞且残余不 flush（现状语义钉死）。"""
    body = json.dumps(
        {
            "id": "chatcmpl-think",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "💭hidden"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    base = {"id": "chatcmpl-think", "object": "chat.completion.chunk", "created": 1700000000, "model": "test-model"}
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([body])),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_chat_channel("ch_nonsse_chat_think"),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
                need_think_filter=True,
            )
        )

    assert outputs == [
        _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        _sse({**base, "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
        "data: [DONE]\n\n",
    ]
    # 现状语义钉死：非 SSE 路径不经过 [DONE]/EOF 收尾接缝，ThinkFilter 残余不 flush，
    # 💭 标记内容（含残余）整体不泄漏为正文输出
    assert not any("💭" in line for line in outputs)


# ─── Anthropic 目标：整块 message 拆事件序列 ───

_ANTHROPIC_NON_SSE_OBJ = {
    "id": "msg_nonsse",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "Hi"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 1},
}
_ANTHROPIC_NON_SSE_BODY = json.dumps(_ANTHROPIC_NON_SSE_OBJ)


@pytest.mark.asyncio
async def test_anthropic_target_non_sse_json_split_to_message_events_bytes():
    """Anthropic 直通：非 SSE JSON 拆为 message_start/.../message_stop 事件序列，无 [DONE]（字节钉死）。"""
    captured = {}
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([_ANTHROPIC_NON_SSE_BODY])),
        patch("proxy.stream_executor._record_request", lambda **record: captured.update(record)),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_anthropic_channel("ch_nonsse_anthropic"),
                url="https://upstream.example.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
        )

    message_start_obj = {k: v for k, v in _ANTHROPIC_NON_SSE_OBJ.items() if k not in ("stop_reason", "stop_sequence")}
    message_start_obj["usage"] = {"input_tokens": 3, "output_tokens": 0}
    assert outputs == [
        _sse_event("message_start", {"message": message_start_obj}),
        _sse_event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        _sse_event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        _sse_event("content_block_stop", {"index": 0}),
        _sse_event("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        _sse_event("message_stop", {}),
    ]
    # usage/finish_reason 捕获：stop_reason 作为 finish_reason
    assert captured["input_tokens"] == 3
    assert captured["output_tokens"] == 1
    assert captured["finish_reason"] == "end_turn"
    assert captured["success"] is True


# ─── Responses 目标：整块 Response 拆事件序列 ───

_RESPONSES_NON_SSE_OBJ = {
    "id": "resp_nonsse",
    "object": "response",
    "status": "completed",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "Hey"}]}],
    "usage": {"input_tokens": 4, "output_tokens": 2},
}
_RESPONSES_NON_SSE_BODY = json.dumps(_RESPONSES_NON_SSE_OBJ)


@pytest.mark.asyncio
async def test_responses_target_non_sse_json_split_to_events_bytes():
    """Responses 透传：非 SSE JSON 拆为 response.created → ... → response.completed 事件序列（字节钉死）。"""
    captured = {}
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient([_RESPONSES_NON_SSE_BODY])),
        patch("proxy.stream_executor._record_request", lambda **record: captured.update(record)),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_responses_channel("ch_nonsse_responses"),
                url="https://upstream.example.com/v1/responses",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-response",
                target_api_type=APIType.OPENAI_RESPONSE,
            )
        )

    item = {"type": "message", "content": [{"type": "output_text", "text": "Hey"}]}
    part_added = {"type": "response.content_part.added", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}}
    part_done = {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": item["content"][0]}
    assert outputs == [
        _sse_event("response.created", {"type": "response.created", "response": _RESPONSES_NON_SSE_OBJ}),
        _sse_event("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": item}),
        _sse_event("response.content_part.added", part_added),
        _sse_event("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "Hey"}),
        _sse_event("response.content_part.done", part_done),
        _sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
        _sse_event("response.completed", {"type": "response.completed", "response": {**_RESPONSES_NON_SSE_OBJ, "status": "completed"}}),
    ]
    # usage 嵌套归一：input_tokens / output_tokens 正确捕获
    assert captured["input_tokens"] == 4
    assert captured["output_tokens"] == 2
    assert captured["success"] is True


# ─── 解析失败：三格式错误事件接缝 ───

_ERROR_CHUNK_WIRE = 'data: {"error": {"message": "Upstream returned unparseable response", "type": "api_error"}}\n\n'


@pytest.mark.asyncio
async def test_non_sse_parse_failure_emits_error_and_done_chat():
    """解析失败（非法 JSON）：Chat 目标输出 error chunk + [DONE]，记账复位为失败（字节钉死）。"""
    captured = {}
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(["not-json{"])),
        patch("proxy.stream_executor._record_request", lambda **record: captured.update(record)),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_chat_channel("ch_nonsse_parse_fail"),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
            )
        )

    assert outputs == [_ERROR_CHUNK_WIRE, "data: [DONE]\n\n"]
    assert captured["success"] is False
    assert captured["error_msg"] == "non_sse_json_parse_error"


@pytest.mark.asyncio
async def test_non_sse_non_dict_json_body_treated_as_parse_failure():
    """解析为非 dict 整块（如 JSON 数组）同样走解析失败分支。"""
    captured = {}
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(["[1, 2]"])),
        patch("proxy.stream_executor._record_request", lambda **record: captured.update(record)),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_chat_channel("ch_nonsse_non_dict"),
                url="https://upstream.example.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "test-model", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
            )
        )

    assert outputs == [_ERROR_CHUNK_WIRE, "data: [DONE]\n\n"]
    assert captured["success"] is False
    assert captured["error_msg"] == "non_sse_json_parse_error"


@pytest.mark.asyncio
async def test_non_sse_parse_failure_anthropic_target_error_events():
    """解析失败：Anthropic 目标输出三格式错误事件（error + message_stop），无 [DONE]。"""
    with (
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient(["not-json{"])),
        patch("proxy.stream_executor._record_request"),
    ):
        outputs = await _collect(
            _do_stream_request(
                channel=_anthropic_channel("ch_nonsse_parse_fail_anthropic"),
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


# ─── 接缝直测：usage 归一语义 ───


def _noop_async_error_events(message):
    async def _gen():
        return
        yield

    return _gen()


@pytest.mark.asyncio
async def test_seam_usage_normalization_anthropic_cache_semantics():
    """Anthropic 语义归一：usage 无 prompt_tokens / input_tokens_details 时，
    input_tokens 补加缓存两项（总输入 = input_tokens + cache_read + cache_creation）。"""
    body = json.dumps(
        {
            "id": "msg_cache",
            "type": "message",
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1, "output_tokens": 1},
        }
    )
    usage_out = _NonSseUsageResult(input_tokens=99, output_tokens=99)
    chunks = []
    async for sse in _emit_non_sse_fallback_events(
        body,
        model="test-model",
        source_type="anthropic",
        response_converter=None,
        output_anthropic_sse=False,
        output_responses_sse=False,
        output_sse_events=False,
        is_upstream_anthropic=True,
        think_filter=None,
        cur_input_tokens=99,
        cur_output_tokens=99,
        cur_finish_reason=None,
        usage_out=usage_out,
        record_chunk=lambda item: chunks.append(item),
        mark_first_token=lambda: None,
        mark_output=lambda: None,
        log_event=lambda sse: None,
        emit_error_events=_noop_async_error_events,
    ):
        chunks.append(sse)

    # choices 为空 → Chat 拆块返回空 → 整块对象原样单事件输出 + [DONE]
    assert chunks[-1] == "data: [DONE]\n\n"
    assert usage_out.parse_failed is False
    assert usage_out.input_tokens == 3 + 2 + 1
    assert usage_out.cache_read_input_tokens == 2
    assert usage_out.cache_creation_input_tokens == 1
    assert usage_out.output_tokens == 1


def test_seam_is_private_importable_not_in_all():
    """接缝可直接 import 且不进 __all__（D0 接缝约定）。"""
    import proxy.stream_executor as se

    assert callable(se._emit_non_sse_fallback_events)
    assert "_emit_non_sse_fallback_events" not in se.__all__
    assert "_NonSseUsageResult" not in se.__all__
