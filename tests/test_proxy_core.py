from unittest.mock import patch, AsyncMock

import httpx
import pytest

from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from proxy_core import (
    _build_anthropic_stream_response,
    _build_openai_stream_response,
    _do_request,
    _do_stream_request,
    _get_channels_for_model,
    _get_converter_and_upstream_type,
    _get_upstream_url,
    _proxy_single_model_request,
    _raise_preflight_stream_errors,
    _yield_anthropic_event,
    CONVERTER_MAP,
    _is_channel_config_error,
    _model_channels_cache,
)


def test_openai_stream_archive_created_uses_time_time():
    """OpenAI 流式归档响应的 created 应使用 time.time()。"""
    chunks = [
        {
            "id": "chatcmpl_1",
            "choices": [{"delta": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
    ]

    with patch("proxy_core.time.time", return_value=1234.9):
        result = _build_openai_stream_response(chunks, "gpt-4o")

    assert result["created"] == 1234


@pytest.fixture(autouse=True)
def reset_model_cache():
    """每个测试前清理模型渠道缓存。"""
    _model_channels_cache.cache_clear() if hasattr(
        _model_channels_cache, "cache_clear"
    ) else None
    import proxy_core

    proxy_core._model_channels_cache = None
    proxy_core._model_channels_cache_version = 0
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

        with patch.object(
            storage, "load_data", new_callable=AsyncMock, return_value=mock_data
        ):
            channels = await _get_channels_for_model("gpt-4")
            assert len(channels) == 2
            assert {ch.id for ch in channels} == {"ch_1", "ch_2"}

    @pytest.mark.anyio
    async def test_returns_empty_when_no_match(self):
        import storage

        with patch.object(
            storage, "load_data", new_callable=AsyncMock, return_value={"channels": []}
        ):
            channels = await _get_channels_for_model("gpt-4")
            assert channels == []

    @pytest.mark.anyio
    async def test_skips_invalid_channel_entries(self):
        import storage

        mock_data = {
            "channels": [
                {"id": "broken", "name": "Missing required fields"},
                {
                    "id": "ch_valid",
                    "name": "Valid",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://api.example.com",
                    "api_key": "sk-test",
                    "models": ["gpt-4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }

        with patch.object(
            storage, "load_data", new_callable=AsyncMock, return_value=mock_data
        ):
            channels = await _get_channels_for_model("gpt-4")

        assert [ch.id for ch in channels] == ["ch_valid"]

    @pytest.mark.anyio
    async def test_save_data_invalidates_model_channels_cache_immediately(self):
        import proxy_core
        import storage

        old_payload = {
            "channels": [
                {
                    "id": "ch_old",
                    "name": "Old",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://old.example",
                    "api_key": "sk-old",
                    "models": ["gpt-4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                }
            ]
        }
        new_payload = {
            "channels": [
                {
                    "id": "ch_new",
                    "name": "New",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://new.example",
                    "api_key": "sk-new",
                    "models": ["gpt-4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                }
            ]
        }

        await storage.save_data(old_payload)
        channels = await _get_channels_for_model("gpt-4")
        assert [ch.id for ch in channels] == ["ch_old"]

        await storage.save_data(new_payload)
        assert proxy_core._model_channels_cache is None

        channels = await _get_channels_for_model("gpt-4")
        assert [ch.id for ch in channels] == ["ch_new"]

    @pytest.mark.anyio
    async def test_does_not_cache_channels_loaded_during_invalidation(self):
        import proxy_core
        import storage

        old_payload = {
            "channels": [
                {
                    "id": "ch_old",
                    "name": "Old",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://old.example",
                    "api_key": "sk-old",
                    "models": ["gpt-4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                }
            ]
        }
        new_payload = {
            "channels": [
                {
                    "id": "ch_new",
                    "name": "New",
                    "api_type": "openai-chat-completions",
                    "base_url": "https://new.example",
                    "api_key": "sk-new",
                    "models": ["gpt-4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                }
            ]
        }

        async def load_data():
            if load_data.calls == 0:
                load_data.calls += 1
                proxy_core._schedule_invalidate_model_channels_cache()
                return old_payload
            load_data.calls += 1
            return new_payload

        load_data.calls = 0

        with patch.object(storage, "load_data", load_data):
            channels = await _get_channels_for_model("gpt-4")

        assert [ch.id for ch in channels] == ["ch_new"]
        assert [ch.id for ch in proxy_core._model_channels_cache["gpt-4"]] == ["ch_new"]


class TestStreamPreflight:
    @pytest.mark.anyio
    async def test_closes_inner_generator_when_preflight_error_is_raised(self):
        closed = False

        async def gen():
            nonlocal closed
            try:
                request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
                response = httpx.Response(500, request=request)
                raise httpx.HTTPStatusError("upstream error", request=request, response=response)
                yield b"unreachable"
            finally:
                closed = True

        wrapped = _raise_preflight_stream_errors(gen())
        with pytest.raises(Exception) as exc_info:
            await wrapped.__anext__()

        assert exc_info.value.__class__.__name__ == "_StreamPreflightError"
        assert closed is True


class TestChannelConfigError:
    def test_auth_and_not_found_statuses_are_channel_config_errors(self):
        request = httpx.Request("POST", "https://upstream.example/v1/messages")
        for status_code in (401, 403, 404):
            response = httpx.Response(status_code, request=request)
            exc = httpx.HTTPStatusError("upstream error", request=request, response=response)

            assert _is_channel_config_error(exc) is True

    def test_non_config_status_is_not_channel_config_error(self):
        request = httpx.Request("POST", "https://upstream.example/v1/messages")
        response = httpx.Response(429, request=request)
        exc = httpx.HTTPStatusError("rate limited", request=request, response=response)

        assert _is_channel_config_error(exc) is False

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

        with patch.object(
            storage, "load_data", new_callable=AsyncMock, return_value=mock_data
        ):
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
        req_conv, resp_conv, source = _get_converter_and_upstream_type(
            ch, APIType.OPENAI_CHAT
        )
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
        req_conv, resp_conv, source = _get_converter_and_upstream_type(
            ch, APIType.OPENAI_CHAT
        )
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
        req_conv, resp_conv, source = _get_converter_and_upstream_type(
            ch, APIType.ANTHROPIC
        )
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
        req_conv, resp_conv, source = _get_converter_and_upstream_type(
            ch, APIType.OPENAI_CHAT
        )
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

    def test_anthropic_base_url_ending_v1(self):
        ch = Channel(
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com/v1",
            api_key="ak-test",
        )

        assert _get_upstream_url(ch) == "https://api.anthropic.com/v1/messages"

    def test_openai_chat_base_url_ending_v1(self):
        ch = Channel(
            name="OpenAI",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )

        assert _get_upstream_url(ch) == "https://api.openai.com/v1/chat/completions"

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
        assert (
            _get_upstream_url(ch)
            == "https://qianfan.baidubce.com/v2/coding/chat/completions"
        )

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
        assert (
            _get_upstream_url(ch)
            == "https://qianfan.baidubce.com/v2/coding/chat/completion"
        )

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

    def test_endpoint_url_overrides_base_url(self):
        ch = Channel(
            id="ch_1",
            name="Custom API",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.example.com",
            endpoint_url="https://gateway.example.com/custom/chat",
            api_key="sk-test",
            models=["gpt-4"],
        )

        assert _get_upstream_url(ch) == "https://gateway.example.com/custom/chat"


class TestYieldAnthropicEvent:
    def test_single_event(self):
        result = _yield_anthropic_event("message_start", {"message": {"id": "msg_1"}})
        assert result == 'event: message_start\ndata: {"message": {"id": "msg_1"}}\n\n'

    def test_empty_data(self):
        result = _yield_anthropic_event("ping", {})
        assert result == "event: ping\ndata: {}\n\n"


class TestBuildAnthropicStreamResponse:
    def test_preserves_block_order_and_signature(self):
        chunks = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "claude-3",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 3,
                    },
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "plan"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig_1"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "hello"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "calc",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"x":'},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": "1}"},
            },
            {"type": "content_block_stop", "index": 2},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        ]

        response = _build_anthropic_stream_response(chunks, "claude-3")

        assert response["content"] == [
            {"type": "thinking", "thinking": "plan", "signature": "sig_1"},
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "toolu_1", "name": "calc", "input": {"x": 1}},
        ]
        assert response["stop_reason"] == "tool_use"
        assert response["stop_sequence"] is None
        assert response["usage"]["input_tokens"] == 10
        assert response["usage"]["output_tokens"] == 7
        assert response["usage"]["cache_read_input_tokens"] == 3


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
    async def test_same_type_non_stream_applies_capability_filter_and_response_think_filter(
        self,
    ):
        """同格式透传时，capabilities 描述的是上游真实约束，必须仍然生效。

        DeepSeek（base_url 命中关键字）：
        - 请求侧应剥除 parallel_tool_calls
        - 响应侧应过滤 💭...💭 思考内容
        """
        captured = {}
        upstream_response = {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "💭internal💭 visible"},
                    "finish_reason": "stop",
                }
            ],
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

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            response = await _do_request(
                channel, request_data, APIType.OPENAI_CHAT, is_stream=False
            )

        # 请求侧：parallel_tool_calls 已被 capability 过滤
        assert "parallel_tool_calls" not in captured["json"]
        # 响应侧：💭 内容已被剥除
        assert response["choices"][0]["message"]["content"] == "visible"

    @pytest.mark.anyio
    async def test_same_type_chat_stream_passthrough_applies_think_filter(self):
        """同格式透传 + 流式 + capabilities.filter_think_content：
        DeepSeek Chat→Chat 流式响应中的 💭...💭 应被剥除。
        """

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"💭hidden💭 "}}]}'
                yield ""
                yield 'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"visible"}}]}'
                yield ""
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
            id="ch_deepseek",
            name="DeepSeek",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            models=["deepseek-chat"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = await _do_request(
                channel,
                {"model": "deepseek-chat", "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                is_stream=True,
            )
            outputs = "".join([chunk async for chunk in stream])

        # 💭 内部内容必须被剥除
        assert "hidden" not in outputs
        assert "💭" not in outputs
        # 正常文本必须保留
        assert "visible" in outputs

    @pytest.mark.anyio
    async def test_same_type_chat_non_sse_json_fallback_applies_think_filter(self):
        """同格式透传 + 流式请求但上游返回整块 JSON 兜底场景：
        Chat→Chat 拆成 chat.completion.chunk 序列时仍需剥除 💭...💭。
        """

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                # 上游对 stream=true 仍返回整块 JSON（无 data: 前缀），触发 non_sse_stream_body 兜底
                yield (
                    '{"id":"chatcmpl-x","object":"chat.completion","model":"deepseek-chat",'
                    '"choices":[{"index":0,"message":{"role":"assistant",'
                    '"content":"💭hidden💭 visible"},"finish_reason":"stop"}],'
                    '"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}'
                )

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
            id="ch_deepseek_fallback",
            name="DeepSeek",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            models=["deepseek-chat"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = await _do_request(
                channel,
                {"model": "deepseek-chat", "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                is_stream=True,
            )
            outputs = "".join([chunk async for chunk in stream])

        # 💭 内部内容必须被剥除
        assert "hidden" not in outputs
        assert "💭" not in outputs
        # 正常文本必须保留
        assert "visible" in outputs

    @pytest.mark.anyio
    async def test_same_type_response_stream_passthrough_applies_think_filter(self):
        """同格式透传 + Response→Response 流式 + filter_think_content：
        response.output_text.delta 中的 💭...💭 应被剥除。
        """

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: response.output_text.delta"
                yield 'data: {"type":"response.output_text.delta","delta":"💭thinking💭 "}'
                yield ""
                yield "event: response.output_text.delta"
                yield 'data: {"type":"response.output_text.delta","delta":"shown"}'
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

        channel = Channel(
            id="ch_deepseek_resp",
            name="DeepSeek-Responses",
            api_type=APIType.OPENAI_RESPONSE,
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            models=["deepseek-chat"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = await _do_request(
                channel,
                {"model": "deepseek-chat", "stream": True, "input": "hi"},
                APIType.OPENAI_RESPONSE,
                is_stream=True,
            )
            outputs = "".join([chunk async for chunk in stream])

        assert "thinking" not in outputs
        assert "💭" not in outputs
        assert "shown" in outputs

    @pytest.mark.anyio
    async def test_same_type_passthrough_applies_capability_filter_when_capabilities_set(
        self,
    ):
        """同格式透传也必须尊重 channel.capabilities 用户配置。"""
        captured = {}
        upstream_response = {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        class FakeClient:
            async def post(self, url, json, headers):
                captured["json"] = json
                request = httpx.Request("POST", url)
                return httpx.Response(200, json=upstream_response, request=request)

        channel = Channel(
            id="ch_chat",
            name="Chat",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://chat.example",
            api_key="sk-test",
            models=["gpt-4o"],
            capabilities={"supports_parallel_tool_calls": False},
        )
        request_data = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
            "parallel_tool_calls": True,
        }

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            response = await _do_request(
                channel, request_data, APIType.OPENAI_CHAT, is_stream=False
            )

        assert "parallel_tool_calls" not in captured["json"]
        assert response == upstream_response

    @pytest.mark.anyio
    async def test_client_hop_by_hop_headers_are_not_forwarded(self):
        captured = {}
        upstream_response = {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        class FakeClient:
            async def post(self, url, json, headers):
                captured["headers"] = headers
                request = httpx.Request("POST", url)
                return httpx.Response(200, json=upstream_response, request=request)

        channel = Channel(
            id="ch_chat",
            name="Chat",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4o"],
        )

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel,
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
                APIType.OPENAI_CHAT,
                is_stream=False,
                client_headers={
                    "Connection": "keep-alive",
                    "Keep-Alive": "timeout=5",
                    "Transfer-Encoding": "chunked",
                    "X-Request-Id": "req_123",
                },
            )

        lowered = {key.lower(): value for key, value in captured["headers"].items()}
        assert "connection" not in lowered
        assert "keep-alive" not in lowered
        assert "transfer-encoding" not in lowered
        assert lowered["x-request-id"] == "req_123"
        assert lowered["authorization"] == "Bearer sk-test"

    @pytest.mark.anyio
    async def test_non_stream_success_records_stats_and_request_log(self):
        upstream_response = {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

        class FakeClient:
            async def post(self, url, json, headers):
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json=upstream_response,
                    headers={"X-Upstream": "yes"},
                    request=request,
                )

        channel = Channel(
            id="ch_chat",
            name="Chat",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4o"],
        )

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request") as stats_record,
            patch("proxy_core.request_logs.record_request") as request_log_record,
        ):
            await _do_request(
                channel,
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
                APIType.OPENAI_CHAT,
                is_stream=False,
            )

        stats_record.assert_called_once()
        request_log_record.assert_called_once()
        assert request_log_record.call_args.kwargs == stats_record.call_args.kwargs
        assert request_log_record.call_args.kwargs["response_body"] == upstream_response

    @pytest.mark.anyio
    async def test_same_type_openai_response_non_stream_forwards_body_and_response_unchanged(
        self,
    ):
        captured = {}
        upstream_response = {
            "id": "resp_remote_2",
            "object": "response",
            "created_at": 123,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_resp_remote_2",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "output_text": "ok",
            "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        }

        class FakeClient:
            async def post(self, url, json, headers):
                captured["url"] = url
                captured["json"] = json
                request = httpx.Request("POST", url)
                return httpx.Response(200, json=upstream_response, request=request)

        channel = Channel(
            id="ch_response",
            name="Responses",
            api_type=APIType.OPENAI_RESPONSE,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4o"],
        )
        request_data = {
            "model": "gpt-4o",
            "input": "Continue",
            "previous_response_id": "resp_remote_1",
            "store": True,
        }

        with (
            patch("proxy_core._responses_store") as mock_store,
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            mock_store.get_conversation = AsyncMock(return_value=None)

            response = await _do_request(
                channel, request_data, APIType.OPENAI_RESPONSE, is_stream=False
            )

        assert captured["url"] == "https://api.openai.com/v1/responses"
        assert captured["json"] == request_data
        assert response == upstream_response
        mock_store.get_conversation.assert_not_awaited()

    @pytest.mark.anyio
    async def test_openai_response_previous_response_id_expands_history_for_chat_upstream(
        self,
    ):
        captured = {}
        upstream_response = {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "created": 123,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        class FakeClient:
            async def post(self, url, json, headers):
                captured["json"] = json
                request = httpx.Request("POST", url)
                return httpx.Response(200, json=upstream_response, request=request)

        channel = Channel(
            id="ch_chat",
            name="Chat",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4o"],
        )
        request_data = {
            "model": "gpt-4o",
            "input": "How are you?",
            "previous_response_id": "resp_1",
        }

        with (
            patch("proxy_core._responses_store") as mock_store,
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            mock_store.get_conversation = AsyncMock(
                return_value={
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi there"},
                    ],
                    "instructions": "Be terse.",
                }
            )

            await _do_request(
                channel, request_data, APIType.OPENAI_RESPONSE, is_stream=False
            )

        assert captured["json"]["messages"] == [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
        ]
        assert "previous_response_id" not in captured["json"]

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

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=BadRequestClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await _proxy_single_model_request(
                    model="gpt-4o",
                    request_data={"model": "gpt-4o", "messages": []},
                    target_api_type=APIType.OPENAI_CHAT,
                    is_stream=False,
                    query_string=None,
                    client_headers=None,
                    api_key_id=None,
                    client_ip=None,
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

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
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
    async def test_same_type_anthropic_stream_preserves_event_type_for_multiline_data(
        self,
    ):
        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: ping"
                yield 'data: {"type":"ping",'
                yield 'data: "extra":true}'
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

        channel = Channel(
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = _do_stream_request(
                channel=channel,
                url="https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "claude-3", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
            outputs = [chunk async for chunk in stream]

        joined = "".join(outputs)
        assert "event: ping" in joined
        assert '"extra": true' in joined
        for block in joined.strip().split("\n\n"):
            assert block.startswith("event: ")

    @pytest.mark.anyio
    async def test_anthropic_stream_mid_error_emits_message_stop(self):
        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: message_start"
                yield 'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","content":[],"model":"claude-3","usage":{"input_tokens":1,"output_tokens":0}}}'
                yield ""
                yield "event: content_block_start"
                yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
                yield ""
                yield "event: content_block_delta"
                yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}'
                yield ""
                yield "event: error"
                yield 'data: {"type":"error","error":{"type":"api_error","message":"midstream failure"}}'
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

        channel = Channel(
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
            patch("proxy_core.load_balancer.record_failure", new_callable=AsyncMock),
        ):
            stream = _do_stream_request(
                channel=channel,
                url="https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "claude-3", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
            outputs = [chunk async for chunk in stream]

        joined = "".join(outputs)
        assert "event: error" in joined
        assert "event: message_stop" in joined

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

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = await _do_request(
                channel, request_data, APIType.OPENAI_CHAT, is_stream=True
            )
            outputs = [chunk async for chunk in stream]

        assert outputs
        assert captured["json"] == request_data

    @pytest.mark.anyio
    async def test_openai_responses_sse_event_lines_are_preserved(self):
        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: response.created"
                yield 'data: {"type":"response.created","response":{"id":"resp_1","object":"response","status":"in_progress"}}'
                yield ""
                yield "event: response.output_text.delta"
                yield 'data: {"type":"response.output_text.delta","delta":"Hello"}'
                yield ""
                yield "event: response.completed"
                yield 'data: {"type":"response.completed","response":{"id":"resp_1","object":"response","status":"completed"}}'
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

        channel = Channel(
            id="ch_response",
            name="Responses",
            api_type=APIType.OPENAI_RESPONSE,
            base_url="https://api.openai.com",
            api_key="sk-test",
            models=["gpt-4o"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = _do_stream_request(
                channel=channel,
                url="https://api.openai.com/v1/responses",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "gpt-4o", "stream": True},
                response_converter=None,
                source_type="openai-response",
                target_api_type=APIType.OPENAI_RESPONSE,
            )
            outputs = [chunk async for chunk in stream]

        joined = "".join(outputs)
        assert "event: response.created" in joined
        assert "event: response.output_text.delta" in joined
        assert "event: response.completed" in joined
        assert '"delta": "Hello"' in joined

    @pytest.mark.anyio
    async def test_stream_retries_next_channel_when_first_channel_fails_before_output(
        self,
    ):
        class FailingStreamResponse:
            async def __aenter__(self):
                request = httpx.Request(
                    "POST", "https://primary.example/v1/chat/completions"
                )
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

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch("proxy_core.create_stream_client", side_effect=fake_stream_client),
            patch("proxy_core.stats.record_request"),
        ):
            stream, selected = await _proxy_single_model_request(
                model="gpt-4o",
                request_data={"model": "gpt-4o", "stream": True, "messages": []},
                target_api_type=APIType.OPENAI_CHAT,
                is_stream=True,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )
            outputs = [chunk async for chunk in stream]

        assert selected.id == "ch_fallback"
        assert "fallback" in "".join(outputs)


class TestAnthropicNonSseJsonFallbackEarly:
    """Anthropic 同类型流式请求收到非 SSE JSON 时，应输出完整的 Anthropic SSE 事件序列。"""

    @pytest.mark.anyio
    async def test_anthropic_non_sse_json_produces_event_lines(self):
        """同类型 Anthropic 直通，上游返回普通 JSON 而非 SSE，应拆分为
        message_start/content_block_start/.../message_stop 事件。"""

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            _consumed = False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                if self._consumed:
                    return
                self._consumed = True
                # 上游直接返回 JSON，没有 data: / event: 前缀
                yield '{"id":"msg_1","type":"message","role":"assistant","content":[{"type":"text","text":"hello"}],"model":"claude-3","stop_reason":"end_turn","usage":{"input_tokens":5,"output_tokens":1}}'

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
            models=["claude-3"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = _do_stream_request(
                channel=channel,
                url="https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "claude-3", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
            outputs = [chunk async for chunk in stream]

        joined = "".join(outputs)
        # 必须包含 Anthropic SSE 的 event: 行
        assert "event: message_start" in joined
        assert "event: content_block_start" in joined
        assert "event: content_block_delta" in joined
        assert "event: content_block_stop" in joined
        assert "event: message_delta" in joined
        assert "event: message_stop" in joined
        # 不应出现裸 data: 行（不带 event: 前缀的 Anthropic 响应）
        # 每个事件块应以 event: 开头
        for block in joined.strip().split("\n\n"):
            if block.strip():
                assert block.startswith("event: "), (
                    f"Unexpected SSE block without event line: {block[:80]}"
                )


class TestAnthropicSameTypeFailoverEarly:
    """Anthropic -> Anthropic 多上游故障转移测试。"""

    @pytest.mark.anyio
    async def test_anthropic_non_stream_weighted_failover(self):
        """Anthropic 同类型非流式：第一个渠道 5xx，故障转移到第二个 Anthropic 渠道。"""

        class FailingClient:
            async def post(self, url, json, headers):
                request = httpx.Request("POST", url)
                return httpx.Response(500, json={"error": "internal"}, request=request)

        class WorkingClient:
            async def post(self, url, json, headers):
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_ok",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "fallback_ok"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 3, "output_tokens": 2},
                    },
                    request=request,
                )

        primary = Channel(
            id="ch_anthropic_1",
            name="Anthropic Primary",
            api_type=APIType.ANTHROPIC,
            base_url="https://primary-anthropic.example",
            api_key="ak-1",
            models=["claude-3"],
            priority=1,
        )
        fallback = Channel(
            id="ch_anthropic_2",
            name="Anthropic Fallback",
            api_type=APIType.ANTHROPIC,
            base_url="https://fallback-anthropic.example",
            api_key="ak-2",
            models=["claude-3"],
            priority=2,
        )

        def fake_create_client(ch):
            return FailingClient() if ch.id == "ch_anthropic_1" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                side_effect=fake_create_client,
            ),
            patch("proxy_core.stats.record_request"),
        ):
            result, selected = await _proxy_single_model_request(
                model="claude-3",
                request_data={
                    "model": "claude-3",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                target_api_type=APIType.ANTHROPIC,
                is_stream=False,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )

        assert selected.id == "ch_anthropic_2"
        assert result["content"][0]["text"] == "fallback_ok"

    @pytest.mark.anyio
    async def test_anthropic_stream_preflight_failover(self):
        """Anthropic 同类型流式：首包前连接失败，故障转移到第二个 Anthropic 渠道。"""

        class FailingStreamResponse:
            async def __aenter__(self):
                request = httpx.Request(
                    "POST", "https://primary-anthropic.example/v1/messages"
                )
                raise httpx.ConnectError("connection refused", request=request)

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
                yield "event: message_start"
                yield 'data: {"type":"message_start","message":{"id":"msg_fb"}}'
                yield "event: content_block_start"
                yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
                yield "event: content_block_delta"
                yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}'
                yield "event: content_block_stop"
                yield 'data: {"type":"content_block_stop","index":0}'
                yield "event: message_delta"
                yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}'
                yield "event: message_stop"
                yield 'data: {"type":"message_stop"}'

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
            id="ch_anthropic_1",
            name="Anthropic Primary",
            api_type=APIType.ANTHROPIC,
            base_url="https://primary-anthropic.example",
            api_key="ak-1",
            models=["claude-3"],
            priority=1,
        )
        fallback = Channel(
            id="ch_anthropic_2",
            name="Anthropic Fallback",
            api_type=APIType.ANTHROPIC,
            base_url="https://fallback-anthropic.example",
            api_key="ak-2",
            models=["claude-3"],
            priority=2,
        )

        def fake_stream_client(ch):
            return FailingClient() if ch.id == "ch_anthropic_1" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch("proxy_core.create_stream_client", side_effect=fake_stream_client),
            patch("proxy_core.stats.record_request"),
        ):
            stream, selected = await _proxy_single_model_request(
                model="claude-3",
                request_data={
                    "model": "claude-3",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                target_api_type=APIType.ANTHROPIC,
                is_stream=True,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )
            outputs = [chunk async for chunk in stream]

        assert selected.id == "ch_anthropic_2"
        joined = "".join(outputs)
        assert "event: message_start" in joined
        assert "ok" in joined

    @pytest.mark.anyio
    async def test_anthropic_stream_error_event_before_output_fails_over(self):
        class ErrorStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: error"
                yield 'data: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}'
                yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class WorkingStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: message_start"
                yield 'data: {"type":"message_start","message":{"id":"msg_fb","type":"message","role":"assistant","content":[],"model":"claude-3","usage":{"input_tokens":1,"output_tokens":0}}}'
                yield ""
                yield "event: content_block_start"
                yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
                yield ""
                yield "event: content_block_delta"
                yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}'
                yield ""
                yield "event: content_block_stop"
                yield 'data: {"type":"content_block_stop","index":0}'
                yield ""
                yield "event: message_delta"
                yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}'
                yield ""
                yield "event: message_stop"
                yield 'data: {"type":"message_stop"}'
                yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class ErrorClient:
            def stream(self, *args, **kwargs):
                return ErrorStreamResponse()

            async def aclose(self):
                return None

        class WorkingClient:
            def stream(self, *args, **kwargs):
                return WorkingStreamResponse()

            async def aclose(self):
                return None

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.ANTHROPIC,
            base_url="https://primary.example",
            api_key="ak-primary",
            models=["claude-3"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.ANTHROPIC,
            base_url="https://fallback.example",
            api_key="ak-fallback",
            models=["claude-3"],
            priority=2,
        )

        def fake_stream_client(ch):
            return ErrorClient() if ch.id == "ch_primary" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch("proxy_core.create_stream_client", side_effect=fake_stream_client),
            patch("proxy_core.stats.record_request"),
        ):
            stream, selected = await _proxy_single_model_request(
                model="claude-3",
                request_data={
                    "model": "claude-3",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                target_api_type=APIType.ANTHROPIC,
                is_stream=True,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )
            outputs = [chunk async for chunk in stream]

        assert selected.id == "ch_fallback"
        assert "ok" in "".join(outputs)


class TestAnthropicHeaderPriority:
    """渠道级 anthropic-version / anthropic-beta 不应被客户端请求头覆盖。"""

    @pytest.mark.anyio
    async def test_client_headers_do_not_override_anthropic_channel_config(self):
        """客户端发送 anthropic-version 和 anthropic-beta 时，渠道配置应优先。"""
        captured_headers = {}

        class FakeClient:
            async def post(self, url, json, headers):
                captured_headers.update(headers)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
            anthropic_beta="max-tokens-3-5-sonnet-2024-07-15",
        )

        client_headers = {
            "anthropic-version": "2024-01-01",  # 客户端试图用不同版本
            "anthropic-beta": "client-beta-feature",  # 客户端试图覆盖 beta
            "x-custom": "should-pass",  # 自定义头应透传
        }

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel,
                {"model": "claude-3", "messages": []},
                APIType.ANTHROPIC,
                is_stream=False,
                client_headers=client_headers,
            )

        # 默认 channel 策略下，未配置时回退为默认版本
        assert captured_headers["anthropic-version"] == "2023-06-01"
        # 渠道配置的 anthropic_beta 应保留
        assert captured_headers["anthropic-beta"] == "max-tokens-3-5-sonnet-2024-07-15"
        # 客户端自定义头应透传
        assert captured_headers["x-custom"] == "should-pass"

    @pytest.mark.anyio
    async def test_client_headers_can_override_when_channel_policy_allows(self):
        captured_headers = {}

        class FakeClient:
            async def post(self, url, json, headers):
                captured_headers.update(headers)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
            anthropic_version="2024-10-22",
            anthropic_version_policy="client",
            anthropic_beta="prompt-caching-2024-07-31",
            anthropic_beta_policy="merge",
        )

        client_headers = {
            "anthropic-version": "2025-01-01",
            "anthropic-beta": "prompt-caching-2024-07-31,search-results-2025-01-15",
        }

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel,
                {"model": "claude-3", "messages": []},
                APIType.ANTHROPIC,
                is_stream=False,
                client_headers=client_headers,
            )

        assert captured_headers["anthropic-version"] == "2025-01-01"
        assert captured_headers["anthropic-beta"] == (
            "prompt-caching-2024-07-31,search-results-2025-01-15"
        )

    @pytest.mark.anyio
    async def test_emits_response_completed_before_done_when_finish_reason_missing(
        self,
    ):
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

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
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

    @pytest.mark.anyio
    async def test_openai_stream_with_null_tool_calls_still_records_request(self):
        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"mimo-v2.5-pro","choices":[{"index":0,"delta":{"role":"assistant","content":"","tool_calls":null},"finish_reason":null}]}'
                yield 'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"mimo-v2.5-pro","choices":[{"index":0,"delta":{"content":"Hello","tool_calls":null},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}'
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
            id="ch_mimo",
            name="XiaoMi-TokenPlan",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://token-plan-cn.xiaomimimo.com",
            api_key="sk-test",
            models=["mimo-v2.5-pro"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
            patch("proxy_core.request_logs.record_request") as request_log_record,
        ):
            stream = _do_stream_request(
                channel=channel,
                url="https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "mimo-v2.5-pro", "stream": True},
                response_converter=None,
                source_type="openai-chat-completions",
                target_api_type=APIType.OPENAI_CHAT,
            )
            outputs = [chunk async for chunk in stream]

        assert outputs[-1] == "data: [DONE]\n\n"
        request_log_record.assert_called_once()
        assert request_log_record.call_args.kwargs["model"] == "mimo-v2.5-pro"
        assert request_log_record.call_args.kwargs["response_body"]["choices"][0]["message"]["content"] == "Hello"


class TestFailoverOn401:
    """401/403/404 属于渠道配置错误，应尝试下一个候选渠道。"""

    @pytest.mark.anyio
    async def test_401_fails_over_to_next_channel(self):
        calls = []

        class UnauthorizedClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    401,
                    json={"error": {"message": "invalid api key"}},
                    request=request,
                )

        class WorkingClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl_1",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                    request=request,
                )

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://primary.example",
            api_key="sk-bad",
            models=["gpt-4o"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://fallback.example",
            api_key="sk-good",
            models=["gpt-4o"],
            priority=2,
        )

        def fake_create_client(ch):
            return UnauthorizedClient() if ch.id == "ch_primary" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                side_effect=fake_create_client,
            ),
            patch("proxy_core.stats.record_request"),
            patch("proxy_core.load_balancer.record_failure", new_callable=AsyncMock) as record_failure,
        ):
            response, channel = await _proxy_single_model_request(
                model="gpt-4o",
                request_data={"model": "gpt-4o", "messages": []},
                target_api_type=APIType.OPENAI_CHAT,
                is_stream=False,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )

        assert response["choices"][0]["message"]["content"] == "ok"
        assert channel.id == "ch_fallback"
        assert len(calls) == 2
        record_failure.assert_awaited_once()

    @pytest.mark.anyio
    async def test_403_fails_over_to_next_channel(self):
        calls = []

        class ForbiddenClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    403,
                    json={"error": {"message": "forbidden"}},
                    request=request,
                )

        class WorkingClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl_1",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                    request=request,
                )

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://primary.example",
            api_key="sk-bad",
            models=["gpt-4o"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://fallback.example",
            api_key="sk-good",
            models=["gpt-4o"],
            priority=2,
        )

        def fake_create_client(ch):
            return ForbiddenClient() if ch.id == "ch_primary" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                side_effect=fake_create_client,
            ),
            patch("proxy_core.stats.record_request"),
            patch("proxy_core.load_balancer.record_failure", new_callable=AsyncMock) as record_failure,
        ):
            response, channel = await _proxy_single_model_request(
                model="gpt-4o",
                request_data={"model": "gpt-4o", "messages": []},
                target_api_type=APIType.OPENAI_CHAT,
                is_stream=False,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )

        assert response["choices"][0]["message"]["content"] == "ok"
        assert channel.id == "ch_fallback"
        assert len(calls) == 2
        record_failure.assert_awaited_once()

    @pytest.mark.anyio
    async def test_404_fails_over_to_next_channel(self):
        calls = []

        class NotFoundClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    404,
                    json={"error": {"message": "not found"}},
                    request=request,
                )

        class WorkingClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl_1",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                    request=request,
                )

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://primary.example",
            api_key="sk-bad",
            models=["gpt-4o"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://fallback.example",
            api_key="sk-good",
            models=["gpt-4o"],
            priority=2,
        )

        def fake_create_client(ch):
            return NotFoundClient() if ch.id == "ch_primary" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                side_effect=fake_create_client,
            ),
            patch("proxy_core.stats.record_request"),
        ):
            result, selected = await _proxy_single_model_request(
                model="gpt-4o",
                request_data={"model": "gpt-4o", "messages": []},
                target_api_type=APIType.OPENAI_CHAT,
                is_stream=False,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )

        assert selected.id == "ch_fallback"
        assert len(calls) == 2

    @pytest.mark.anyio
    async def test_all_channels_401_raises_last_error(self):
        """所有渠道都返回 401 时，应抛出最后一个错误。"""

        class UnauthorizedClient:
            async def post(self, url, json, headers):
                request = httpx.Request("POST", url)
                return httpx.Response(
                    401, json={"error": {"message": "invalid"}}, request=request
                )

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://primary.example",
            api_key="sk-bad",
            models=["gpt-4o"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://fallback.example",
            api_key="sk-bad2",
            models=["gpt-4o"],
            priority=2,
        )

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=UnauthorizedClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await _proxy_single_model_request(
                    model="gpt-4o",
                    request_data={"model": "gpt-4o", "messages": []},
                    target_api_type=APIType.OPENAI_CHAT,
                    is_stream=False,
                    query_string=None,
                    client_headers=None,
                    api_key_id=None,
                    client_ip=None,
                )

        assert exc_info.value.response.status_code == 401


class TestConverterErrorFailover:
    """转换异常应允许故障转移到其他渠道。"""

    @pytest.mark.anyio
    async def test_response_conversion_error_fails_over(self):
        """非流式响应转换失败时，应故障转移到其他渠道。"""
        calls = []

        class BrokenResponseClient:
            """返回一个结构异常的响应，导致 converter 抛错。"""

            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={"content": [None]},
                    request=request,
                )

        class WorkingClient:
            async def post(self, url, json, headers):
                calls.append(url)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl_1",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                    request=request,
                )

        # anthropic 上游 → openai-chat-completions 客户端，需要转换
        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.ANTHROPIC,
            base_url="https://primary.example",
            api_key="ak-test",
            models=["claude-3"],
            priority=1,
        )
        # 同类型，直通
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://fallback.example",
            api_key="sk-good",
            models=["claude-3"],
            priority=2,
        )

        def fake_create_client(ch):
            return BrokenResponseClient() if ch.id == "ch_primary" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                side_effect=fake_create_client,
            ),
            patch("proxy_core.stats.record_request"),
        ):
            result, selected = await _proxy_single_model_request(
                model="claude-3",
                request_data={
                    "model": "claude-3",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                target_api_type=APIType.OPENAI_CHAT,
                is_stream=False,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )

        assert selected.id == "ch_fallback"
        assert result["choices"][0]["message"]["content"] == "ok"


class TestResponsesToChatCompletionsFlow:
    """客户端 Responses 请求经 Chat Completions 上游完成转换。"""

    @pytest.mark.anyio
    async def test_non_stream_responses_request_converts_to_chat_upstream_and_back(
        self,
    ):
        captured = {}

        class ChatClient:
            async def post(self, url, json, headers):
                captured["url"] = url
                captured["json"] = json
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl_1",
                        "object": "chat.completion",
                        "created": 123,
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "Hello"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_chat",
            name="Chat",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://chat.example",
            api_key="sk-test",
            models=["gpt-4o"],
        )

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=ChatClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            result = await _do_request(
                channel=channel,
                request_data={
                    "model": "gpt-4o",
                    "instructions": "Be concise.",
                    "input": "Hello",
                    "tools": [
                        {
                            "type": "function",
                            "name": "search",
                            "parameters": {"type": "object"},
                            "strict": True,
                        }
                    ],
                    "tool_choice": {"type": "function", "name": "search"},
                    "max_output_tokens": 100,
                },
                target_api_type=APIType.OPENAI_RESPONSE,
                is_stream=False,
            )

        assert captured["url"] == "https://chat.example/v1/chat/completions"
        assert captured["json"] == {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hello"},
            ],
            "stream": False,
            "max_tokens": 100,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "",
                        "parameters": {"type": "object"},
                        "strict": True,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "search"}},
        }
        assert result["object"] == "response"
        assert result["id"].startswith("resp_")
        assert result["_upstream_id"] == "chatcmpl_1"
        assert result["output_text"] == "Hello"
        assert result["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }

    @pytest.mark.anyio
    async def test_responses_to_chat_applies_capability_filter_after_conversion(self):
        captured = {}

        class ChatClient:
            async def post(self, url, json, headers):
                captured["json"] = json
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl_1",
                        "object": "chat.completion",
                        "created": 123,
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_chat",
            name="Chat",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://chat.example",
            api_key="sk-test",
            models=["gpt-4o"],
            capabilities={"supports_parallel_tool_calls": False},
        )

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=ChatClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel=channel,
                request_data={
                    "model": "gpt-4o",
                    "input": "Hello",
                    "parallel_tool_calls": True,
                },
                target_api_type=APIType.OPENAI_RESPONSE,
                is_stream=False,
            )

        assert "parallel_tool_calls" not in captured["json"]

    @pytest.mark.anyio
    async def test_responses_to_chat_merges_system_messages_after_conversion(self):
        captured = {}

        class ChatClient:
            async def post(self, url, json, headers):
                captured["json"] = json
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl_1",
                        "object": "chat.completion",
                        "created": 123,
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_minimax",
            name="MiniMax",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.minimax.chat",
            api_key="sk-test",
            models=["gpt-4o"],
        )

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=ChatClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel=channel,
                request_data={
                    "model": "gpt-4o",
                    "instructions": "Rule 1.",
                    "input": [
                        {"role": "developer", "content": "Rule 2."},
                        {"role": "user", "content": "Hello"},
                    ],
                },
                target_api_type=APIType.OPENAI_RESPONSE,
                is_stream=False,
            )

        assert captured["json"]["messages"] == [
            {"role": "system", "content": "Rule 1.\n\nRule 2."},
            {"role": "user", "content": "Hello"},
        ]


class TestEmptyStreamFailover:
    """流式空响应应触发故障转移。"""

    @pytest.mark.anyio
    async def test_empty_stream_triggers_failover(self):
        """上游流式连接成功但无任何 SSE 输出时，应故障转移。"""

        class EmptyStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                # 不产出任何行
                return
                yield  # 使其成为 async generator

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

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

        class EmptyClient:
            def stream(self, *args, **kwargs):
                return EmptyStreamResponse()

            async def aclose(self):
                return None

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

        def fake_stream_client(ch):
            return EmptyClient() if ch.id == "ch_primary" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch("proxy_core.create_stream_client", side_effect=fake_stream_client),
            patch("proxy_core.stats.record_request"),
        ):
            stream, selected = await _proxy_single_model_request(
                model="gpt-4o",
                request_data={"model": "gpt-4o", "stream": True, "messages": []},
                target_api_type=APIType.OPENAI_CHAT,
                is_stream=True,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )
            outputs = [chunk async for chunk in stream]

        assert selected.id == "ch_fallback"
        assert "fallback" in "".join(outputs)


class TestAnthropicNonSseJsonFallback:
    """Anthropic 同类型流式请求收到非 SSE JSON 时，应输出完整的 Anthropic SSE 事件序列。"""

    @pytest.mark.anyio
    async def test_anthropic_non_sse_json_produces_event_lines(self):
        """同类型 Anthropic 直通，上游返回普通 JSON 而非 SSE，应拆分为
        message_start/content_block_start/.../message_stop 事件。"""

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "application/json"}
            _consumed = False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                if self._consumed:
                    return
                self._consumed = True
                # 上游直接返回 JSON，没有 data: / event: 前缀
                yield '{"id":"msg_1","type":"message","role":"assistant","content":[{"type":"text","text":"hello"}],"model":"claude-3","stop_reason":"end_turn","usage":{"input_tokens":5,"output_tokens":1}}'

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
            models=["claude-3"],
        )

        with (
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = _do_stream_request(
                channel=channel,
                url="https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                upstream_data={"model": "claude-3", "stream": True},
                response_converter=None,
                source_type="anthropic",
                target_api_type=APIType.ANTHROPIC,
            )
            outputs = [chunk async for chunk in stream]

        joined = "".join(outputs)
        # 必须包含 Anthropic SSE 的 event: 行
        assert "event: message_start" in joined
        assert "event: content_block_start" in joined
        assert "event: content_block_delta" in joined
        assert "event: content_block_stop" in joined
        assert "event: message_delta" in joined
        assert "event: message_stop" in joined
        # 不应出现裸 data: 行（不带 event: 前缀的 Anthropic 响应）
        # 每个事件块应以 event: 开头
        for block in joined.strip().split("\n\n"):
            if block.strip():
                assert block.startswith("event: "), (
                    f"Unexpected SSE block without event line: {block[:80]}"
                )


class TestAnthropicSameTypeFailover:
    """Anthropic -> Anthropic 多上游故障转移测试。"""

    @pytest.mark.anyio
    async def test_anthropic_non_stream_weighted_failover(self):
        """Anthropic 同类型非流式：第一个渠道 5xx，故障转移到第二个 Anthropic 渠道。"""

        class FailingClient:
            async def post(self, url, json, headers):
                request = httpx.Request("POST", url)
                return httpx.Response(500, json={"error": "internal"}, request=request)

        class WorkingClient:
            async def post(self, url, json, headers):
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_ok",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "fallback_ok"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 3, "output_tokens": 2},
                    },
                    request=request,
                )

        primary = Channel(
            id="ch_anthropic_1",
            name="Anthropic Primary",
            api_type=APIType.ANTHROPIC,
            base_url="https://primary-anthropic.example",
            api_key="ak-1",
            models=["claude-3"],
            priority=1,
        )
        fallback = Channel(
            id="ch_anthropic_2",
            name="Anthropic Fallback",
            api_type=APIType.ANTHROPIC,
            base_url="https://fallback-anthropic.example",
            api_key="ak-2",
            models=["claude-3"],
            priority=2,
        )

        def fake_create_client(ch):
            return FailingClient() if ch.id == "ch_anthropic_1" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                side_effect=fake_create_client,
            ),
            patch("proxy_core.stats.record_request"),
        ):
            result, selected = await _proxy_single_model_request(
                model="claude-3",
                request_data={
                    "model": "claude-3",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                target_api_type=APIType.ANTHROPIC,
                is_stream=False,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )

        assert selected.id == "ch_anthropic_2"
        assert result["content"][0]["text"] == "fallback_ok"

    @pytest.mark.anyio
    async def test_anthropic_stream_preflight_failover(self):
        """Anthropic 同类型流式：首包前连接失败，故障转移到第二个 Anthropic 渠道。"""

        class FailingStreamResponse:
            async def __aenter__(self):
                request = httpx.Request(
                    "POST", "https://primary-anthropic.example/v1/messages"
                )
                raise httpx.ConnectError("connection refused", request=request)

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
                yield "event: message_start"
                yield 'data: {"type":"message_start","message":{"id":"msg_fb"}}'
                yield "event: content_block_start"
                yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
                yield "event: content_block_delta"
                yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}'
                yield "event: content_block_stop"
                yield 'data: {"type":"content_block_stop","index":0}'
                yield "event: message_delta"
                yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}'
                yield "event: message_stop"
                yield 'data: {"type":"message_stop"}'

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
            id="ch_anthropic_1",
            name="Anthropic Primary",
            api_type=APIType.ANTHROPIC,
            base_url="https://primary-anthropic.example",
            api_key="ak-1",
            models=["claude-3"],
            priority=1,
        )
        fallback = Channel(
            id="ch_anthropic_2",
            name="Anthropic Fallback",
            api_type=APIType.ANTHROPIC,
            base_url="https://fallback-anthropic.example",
            api_key="ak-2",
            models=["claude-3"],
            priority=2,
        )

        def fake_stream_client(ch):
            return FailingClient() if ch.id == "ch_anthropic_1" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch("proxy_core.create_stream_client", side_effect=fake_stream_client),
            patch("proxy_core.stats.record_request"),
        ):
            stream, selected = await _proxy_single_model_request(
                model="claude-3",
                request_data={
                    "model": "claude-3",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                target_api_type=APIType.ANTHROPIC,
                is_stream=True,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )
            outputs = [chunk async for chunk in stream]

        assert selected.id == "ch_anthropic_2"
        joined = "".join(outputs)
        assert "event: message_start" in joined
        assert "ok" in joined

    @pytest.mark.anyio
    async def test_anthropic_stream_error_event_before_output_fails_over(self):
        class ErrorStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: error"
                yield 'data: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}'
                yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class WorkingStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "event: message_start"
                yield 'data: {"type":"message_start","message":{"id":"msg_fb","type":"message","role":"assistant","content":[],"model":"claude-3","usage":{"input_tokens":1,"output_tokens":0}}}'
                yield ""
                yield "event: content_block_start"
                yield 'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'
                yield ""
                yield "event: content_block_delta"
                yield 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}'
                yield ""
                yield "event: content_block_stop"
                yield 'data: {"type":"content_block_stop","index":0}'
                yield ""
                yield "event: message_delta"
                yield 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}'
                yield ""
                yield "event: message_stop"
                yield 'data: {"type":"message_stop"}'
                yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class ErrorClient:
            def stream(self, *args, **kwargs):
                return ErrorStreamResponse()

            async def aclose(self):
                return None

        class WorkingClient:
            def stream(self, *args, **kwargs):
                return WorkingStreamResponse()

            async def aclose(self):
                return None

        primary = Channel(
            id="ch_primary",
            name="Primary",
            api_type=APIType.ANTHROPIC,
            base_url="https://primary.example",
            api_key="ak-primary",
            models=["claude-3"],
            priority=1,
        )
        fallback = Channel(
            id="ch_fallback",
            name="Fallback",
            api_type=APIType.ANTHROPIC,
            base_url="https://fallback.example",
            api_key="ak-fallback",
            models=["claude-3"],
            priority=2,
        )

        def fake_stream_client(ch):
            return ErrorClient() if ch.id == "ch_primary" else WorkingClient()

        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[primary, fallback],
            ),
            patch("proxy_core.create_stream_client", side_effect=fake_stream_client),
            patch("proxy_core.stats.record_request"),
        ):
            stream, selected = await _proxy_single_model_request(
                model="claude-3",
                request_data={
                    "model": "claude-3",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                target_api_type=APIType.ANTHROPIC,
                is_stream=True,
                query_string=None,
                client_headers=None,
                api_key_id=None,
                client_ip=None,
            )
            outputs = [chunk async for chunk in stream]

        assert selected.id == "ch_fallback"
        assert "ok" in "".join(outputs)


class TestAnthropicHeaderPriorityEarly:
    """渠道级 anthropic-version / anthropic-beta 不应被客户端请求头覆盖。"""

    @pytest.mark.anyio
    async def test_client_headers_do_not_override_anthropic_channel_config(self):
        """客户端发送 anthropic-version 和 anthropic-beta 时，渠道配置应优先。"""
        captured_headers = {}

        class FakeClient:
            async def post(self, url, json, headers):
                captured_headers.update(headers)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
            anthropic_beta="max-tokens-3-5-sonnet-2024-07-15",
        )

        client_headers = {
            "anthropic-version": "2024-01-01",  # 客户端试图用不同版本
            "anthropic-beta": "client-beta-feature",  # 客户端试图覆盖 beta
            "x-custom": "should-pass",  # 自定义头应透传
        }

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel,
                {"model": "claude-3", "messages": []},
                APIType.ANTHROPIC,
                is_stream=False,
                client_headers=client_headers,
            )

        # 默认 channel 策略下，未配置渠道级 version 时回退到默认版本
        assert captured_headers["anthropic-version"] == "2023-06-01"
        # 渠道配置的 anthropic_beta 应保留
        assert captured_headers["anthropic-beta"] == "max-tokens-3-5-sonnet-2024-07-15"
        # 客户端自定义头应透传
        assert captured_headers["x-custom"] == "should-pass"

    @pytest.mark.anyio
    async def test_same_type_anthropic_uses_channel_policy_when_channel_not_configured(
        self,
    ):
        captured_headers = {}

        class FakeClient:
            async def post(self, url, json, headers):
                captured_headers.update(headers)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-channel",
            models=["claude-3"],
        )

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel,
                {"model": "claude-3", "messages": []},
                APIType.ANTHROPIC,
                is_stream=False,
                client_headers={
                    "x-api-key": "client-proxy-key",
                    "authorization": "Bearer client-proxy-key",
                    "anthropic-version": "2024-01-01",
                    "anthropic-beta": "client-beta",
                },
            )

        assert captured_headers["x-api-key"] == "ak-channel"
        assert "authorization" not in {
            k.lower(): v for k, v in captured_headers.items()
        }
        assert captured_headers["anthropic-version"] == "2023-06-01"
        assert "anthropic-beta" not in captured_headers

    @pytest.mark.anyio
    async def test_anthropic_channel_version_overrides_client_version(self):
        captured_headers = {}

        class FakeClient:
            async def post(self, url, json, headers):
                captured_headers.update(headers)
                request = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    json={
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "model": "claude-3",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                    request=request,
                )

        channel = Channel(
            id="ch_1",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
            anthropic_version="2023-06-01",
        )

        with (
            patch(
                "proxy_core.create_client",
                new_callable=AsyncMock,
                return_value=FakeClient(),
            ),
            patch("proxy_core.stats.record_request"),
        ):
            await _do_request(
                channel,
                {"model": "claude-3", "messages": []},
                APIType.ANTHROPIC,
                is_stream=False,
                client_headers={"anthropic-version": "2024-01-01"},
            )

        assert captured_headers["anthropic-version"] == "2023-06-01"


class TestBuildOpenaiStreamResponsePreservesTokenDetails:
    def test_preserves_upstream_total_tokens_and_details(self):
        """_build_openai_stream_response 应该使用上游的 total_tokens，而不是自加；
        同时应该透传 prompt_tokens_details 和 completion_tokens_details。"""
        chunks = [
            {
                "id": "chatcmpl-x",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}
                ],
            },
            {
                "id": "chatcmpl-x",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "prompt_tokens_details": {"cached_tokens": 900},
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            },
        ]
        result = _build_openai_stream_response(chunks, "gpt-4o")
        assert result is not None
        assert result["usage"]["prompt_tokens"] == 1000
        assert result["usage"]["completion_tokens"] == 50
        assert result["usage"]["total_tokens"] == 1050  # 使用上游的 total，不是自加
        assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 900
        assert result["usage"]["completion_tokens_details"]["reasoning_tokens"] == 30

    def test_falls_back_to_sum_when_upstream_total_missing(self):
        """当上游没有提供 total_tokens 时，应回退到 prompt + completion 的和。"""
        chunks = [
            {
                "id": "chatcmpl-x",
                "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    # 注意：没有 total_tokens
                    "prompt_tokens_details": {"cached_tokens": 50},
                },
            },
        ]
        result = _build_openai_stream_response(chunks, "gpt-4o")
        assert result is not None
        assert result["usage"]["total_tokens"] == 120  # 100 + 20
        assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 50

    def test_handles_missing_details_gracefully(self):
        """当上游没有 prompt_tokens_details/completion_tokens_details 时，不应包含这些字段。"""
        chunks = [
            {
                "id": "chatcmpl-x",
                "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 10,
                    "total_tokens": 60,
                },
            },
        ]
        result = _build_openai_stream_response(chunks, "gpt-4o")
        assert result is not None
        assert result["usage"]["prompt_tokens"] == 50
        assert result["usage"]["completion_tokens"] == 10
        assert result["usage"]["total_tokens"] == 60
        # 不应该有 details 字段
        assert "prompt_tokens_details" not in result["usage"]
        assert "completion_tokens_details" not in result["usage"]


class TestChatConverterSetStreamIncludeUsage:
    def test_set_stream_include_usage_sets_flag(self):
        """ToChatCompletionsConverter.set_stream_include_usage 应正确设置内部标志。"""
        from converters.to_chat import ToChatCompletionsConverter

        conv = ToChatCompletionsConverter()
        assert conv._stream_include_usage is False
        conv.set_stream_include_usage(True)
        assert conv._stream_include_usage is True
        conv.set_stream_include_usage(False)
        assert conv._stream_include_usage is False


class TestDoRequestSetsIncludeUsage:
    @pytest.mark.anyio
    async def test_do_request_sets_include_usage_on_chat_converter(self):
        """请求体携带 stream_options.include_usage=true 时，response_converter 应被设置该 flag。"""
        captured = {}

        real_setter = ToChatCompletionsConverter.set_stream_include_usage

        def spy(self, flag):
            captured["flag"] = flag
            return real_setter(self, flag)

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"claude-3","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}'
                yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeClient:
            def stream(self, method, url, json, headers):
                return FakeStreamResponse()

            async def aclose(self):
                return None

        channel = Channel(
            id="ch_anthropic",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
        )
        request_data = {
            "model": "claude-3",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
            "stream_options": {"include_usage": True},
        }

        with (
            patch(
                "proxy_core.ToChatCompletionsConverter.set_stream_include_usage",
                spy,
            ),
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = await _do_request(
                channel, request_data, APIType.OPENAI_CHAT, is_stream=True
            )
            # 消费流以触发执行
            _ = [chunk async for chunk in stream]

        assert captured["flag"] is True

    @pytest.mark.anyio
    async def test_do_request_does_not_set_include_usage_when_false(self):
        """请求体不携带 stream_options.include_usage 时，不应调用 setter。"""
        captured = {}

        real_setter = ToChatCompletionsConverter.set_stream_include_usage

        def spy(self, flag):
            captured["called"] = True
            captured["flag"] = flag
            return real_setter(self, flag)

        class FakeStreamResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"claude-3","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}'
                yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeClient:
            def stream(self, method, url, json, headers):
                return FakeStreamResponse()

            async def aclose(self):
                return None

        channel = Channel(
            id="ch_anthropic",
            name="Anthropic",
            api_type=APIType.ANTHROPIC,
            base_url="https://api.anthropic.com",
            api_key="ak-test",
            models=["claude-3"],
        )
        request_data = {
            "model": "claude-3",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
            # 没有 stream_options
        }

        with (
            patch(
                "proxy_core.ToChatCompletionsConverter.set_stream_include_usage",
                spy,
            ),
            patch("proxy_core.create_stream_client", return_value=FakeClient()),
            patch("proxy_core.stats.record_request"),
        ):
            stream = await _do_request(
                channel, request_data, APIType.OPENAI_CHAT, is_stream=True
            )
            _ = [chunk async for chunk in stream]

        assert captured.get("called") is True
        assert captured["flag"] is False
