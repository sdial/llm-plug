from converters.to_anthropic import ToAnthropicConverter
from models.api_types import APIType


class TestChatToAnthropic:
    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_multiple_system_messages(self):
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "system", "content": "Always be concise"},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert isinstance(result["system"], list)
        assert len(result["system"]) == 2
        assert result["system"][0]["type"] == "text"
        assert result["system"][0]["text"] == "You are helpful"
        assert result["system"][1]["text"] == "Always be concise"

    def test_single_system_message_still_works(self):
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert isinstance(result["system"], list)
        assert len(result["system"]) == 1
        assert result["system"][0]["text"] == "You are helpful"

    def test_tool_choice_required_to_any(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": "required",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tool_choice"]["type"] == "any"

    def test_tool_choice_none_string(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": "none",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tool_choice"]["type"] == "none"

    def test_invalid_json_arguments_fallback(self):
        """无效 JSON 参数应回退为空 dict 但不崩溃"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "test", "arguments": "not valid json"},
                        }
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assistant_msg = [m for m in result["messages"] if m["role"] == "assistant"][0]
        assert assistant_msg["content"][0]["input"] == {}

    def test_assistant_empty_string_content_is_preserved(self):
        """OpenAI content: "" 应保留为空文本块，而不是被当成 None 丢弃。"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "assistant", "content": ""},
            ],
        }

        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)

        assistant_msg = result["messages"][0]
        assert assistant_msg["content"] == [{"type": "text", "text": ""}]

    def test_non_data_image_url_fallback_text(self):
        """HTTP URL 图片应直接转为 Anthropic URL source，避免同步下载阻塞"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/image.png"},
                        },
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        user_msg = result["messages"][0]
        assert user_msg["role"] == "user"
        image = [
            c for c in user_msg["content"]
            if isinstance(c, dict) and c.get("type") == "image"
        ][0]
        assert image["source"] == {
            "type": "url",
            "url": "https://example.com/image.png",
        }

    def test_user_to_metadata_user_id(self):
        """OpenAI user 参数应转为 Anthropic metadata.user_id"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "user": "user_12345",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "metadata" in result
        assert result["metadata"]["user_id"] == "user_12345"

    def test_tools_strict_passthrough(self):
        """OpenAI tools strict 字段应透传到 Anthropic"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "test",
                        "description": "A test function",
                        "parameters": {"type": "object"},
                        "strict": True,
                    },
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tools"][0]["strict"] is True

    def test_input_text_conversion(self):
        """OpenAI Response input_text 应转为 text"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Hello from Response API"},
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        user_msg = result["messages"][0]
        assert user_msg["role"] == "user"
        content = user_msg.get("content")
        if isinstance(content, list):
            assert content[0]["type"] == "text"
            assert content[0]["text"] == "Hello from Response API"
        else:
            assert content == "Hello from Response API"

    def test_refusal_conversion(self):
        """OpenAI refusal 应转为带标记的 text"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "refusal", "refusal": "I cannot answer this"},
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assistant_msg = [m for m in result["messages"] if m["role"] == "assistant"][0]
        content = assistant_msg.get("content")
        if isinstance(content, list):
            has_refusal = any(
                c.get("type") == "text" and "[REFUSAL]" in c.get("text", "")
                for c in content
            )
            assert has_refusal

    def test_developer_role_to_system(self):
        """developer 角色应合并到顶层 system"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "developer", "content": "Use concise language"},
                {"role": "user", "content": "Hello"},
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert isinstance(result["system"], list)
        assert len(result["system"]) == 2
        assert result["system"][0]["text"] == "You are helpful"
        assert result["system"][1]["text"] == "Use concise language"
        # developer 不应出现在 messages 中
        assert all(m["role"] != "developer" for m in result["messages"])

    def test_max_completion_tokens_used_when_max_tokens_missing(self):
        """max_completion_tokens 应在 max_tokens 缺失时被使用"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_completion_tokens": 2048,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["max_tokens"] == 2048

    def test_max_tokens_takes_priority_over_max_completion_tokens(self):
        """max_tokens 应优先于 max_completion_tokens"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 512,
            "max_completion_tokens": 2048,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["max_tokens"] == 512

    def test_tool_choice_function_flat(self):
        """tool_choice 扁平形态 {type:function, name:...} 应正确转换"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": {"type": "function", "name": "get_weather"},
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tool_choice"]["type"] == "tool"
        assert result["tool_choice"]["name"] == "get_weather"

    def test_tool_choice_function_nested(self):
        """tool_choice 嵌套形态仍应正确转换"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["tool_choice"]["type"] == "tool"
        assert result["tool_choice"]["name"] == "get_weather"

    def test_input_audio_not_supported(self):
        """input_audio 应转为文本提示而非静默丢失"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "abc", "format": "mp3"},
                        },
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        user_msg = result["messages"][0]
        content = user_msg.get("content")
        if isinstance(content, list):
            assert any(
                c.get("type") == "text"
                and "Audio input not supported" in c.get("text", "")
                for c in content
            )

    def test_file_data_uri_to_document(self):
        """file data URI 应转为 document"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "file",
                            "file": {
                                "filename": "report.pdf",
                                "file_data": "data:application/pdf;base64,dGVzdA==",
                            },
                        },
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        user_msg = result["messages"][0]
        content = user_msg.get("content")
        if isinstance(content, list):
            assert any(c.get("type") == "document" for c in content)

    def test_file_without_data_uri_fallback(self):
        """file 无 data URI 时应保留文本提示"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "file", "file": {"filename": "report.txt"}},
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        user_msg = result["messages"][0]
        content = user_msg.get("content")
        if isinstance(content, list):
            assert any(
                c.get("type") == "text"
                and "File input not supported" in c.get("text", "")
                for c in content
            )

    def test_assistant_reasoning_content_emits_thinking_with_empty_signature(self):
        """Chat 请求转 Anthropic 上游默认允许空 signature thinking 块"""
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Question"},
                {
                    "role": "assistant",
                    "reasoning_content": "private chain",
                    "content": "Visible answer",
                },
            ],
            "max_tokens": 100,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assistant_msg = result["messages"][1]
        assert assistant_msg["content"] == [
            {"type": "thinking", "thinking": "private chain", "signature": ""},
            {"type": "text", "text": "Visible answer"},
        ]

    def test_chat_response_reasoning_content_emits_thinking_with_empty_signature(self):
        """Chat 响应转 Anthropic 时应生成空 signature thinking 块（thinking 排在 text 之前）"""
        response = {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "private chain",
                        "content": "Visible answer",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_CHAT)
        assert result["content"] == [
            {"type": "thinking", "thinking": "private chain", "signature": ""},
            {"type": "text", "text": "Visible answer"},
        ]

    def test_explicit_thinking_takes_priority_over_reasoning_effort(self):
        """显式 thinking 不应被 reasoning_effort 静默覆盖"""
        explicit_thinking = {"type": "enabled", "budget_tokens": 1234}
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "thinking": explicit_thinking,
            "reasoning_effort": "high",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["thinking"] == explicit_thinking

    def test_reasoning_effort_overrides_enable_thinking_default(self):
        """enable_thinking 的默认预算可被 reasoning_effort 调整"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "enable_thinking": True,
            "reasoning_effort": "low",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 1024}

    def test_chat_to_anthropic_nonstream_translates_cached_tokens(self):
        """Chat 响应的 prompt_tokens_details.cached_tokens 应转为 cache_read_input_tokens"""
        chat_resp = {
            "id": "chatcmpl-xxx",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "total_tokens": 1050,
                "prompt_tokens_details": {"cached_tokens": 900},
            },
        }
        result = self.converter.convert_response(chat_resp, APIType.OPENAI_CHAT)
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 50
        assert result["usage"]["cache_read_input_tokens"] == 900
        assert result["usage"]["cache_creation_input_tokens"] == 0

    def test_chat_to_anthropic_stream_cache_read_from_cached_tokens(self):
        """Chat 流式 message_delta 的 usage 只包含增量 output_tokens。"""
        chunks = [
            {"id": "chatcmpl-a", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}]},
            {"id": "chatcmpl-a", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 900}}},
        ]
        events: list[tuple[str, dict]] = []
        for c in chunks:
            out = self.converter._chat_stream_chunk_to_anthropic(c)
            events.extend(out)
        delta_events = [e for e in events if e[0] == "message_delta"]
        assert delta_events, "expected message_delta"
        md = delta_events[-1][1]
        assert md["usage"] == {"output_tokens": 50}

    def test_response_to_anthropic_nonstream_uses_input_tokens_details(self):
        """Response 响应的 input_tokens_details.cached_tokens 应转为 cache_read_input_tokens"""
        resp = {
            "id": "resp_xxx",
            "object": "response",
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_a",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}],
                }
            ],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 50,
                "total_tokens": 1050,
                "input_tokens_details": {"cached_tokens": 900},
            },
        }
        result = self.converter.convert_response(resp, APIType.OPENAI_RESPONSE)
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 50
        assert result["usage"]["cache_read_input_tokens"] == 900
        assert result["usage"]["cache_creation_input_tokens"] == 0

    def test_response_to_anthropic_stream_uses_input_tokens_details(self):
        """Response 流式 message_delta 的 usage 只包含增量 output_tokens。"""
        chunks = [
            {"type": "response.created", "response": {"id": "resp_a", "model": "gpt-4o", "status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "hi"},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_a",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_a",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "hi"}],
                        }
                    ],
                    "usage": {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 900}},
                },
            },
        ]
        all_events: list[tuple[str, dict]] = []
        for c in chunks:
            out = self.converter._response_stream_chunk_to_anthropic(c)
            all_events.extend(out)
        delta_events = [e for e in all_events if e[0] == "message_delta"]
        assert delta_events, "expected message_delta"
        md = delta_events[-1][1]
        assert md["usage"] == {"output_tokens": 50}

    def test_response_to_anthropic_stream_thinking_start_has_signature(self):
        """Response reasoning 流式块应带空 signature，与 Chat reasoning 路径一致。"""
        chunks = [
            {"type": "response.created", "response": {"id": "resp_a", "model": "gpt-4o", "status": "in_progress"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "reasoning", "id": "rs_1"},
            },
            {"type": "response.reasoning_text.delta", "delta": "thinking"},
        ]

        all_events: list[tuple[str, dict]] = []
        for c in chunks:
            all_events.extend(self.converter._response_stream_chunk_to_anthropic(c))

        thinking_start = [
            event
            for event_type, event in all_events
            if event_type == "content_block_start"
            and event["content_block"]["type"] == "thinking"
        ][0]
        assert thinking_start["content_block"]["signature"] == ""
