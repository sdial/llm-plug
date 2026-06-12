"""测试 capability_manager 模块"""

from capability_manager import (
    ProviderCapabilities,
    infer_capabilities,
    apply_capability_filter,
    merge_system_messages,
)
from models.channel import Channel, ModelCapabilities
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
        assert caps.supports_image_content is False
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
                "supports_image_content": True,
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
        assert caps.supports_image_content is True
        assert caps.supports_tool_choice_required is False
        assert caps.supports_strict_tools is False

    def test_model_capabilities_override_multimodal(self):
        """模型级能力覆盖应仅作用于多模态能力。"""
        channel = Channel(
            name="openai",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="test-key",
            model_capabilities={
                "gpt-4o": ModelCapabilities(
                    supports_image_content=True,
                    supports_file_content=True,
                ),
                "gpt-4o-audio-preview": ModelCapabilities(
                    supports_audio_content=True,
                ),
            },
        )
        # gpt-4o 支持图片和文件，但不支持音频
        caps = infer_capabilities(channel, "gpt-4o")
        assert caps.supports_image_content is True
        assert caps.supports_file_content is True
        assert caps.supports_audio_content is False
        # gpt-4o-audio-preview 支持音频
        audio_caps = infer_capabilities(channel, "gpt-4o-audio-preview")
        assert audio_caps.supports_audio_content is True
        assert audio_caps.supports_image_content is False
        # 未配置模型使用默认值
        default_caps = infer_capabilities(channel, "gpt-3.5-turbo")
        assert default_caps.supports_image_content is False
        assert default_caps.supports_audio_content is False
        assert default_caps.supports_file_content is False


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

    def test_image_content_in_messages_not_supported(self):
        """image_url / image content 在不支持时应在请求中移除"""
        caps = ProviderCapabilities(supports_image_content=False)
        request = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BBB"}},
                    ],
                }
            ],
        }
        result = apply_capability_filter(request, caps)
        user_msg = result["messages"][0]
        if isinstance(user_msg["content"], list):
            image_parts = [
                p
                for p in user_msg["content"]
                if isinstance(p, dict) and p.get("type") in ("image_url", "image")
            ]
            assert len(image_parts) == 0

    def test_filter_logs_channel_and_model(self):
        """过滤多模态内容时应记录包含渠道名和模型名的 warn 日志"""
        from loguru import logger

        captured = []
        handler_id = logger.add(lambda msg: captured.append(msg), level="WARNING")
        try:
            caps = ProviderCapabilities(
                supports_image_content=False,
                supports_audio_content=False,
            )
            request = {
                "model": "gpt-3.5-turbo",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                            {"type": "input_audio", "input_audio": {"data": "BBB", "format": "wav"}},
                        ],
                    }
                ],
            }
            apply_capability_filter(request, caps, channel_name="TestChannel", model_name="gpt-3.5-turbo")
        finally:
            logger.remove(handler_id)

        log_text = " ".join(captured)
        assert "TestChannel" in log_text
        assert "gpt-3.5-turbo" in log_text
        assert "image" in log_text or "audio" in log_text

    def test_no_warning_when_no_unsupported_content(self):
        """渠道不支持 image/audio/file，但请求中没有这些内容时，不应发出警告"""
        from loguru import logger

        captured = []
        handler_id = logger.add(lambda msg: captured.append(msg), level="WARNING")
        try:
            caps = ProviderCapabilities(
                supports_image_content=False,
                supports_audio_content=False,
                supports_file_content=False,
            )
            request = {
                "model": "gpt-4",
                "messages": [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Describe this text"},
                    {"role": "assistant", "content": "OK"},
                ],
            }
            result = apply_capability_filter(request, caps, channel_name="TestChannel", model_name="gpt-4")
        finally:
            logger.remove(handler_id)

        # 不应有任何 [CAPABILITY] 相关的警告
        log_text = " ".join(captured)
        assert "[CAPABILITY]" not in log_text
        # 消息内容应保持不变
        assert result["messages"] == request["messages"]

    def test_no_warning_for_text_only_messages(self):
        """纯文本消息不应触发多模态降级警告，即使渠道不支持所有多模态类型"""
        from loguru import logger

        captured = []
        handler_id = logger.add(lambda msg: captured.append(msg), level="WARNING")
        try:
            caps = ProviderCapabilities(
                supports_image_content=False,
                supports_audio_content=False,
                supports_file_content=False,
            )
            request = {
                "model": "mimo-v2.5-pro",
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "text", "text": "world"},
                    ]},
                ],
            }
            result = apply_capability_filter(request, caps, channel_name="XiaoMi-TokenPlan", model_name="mimo-v2.5-pro")
        finally:
            logger.remove(handler_id)

        log_text = " ".join(captured)
        assert "[CAPABILITY]" not in log_text
        # 消息内容应保持不变
        assert result["messages"] == request["messages"]

    def test_warning_only_for_present_content_types(self):
        """只应为实际存在的不支持内容类型发出警告，不存在的类型不应警告"""
        from loguru import logger

        captured = []
        handler_id = logger.add(lambda msg: captured.append(msg), level="WARNING")
        try:
            caps = ProviderCapabilities(
                supports_image_content=False,
                supports_audio_content=False,
                supports_file_content=False,
            )
            # 只有 image，没有 audio 和 file
            request = {
                "model": "gpt-4",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                        ],
                    }
                ],
            }
            result = apply_capability_filter(request, caps, channel_name="TestChannel", model_name="gpt-4")
        finally:
            logger.remove(handler_id)

        log_text = " ".join(captured)
        # 应该有 image 的警告
        assert "image" in log_text
        # 不应该有 audio 或 file 的警告（因为请求中没有这些内容）
        assert "audio" not in log_text
        assert "file" not in log_text
        # image 应被移除
        user_msg = result["messages"][0]
        image_parts = [
            p for p in user_msg["content"]
            if isinstance(p, dict) and p.get("type") in ("image_url", "image")
        ]
        assert len(image_parts) == 0

    def test_warning_for_multiple_present_types(self):
        """当多种不支持的内容类型都存在时，应为每种类型发出警告"""
        from loguru import logger

        captured = []
        handler_id = logger.add(lambda msg: captured.append(msg), level="WARNING")
        try:
            caps = ProviderCapabilities(
                supports_image_content=False,
                supports_audio_content=False,
                supports_file_content=False,
            )
            request = {
                "model": "gpt-4",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                            {"type": "input_audio", "input_audio": {"data": "BBB", "format": "wav"}},
                            {"type": "file", "file": {"file_id": "f1"}},
                        ],
                    }
                ],
            }
            result = apply_capability_filter(request, caps, channel_name="TestChannel", model_name="gpt-4")
        finally:
            logger.remove(handler_id)

        log_text = " ".join(captured)
        # 三种类型都应有警告
        assert "image" in log_text
        assert "audio" in log_text
        assert "file" in log_text
        # 所有多模态内容应被移除，只剩 text
        user_msg = result["messages"][0]
        assert len(user_msg["content"]) == 1
        assert user_msg["content"][0]["type"] == "text"


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

    def test_list_system_content(self):
        """list 形式的 system content（OpenAI Chat 规范）应被正确合并，不抛 TypeError"""
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "Part A"},
                    {"type": "text", "text": "Part B"},
                ],
            },
            {"role": "system", "content": "Plain string rule."},
            {"role": "user", "content": "hello"},
        ]
        result = merge_system_messages(messages)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "Part A\n\nPart B\n\nPlain string rule."

    def test_list_system_content_only(self):
        """仅 list 形式的 system content 也应正确合并"""
        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": "Only rule."}],
            },
            {"role": "user", "content": "hello"},
        ]
        result = merge_system_messages(messages)
        assert len(result) == 2
        assert result[0]["content"] == "Only rule."

    def test_list_system_content_non_text_parts_ignored(self):
        """list content 中非 text 块应被忽略，不抛错"""
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "Keep this."},
                    {"type": "image_url", "image_url": {"url": "http://x"}},
                ],
            },
            {"role": "user", "content": "hello"},
        ]
        result = merge_system_messages(messages)
        assert len(result) == 2
        assert result[0]["content"] == "Keep this."
