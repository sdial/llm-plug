"""测试 capability_manager 模块"""

from capability_manager import (
    ProviderCapabilities,
    infer_capabilities,
    apply_capability_filter,
    merge_system_messages,
)
from models.channel import Channel
from models.api_types import APIType


class TestInferCapabilities:
    """测试 infer_capabilities 函数"""

    def test_default_capabilities(self):
        """默认渠道应返回全部支持的能力"""
        channel = Channel(
            name="test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="test-key",
        )
        caps = infer_capabilities(channel)
        assert caps.supports_parallel_tool_calls is True
        assert caps.supports_tool_choice_auto is True
        assert caps.supports_response_format is True
        assert caps.supports_reasoning_effort is True
        assert caps.supports_file_content is False
        assert caps.supports_audio_content is False
        assert caps.supports_tool_choice_required is True
        assert caps.supports_strict_tools is True
        assert caps.requires_single_system_message is False
        assert caps.filter_think_content is False

    def test_deepseek_capabilities(self):
        """DeepSeek 渠道应返回特定的能力限制"""
        channel = Channel(
            name="deepseek",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
        )
        caps = infer_capabilities(channel)
        assert caps.supports_parallel_tool_calls is False
        assert caps.filter_think_content is True

    def test_minimax_capabilities(self):
        """MiniMax 渠道应返回需要单条 system 消息"""
        channel = Channel(
            name="minimax",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.minimax.chat/v1",
            api_key="test-key",
        )
        caps = infer_capabilities(channel)
        assert caps.requires_single_system_message is True

    def test_deepseek_case_insensitive(self):
        """base_url 判断应不区分大小写"""
        channel = Channel(
            name="DeepSeek-Upper",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://API.DEEPSEEK.COM/v1",
            api_key="test-key",
        )
        caps = infer_capabilities(channel)
        assert caps.supports_parallel_tool_calls is False
        assert caps.filter_think_content is True

    def test_channel_capabilities_override_inferred_defaults(self):
        """渠道显式能力配置应覆盖 base_url 推断。"""
        channel = Channel(
            name="deepseek",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            capabilities={
                "supports_parallel_tool_calls": True,
                "filter_think_content": False,
                "supports_response_format": False,
                "supports_reasoning_effort": False,
                "supports_file_content": True,
                "supports_audio_content": True,
                "supports_tool_choice_required": False,
                "supports_strict_tools": False,
            },
        )
        caps = infer_capabilities(channel)
        assert caps.supports_parallel_tool_calls is True
        assert caps.filter_think_content is False
        assert caps.supports_response_format is False
        assert caps.supports_reasoning_effort is False
        assert caps.supports_file_content is True
        assert caps.supports_audio_content is True
        assert caps.supports_tool_choice_required is False
        assert caps.supports_strict_tools is False


class TestApplyCapabilityFilter:
    """测试 apply_capability_filter 函数"""

    def test_no_filter_needed(self):
        """当能力匹配时，不应修改请求"""
        caps = ProviderCapabilities(
            supports_parallel_tool_calls=True,
            supports_tool_choice_auto=True,
        )
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
        }
        result = apply_capability_filter(request, caps)
        assert result == request

    def test_filter_parallel_tool_calls(self):
        """当不支持 parallel_tool_calls 时，应移除该参数"""
        caps = ProviderCapabilities(
            supports_parallel_tool_calls=False,
        )
        request = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "hello"}],
            "parallel_tool_calls": True,
        }
        result = apply_capability_filter(request, caps)
        assert "parallel_tool_calls" not in result

    def test_filter_tool_choice_auto(self):
        """当不支持 tool_choice=auto 时，应移除字段（让上游使用默认 auto-like 行为），而非反转为 none"""
        caps = ProviderCapabilities(
            supports_tool_choice_auto=False,
        )
        request = {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tool_choice": "auto",
        }
        result = apply_capability_filter(request, caps)
        assert "tool_choice" not in result

    def test_no_modification_when_false(self):
        """当参数值为 False 时，不应移除"""
        caps = ProviderCapabilities(
            supports_parallel_tool_calls=False,
        )
        request = {
            "model": "test",
            "messages": [],
            "parallel_tool_calls": False,
        }
        result = apply_capability_filter(request, caps)
        assert "parallel_tool_calls" not in result  # False 也应移除


class TestCapabilityDegradation:
    """测试能力降级：当渠道不支持某些功能时，请求应被正确处理。"""

    def test_parallel_tool_calls_removed_when_not_supported(self):
        """parallel_tool_calls=True 在不支持时应被移除"""
        caps = ProviderCapabilities(supports_parallel_tool_calls=False)
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "parallel_tool_calls": True,
        }
        result = apply_capability_filter(request, caps)
        assert "parallel_tool_calls" not in result

    def test_response_format_json_schema_removed_when_not_supported(self):
        """response_format json_schema 在不支持时应被移除"""
        caps = ProviderCapabilities(supports_response_format=False)
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "a", "schema": {}},
            },
        }
        result = apply_capability_filter(request, caps)
        assert "response_format" not in result

    def test_reasoning_effort_removed_when_not_supported(self):
        """reasoning_effort 在不支持时应被移除"""
        caps = ProviderCapabilities(supports_reasoning_effort=False)
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "medium",
        }
        result = apply_capability_filter(request, caps)
        assert "reasoning_effort" not in result

    def test_file_content_in_messages_not_supported(self):
        """file content 在不支持时应在请求中移除"""
        caps = ProviderCapabilities(supports_file_content=False)
        request = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {"type": "file", "file": {"file_id": "f1"}},
                    ],
                }
            ],
        }
        result = apply_capability_filter(request, caps)
        # file 块应被移除或降级
        user_msg = result["messages"][0]
        if isinstance(user_msg["content"], list):
            file_parts = [
                p
                for p in user_msg["content"]
                if isinstance(p, dict) and p.get("type") == "file"
            ]
            assert len(file_parts) == 0

    def test_audio_content_in_messages_not_supported(self):
        """audio content 在不支持时应在请求中移除"""
        caps = ProviderCapabilities(supports_audio_content=False)
        request = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this"},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "AAA", "format": "wav"},
                        },
                    ],
                }
            ],
        }
        result = apply_capability_filter(request, caps)
        user_msg = result["messages"][0]
        if isinstance(user_msg["content"], list):
            audio_parts = [
                p
                for p in user_msg["content"]
                if isinstance(p, dict) and p.get("type") == "input_audio"
            ]
            assert len(audio_parts) == 0


class TestMergeSystemMessages:
    """测试 merge_system_messages 函数"""

    def test_single_system_message(self):
        """单条 system 消息应保持不变"""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        result = merge_system_messages(messages)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are helpful."

    def test_multiple_system_messages(self):
        """多条 system 消息应合并为一条"""
        messages = [
            {"role": "system", "content": "Rule 1."},
            {"role": "system", "content": "Rule 2."},
            {"role": "user", "content": "hello"},
        ]
        result = merge_system_messages(messages)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "Rule 1.\n\nRule 2."

    def test_no_system_message(self):
        """无 system 消息时应保持不变"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = merge_system_messages(messages)
        assert len(result) == 2
        assert result[0]["role"] == "user"

    def test_empty_system_content(self):
        """空内容的 system 消息应被忽略"""
        messages = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "hello"},
        ]
        result = merge_system_messages(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"
