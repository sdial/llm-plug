from unittest.mock import patch, AsyncMock

import httpx
import pytest

from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from proxy_core import (
    _do_request,
    _do_stream_request,
    _get_channels_for_model,
    _get_converter_and_upstream_type,
    _get_upstream_url,
    _proxy_single_model_request,
    _yield_anthropic_event,
    _yield_anthropic_events,
    CONVERTER_MAP,
    _model_channels_cache,
)


@pytest.fixture(autouse=True)
def reset_model_cache():
    """每个测试前清理模型渠道缓存。"""
    _model_channels_cache.cache_clear() if hasattr(_model_channels_cache, 'cache_clear') else None
    import proxy_core
    proxy_core._model_channels_cache = None
    yield


class TestGetChannelsForModel:
    @pytest.mark.anyio
    async def test_filters_by_model_and_enabled(self):
        mock_data = {
            "channels": [
                {
                    "id": "ch_1",
                    "name": "Chan A",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://api.openai.com",
                    "api_key": "sk-test",
                    "models": ["gpt-4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
                {
                    "id": "ch_2",
                    "name": "Chan B",
                    "api_type": "anthropic",
                    "base_url": "https://api.anthropic.com",
                    "api_key": "ak-test",
                    "models": ["gpt-4", "claude-opus-4-7"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
                {
                    "id": "ch_3",
                    "name": "Chan C",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://api.openai.com",
                    "api_key": "sk-test",
                    "models": ["gpt-4"],
                    "enabled": False,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        import storage
        with patch.object(storage, 'load_data', new_callable=AsyncMock, return_value=mock_data):
            channels = await _get_channels_for_model("gpt-4")
            assert len(channels) == 2
            assert {ch.id for ch in channels} == {"ch_1", "ch_2"}

    @pytest.mark.anyio
    async def test_returns_empty_when_no_match(self):
        import storage
        with patch.object(storage, 'load_data', new_callable=AsyncMock, return_value={"channels": []}):
            channels = await _get_channels_for_model("gpt-4")
            assert channels == []

    @pytest.mark.anyio
    async def test_excludes_disabled_channels(self):
        mock_data = {
            "channels": [
                {
                    "id": "ch_1",
                    "name": "Chan A",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://api.openai.com",
                    "api_key": "sk-test",
                    "models": ["gpt-4"],
                    "enabled": False,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        import storage
        with patch.object(storage, 'load_data', new_callable=AsyncMock, return_value=mock_data):
            channels = await _get_channels_for_model("gpt-4")
            assert channels == []


class TestGetConverterAndUpstreamType:
    def test_same_type_returns_none(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4"],
        )
        req_conv, resp_conv, source = _get_converter_and_upstream_type(ch, APIType.OPENAI_CHAT)
        assert req_conv is None
        assert resp_conv is None
        assert source == "openai-chat-completions"

    def test_anthropic_to_openai_chat(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-opus-4-7"],
        )
        req_conv, resp_conv, source = _get_converter_and_upstream_type(ch, APIType.OPENAI_CHAT)
        assert isinstance(req_conv, ToAnthropicConverter)
        assert isinstance(resp_conv, ToChatCompletionsConverter)
        assert source == "anthropic"

    def test_openai_chat_to_anthropic(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4"],
        )
        req_conv, resp_conv, source = _get_converter_and_upstream_type(ch, APIType.ANTHROPIC)
        assert isinstance(req_conv, ToChatCompletionsConverter)
        assert isinstance(resp_conv, ToAnthropicConverter)
        assert source == "openai-chat-completions"

    def test_openai_response_to_chat_completions(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_RESPONSE,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4"],
        )
        req_conv, resp_conv, source = _get_converter_and_upstream_type(ch, APIType.OPENAI_CHAT)
        assert isinstance(req_conv, ToResponseConverter)
        assert isinstance(resp_conv, ToChatCompletionsConverter)
        assert source == "openai-response"


class TestGetUpstreamUrl:
    def test_openai_chat_url(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4"],
        )
        assert _get_upstream_url(ch) == "https://api.openai.com/v1/chat/completions"

    def test_openai_response_url(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_RESPONSE,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4"],
        )
        assert _get_upstream_url(ch) == "https://api.openai.com/v1/responses"

    def test_anthropic_url(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-opus-4-7"],
        )
        assert _get_upstream_url(ch) == "https://api.anthropic.com/v1/messages"

    def test_trailing_slash_removed(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com/",
            api_key="sk-test",
            models=["gpt-4"],
        )
        assert _get_upstream_url(ch) == "https://api.openai.com/v1/chat/completions"

    def test_full_path_chat_completions(self):
        """base_url 已包含完整路径时不再拼接"""
        ch = Channel(
            id="ch_1",
            name="Baidu Qianfan",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://qianfan.baidubce.com/v2/coding/chat/completions",
            api_key="sk-test",
            models=["ernie-4.0"],
        )
        assert _get_upstream_url(ch) == "https://qianfan.baidubce.com/v2/coding/chat/completions"

    def test_full_path_chat_completion_singular(self):
        """支持 /chat/completion (无 s) 结尾的路径"""
        ch = Channel(
            id="ch_1",
            name="Baidu Qianfan",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://qianfan.baidubce.com/v2/coding/chat/completion",
            api_key="sk-test",
            models=["ernie-4.0"],
        )
        assert _get_upstream_url(ch) == "https://qianfan.baidubce.com/v2/coding/chat/completion"

    def test_full_path_responses(self):
        """base_url 已包含 /responses 时不再拼接"""
        ch = Channel(
            id="ch_1",
            name="Custom API",
            api_type=APIType.OPENAI_RESPONSE,
            base_url="https://api.example.com/custom/responses",
            api_key="sk-test",
            models=["model-1"],
        )
        assert _get_upstream_url(ch) == "https://api.example.com/custom/responses"

    def test_full_path_messages(self):
        """base_url 已包含 /messages 时不再拼接"""
        ch = Channel(
            id="ch_1",
            name="Custom API",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.example.com/custom/messages",
            api_key="sk-test",
            models=["claude-3"],
        )
        assert _get_upstream_url(ch) == "https://api.example.com/custom/messages"


class TestYieldAnthropicEvent:
    def test_single_event(self):
        result = _yield_anthropic_event("message_start", {"message": {"id": "msg_1"}})
        assert result == 'event: message_start\ndata: {"message": {"id": "msg_1"}}\n\n'

    def test_empty_data(self):
        result = _yield_anthropic_event("ping", {})
        assert result == 'event: ping\ndata: {}\n\n'


class TestYieldAnthropicEvents:
    def test_tuple_events(self):
        events = [
            ("message_start", {"message": {"id": "msg_1"}}),
            ("content_block_delta", {"delta": {"text": "hello"}}),
        ]
        result = _yield_anthropic_events(events)
        lines = result.strip().split("\n\n")
        assert len(lines) == 2
        assert lines[0] == 'event: message_start\ndata: {"message": {"id": "msg_1"}}'
        assert lines[1] == 'event: content_block_delta\ndata: {"delta": {"text": "hello"}}'

    def test_dict_events(self):
        events = [
            {"type": "message_stop"},
        ]
        result = _yield_anthropic_events(events)
        assert result == 'data: {"type": "message_stop"}\n\n'

    def test_mixed_events(self):
        events = [
            ("message_start", {"id": "msg_1"}),
            {"type": "message_stop"},
        ]
        result = _yield_anthropic_events(events)
        lines = result.strip().split("\n\n")
        assert len(lines) == 2
        assert lines[0] == 'event: message_start\ndata: {"id": "msg_1"}'
        assert lines[1] == 'data: {"type": "message_stop"}'


class TestConverterMap:
    def test_all_entries_are_valid(self):
        for (source, target), (req_cls, resp_cls) in CONVERTER_MAP.items():
            assert issubclass(req_cls, object)
            assert issubclass(resp_cls, object)
            # 验证可以实例化
            req_inst = req_cls()
            resp_inst = resp_cls()
            assert hasattr(req_inst, "convert_request")
            assert hasattr(resp_inst, "convert_response")


class TestDoRequest:
    @pytest.mark.anyio
    async def test_same_type_non_stream_skips_capability_filter_and_response_think_filter(self):
        captured = {}
        upstream_response = {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "💭internal💭 visible"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        class FakeClient:
            async def post(self, url, json, headers):
                captured["json"] = json
                request = httpx.Request("POST", url)
                return httpx.Response(200, json=upstream_response, request=request)

        channel = Channel(
            id="ch_deepseek",
            name="DeepSeek",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            models=["deepseek-chat"],
        )
        request_data = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "hello"}],
            "parallel_tool_calls": True,
        }

        with patch("proxy_core.create_client", new_callable=AsyncMock, return_value=FakeClient()), \
                patch("proxy_core._log_debug", new_callable=AsyncMock), \
                patch("proxy_core.stats.record_request", new_callable=AsyncMock):
            response = await _do_request(channel, request_data, APIType.OPENAI_CHAT, is_stream=False)

        assert captured["json"] == request_data
        assert response == upstream_response

    @pytest.mark.anyio
    async def test_non_retryable_upstream_400_is_not_retried(self):
        calls = []

        class BadRequestClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    400,
                    json={"error": {"message": "bad request"}},
                    request=request,
                )

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://primary.example",
            api_key="sk-primary",
            models=["gpt-4o"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://fallback.example",
            api_key="sk-fallback",
            models=["gpt-4o"],
            priority=2,
        )

        with patch("proxy_core._get_channels_for_model", new_callable=AsyncMock, return_value=[primary, fallback]), \
                patch("proxy_core.create_client", new_callable=AsyncMock, return_value=BadRequestClient()), \
                patch("proxy_core._log_debug", new_callable=AsyncMock), \
                patch("proxy_core.stats.record_request", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await _proxy_single_model_request(
                    model="gpt-4o",
                    request_data={"model": "gpt-4o", "messages": []},
                    target_api_type=APIType.OPENAI_CHAT,
                    is_stream=False,
                    query_string=None,
                    client_headers=None,
                    api_key_id=None,
                )

        assert exc_info.value.response.status_code == 400
        assert calls == ["https://primary.example/v1/chat/completions"]


class TestDoStreamRequest:
    @pytest.mark.anyio
    async def test_same_type_anthropic_stream_does_not_leak_event_type(self):
        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: message_start"
                yield 'data: {"type": "message_start", "message": {"id": "msg_1"}}'

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
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3-5-sonnet-20241022"],
        )

        with patch("proxy_core.create_stream_client", return_value=FakeClient()), \
                patch("proxy_core._log_debug", new_callable=AsyncMock), \
                patch("proxy_core.stats.record_request", new_callable=AsyncMock):
            stream = _do_stream_request(
                channel=channel,
                url="https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "claude-3-5-sonnet-20241022", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
            outputs = [chunk async for chunk in stream]

        joined = "".join(outputs)
        assert "event: message_start" in joined
        assert "_event_type" not in joined

    @pytest.mark.anyio
    async def test_same_type_openai_stream_does_not_inject_stream_options(self):
        captured = {}

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"gpt-4o","choices":[]}'
                yield "data: [DONE]"

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeClient:
            def stream(self, method, url, json, headers):
                captured["json"] = json
                return FakeStreamResponse()

            async def aclose(self):
                return None

        channel = Channel(
            id="ch_1",
            name="OpenAI",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4o"],
        )
        request_data = {
            "model": "gpt-4o",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }

        with patch("proxy_core.create_stream_client", return_value=FakeClient()), \
                patch("proxy_core._log_debug", new_callable=AsyncMock), \
                patch("proxy_core.stats.record_request", new_callable=AsyncMock):
            stream = await _do_request(channel, request_data, APIType.OPENAI_CHAT, is_stream=True)
            outputs = [chunk async for chunk in stream]

        assert outputs
        assert captured["json"] == request_data

    @pytest.mark.anyio
    async def test_stream_retries_next_channel_when_first_channel_fails_before_output(self):
        class FailingStreamResponse:
            async def __aenter__(self):
                request = httpx.Request("POST", "https://primary.example/v1/chat/completions")
                raise httpx.ConnectError("connect failed", request=request)

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FailingClient:
            def stream(self, *args, **kwargs):
                return FailingStreamResponse()

            async def aclose(self):
                return None

        class WorkingStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"id":"chatcmpl_2","object":"chat.completion.chunk","model":"gpt-4o","choices":[{"index":0,"delta":{"content":"fallback"},"finish_reason":null}]}'
                yield "data: [DONE]"

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class WorkingClient:
            def stream(self, *args, **kwargs):
                return WorkingStreamResponse()

            async def aclose(self):
                return None

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://primary.example",
            api_key="sk-primary",
            models=["gpt-4o"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://fallback.example",
            api_key="sk-fallback",
            models=["gpt-4o"],
            priority=2,
        )

        def fake_stream_client(channel):
            return FailingClient() if channel.id == "ch_primary" else WorkingClient()

        with patch("proxy_core._get_channels_for_model", new_callable=AsyncMock, return_value=[primary, fallback]), \
                patch("proxy_core.create_stream_client", side_effect=fake_stream_client), \
                patch("proxy_core._log_debug", new_callable=AsyncMock), \
                patch("proxy_core.stats.record_request", new_callable=AsyncMock):
            stream, selected = await _proxy_single_model_request(
                model="gpt-4o",
                request_data={"model": "gpt-4o", "stream": True, "messages": []},
                target_api_type=APIType.OPENAI_CHAT,
                is_stream=True,
                query_string=None,
                client_headers=None,
                api_key_id=None,
            )
            outputs = [chunk async for chunk in stream]

        assert selected.id == "ch_fallback"
        assert "fallback" in "".join(outputs)

    @pytest.mark.anyio
    async def test_emits_response_completed_before_done_when_finish_reason_missing(self):
        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"glm-5","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}'
                yield 'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"glm-5","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}'
                yield "data: [DONE]"

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
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["glm-5"],
        )

        with patch("proxy_core.create_stream_client", return_value=FakeClient()), \
                patch("proxy_core._log_debug", new_callable=AsyncMock), \
                patch("proxy_core.stats.record_request", new_callable=AsyncMock):
            stream = _do_stream_request(
                channel=channel,
                url="https://api.openai.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "glm-5", "stream": True},
                response_converter=ToResponseConverter(),
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_RESPONSE,
            )
            outputs = [chunk async for chunk in stream]

        joined = "".join(outputs)
        assert "event: response.completed" in joined
        assert '"text": "Hello"' in joined
