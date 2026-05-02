from unittest.mock import patch

import pytest

from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from proxy_core import (
    _get_channels_for_model,
    _get_converter_and_upstream_type,
    _get_upstream_url,
    _invalidate_model_channels_cache,
    _yield_anthropic_event,
    _yield_anthropic_events,
    CONVERTER_MAP,
)


@pytest.fixture(autouse=True)
def reset_model_cache():
    """每个测试前清理模型渠道缓存。"""
    _invalidate_model_channels_cache()
    yield


class TestGetChannelsForModel:
    def test_filters_by_model_and_enabled(self):
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
        with patch("proxy_core.load_data", return_value=mock_data):
            channels = _get_channels_for_model("gpt-4")
            assert len(channels) == 2
            assert {ch.id for ch in channels} == {"ch_1", "ch_2"}

    def test_returns_empty_when_no_match(self):
        with patch("proxy_core.load_data", return_value={"channels": []}):
            channels = _get_channels_for_model("gpt-4")
            assert channels == []

    def test_excludes_disabled_channels(self):
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
        with patch("proxy_core.load_data", return_value=mock_data):
            channels = _get_channels_for_model("gpt-4")
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
