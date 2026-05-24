from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter


def feed_anthropic_events(converter, events):
    """辅助函数：逐 chunk 输入并收集全部输出（Chat→Anthropic 方向）"""
    outputs = []
    for evt in events:
        result = converter.convert_stream_chunk(evt, "openai-chat-completions")
        if result is not None:
            et = converter.get_stream_event_type(evt, "openai-chat-completions")
            outputs.append((et, result))
            extra = converter.get_extra_events(result or {})
            for extra_evt in extra:
                if isinstance(extra_evt, tuple) and len(extra_evt) == 2:
                    outputs.append(extra_evt)
                elif isinstance(extra_evt, dict):
                    outputs.append((extra_evt.get("type", ""), extra_evt))
    return outputs


def feed_response_events(converter, events):
    """辅助函数：逐 chunk 输入并收集全部输出（Chat→Response 方向）"""
    outputs = []
    for evt in events:
        result = converter.convert_stream_chunk(evt, "openai-chat-completions")
        if result is not None:
            outputs.append(result)
            extra = converter.get_extra_events(result or {})
            outputs.extend(extra)
    # finalize
    outputs.extend(converter.finalize_stream("openai-chat-completions"))
    return outputs


class TestChatToResponseStreamGolden:
    """Golden test：验证 Chat 流 → Response 流的完整事件顺序。"""

    def test_text_stream_golden_event_order(self):
        """文本流应产生完整的 Responses SSE 生命周期事件。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Hello"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": " world"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]
        outputs = feed_response_events(converter, events)
        event_types = [o.get("type") for o in outputs if isinstance(o, dict)]

        # 验证完整事件顺序
        expected_sequence = [
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ]
        assert event_types == expected_sequence, f"Got: {event_types}"

    def test_tool_call_stream_golden_event_order(self):
        """工具调用流应产生完整生命周期事件。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": ""},
                                }
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"q":"x"}'}}
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        outputs = feed_response_events(converter, events)
        event_types = [o.get("type") for o in outputs if isinstance(o, dict)]

        assert "response.created" in event_types
        assert "response.in_progress" in event_types
        assert "response.output_item.added" in event_types
        assert "response.function_call_arguments.delta" in event_types
        assert "response.function_call_arguments.done" in event_types
        assert "response.completed" in event_types

    def test_mixed_text_and_tool_stream_golden(self):
        """文本 + 工具调用流的事件顺序。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Let me search"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": ""},
                                }
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"q":"x"}'}}
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        outputs = feed_response_events(converter, events)
        event_types = [o.get("type") for o in outputs if isinstance(o, dict)]

        # 文本项先完成，然后工具调用完成
        text_done_idx = event_types.index("response.output_text.done")
        item_done_idx = event_types.index("response.output_item.done")
        args_done_idx = event_types.index("response.function_call_arguments.done")
        completed_idx = event_types.index("response.completed")
        assert text_done_idx < item_done_idx
        assert args_done_idx < completed_idx


class TestChatToAnthropicStream:
    def test_reasoning_content_stream_emits_thinking_with_empty_signature(self):
        """OpenAI reasoning_content 流应转为 Anthropic thinking 块，附 signature_delta 收尾"""
        converter = ToAnthropicConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "Let me think..."}}]},
            {"choices": [{"delta": {"content": "The answer is 42"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = feed_anthropic_events(converter, events)
        delta_types = [d.get("delta", {}).get("type") for _, d in outputs]
        # thinking_delta 必须出现（让 client 渲染思考过程）
        assert "thinking_delta" in delta_types
        # 切到 text 前必须补 signature_delta，结束 thinking 块
        assert "signature_delta" in delta_types
        assert delta_types.index("signature_delta") > delta_types.index("thinking_delta")

        thinking_deltas = [
            d.get("delta", {}).get("thinking")
            for _, d in outputs
            if d.get("delta", {}).get("type") == "thinking_delta"
        ]
        assert thinking_deltas == ["Let me think..."]

        text_deltas = [
            d.get("delta", {}).get("text")
            for _, d in outputs
            if d.get("delta", {}).get("type") == "text_delta"
        ]
        assert text_deltas == ["The answer is 42"]

        # thinking content_block 必须带空 signature
        thinking_starts = [
            d for evt, d in outputs
            if evt == "content_block_start"
            and d.get("content_block", {}).get("type") == "thinking"
        ]
        assert len(thinking_starts) == 1
        assert thinking_starts[0]["content_block"]["signature"] == ""

    def test_message_start_with_usage(self):
        """message_start 应包含 input_tokens"""
        converter = ToAnthropicConverter()
        events = [
            {
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
                "usage": {"prompt_tokens": 42},
            },
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = feed_anthropic_events(converter, events)
        msg_start = [d for et, d in outputs if et == "message_start"][0]
        assert msg_start["message"]["usage"]["input_tokens"] == 42


class TestChatToResponseStream:
    def test_non_stream_chat_response_has_standard_response_fields(self):
        """Chat 非流式响应应转换为标准 Responses 结构。"""
        converter = ToResponseConverter()
        result = converter.convert_response(
            {
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
            "openai-chat-completions",
        )

        assert result["id"].startswith("resp_")
        assert result["_upstream_id"] == "chatcmpl_1"
        assert result["object"] == "response"
        assert result["created_at"] == 123
        assert result["model"] == "gpt-4o"
        assert result["status"] == "completed"
        assert result["output_text"] == "Hello"
        assert result["output"][0]["type"] == "message"
        assert result["output"][0]["content"][0]["text"] == "Hello"
        assert result["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }

    def test_non_stream_chat_tool_calls_have_response_function_call_fields(self):
        """Chat tool_calls 应转换为 Responses function_call 输出项。"""
        converter = ToResponseConverter()
        result = converter.convert_response(
            {
                "id": "chatcmpl_1",
                "created": 123,
                "model": "gpt-4o",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"q":"x"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
            "openai-chat-completions",
        )

        function_call = result["output"][0]
        assert function_call["type"] == "function_call"
        assert function_call["id"].startswith("fc_")
        assert function_call["call_id"] == "call_1"
        assert function_call["name"] == "search"
        assert function_call["arguments"] == '{"q":"x"}'
        assert function_call["status"] == "completed"
        assert result["output_text"] == ""

    def test_non_stream_chat_length_finish_reason_sets_incomplete_details(self):
        converter = ToResponseConverter()
        result = converter.convert_response(
            {
                "id": "chatcmpl_1",
                "created": 123,
                "model": "gpt-4o",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Partial"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
            "openai-chat-completions",
        )

        assert result["status"] == "incomplete"
        assert result["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_non_stream_chat_refusal_maps_to_response_refusal_content(self):
        converter = ToResponseConverter()
        result = converter.convert_response(
            {
                "id": "chatcmpl_1",
                "created": 123,
                "model": "gpt-4o",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "refusal": "I cannot help with that.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
            "openai-chat-completions",
        )

        assert result["output_text"] == ""
        assert result["output"][0]["type"] == "message"
        assert result["output"][0]["content"] == [
            {"type": "refusal", "refusal": "I cannot help with that."}
        ]

    def test_non_stream_chat_usage_details_are_mapped(self):
        converter = ToResponseConverter()
        result = converter.convert_response(
            {
                "id": "chatcmpl_1",
                "created": 123,
                "model": "gpt-4o",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 7},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
            "openai-chat-completions",
        )

        assert result["usage"]["input_tokens_details"]["cached_tokens"] == 7
        assert result["usage"]["output_tokens_details"]["reasoning_tokens"] == 3

    def test_multiple_tool_calls_output_index(self):
        """多个 tool_call 应有递增的 output_index"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search"},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_2",
                                    "function": {"name": "calc"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"finish_reason": "tool_calls"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        added_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.output_item.added"
        ]
        assert len(added_events) == 2
        assert added_events[0]["output_index"] == 0
        assert added_events[1]["output_index"] == 1

    def test_tool_call_arguments_accumulate_by_index_when_delta_has_no_id(self):
        """Chat tool_call argument deltas often omit id and must still be accumulated."""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": ""},
                                }
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"q"'}}
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ':"x"}'}}
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]

        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))
        outputs.extend(converter.finalize_stream("openai-chat-completions"))

        completed = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ][0]
        tool_call = [
            item
            for item in completed["response"]["output"]
            if item["type"] == "function_call"
        ][0]
        assert tool_call["call_id"] == "call_1"
        assert tool_call["arguments"] == '{"q":"x"}'

    def test_reasoning_output_index_not_zero_when_preceded_by_tool(self):
        """reasoning 在 tool_call 之后应有递增的 output_index"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"reasoning_content": "Thinking..."}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        added_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.output_item.added"
        ]
        reasoning_events = [
            o for o in added_events if o.get("item", {}).get("type") == "reasoning"
        ]
        assert len(reasoning_events) == 1
        assert reasoning_events[0]["output_index"] == 1

    def test_response_completed_sent_on_finish_reason(self):
        """finish_reason 应生成包含完整 output 和 usage 的 response.completed 事件"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        outputs.extend(converter.finalize_stream("openai-chat-completions"))
        completed_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]
        assert len(completed_events) == 1
        resp = completed_events[0]["response"]
        assert resp["status"] == "completed"
        # response.completed 必须包含完整的 output 和 usage
        assert "output" in resp
        assert len(resp["output"]) > 0
        assert resp["output"][0]["type"] == "message"
        assert resp["output"][0]["content"][0]["text"] == "Hello"
        assert "usage" in resp
        assert "input_tokens" in resp["usage"]
        assert "output_tokens" in resp["usage"]
        assert resp["output_text"] == "Hello"

    def test_response_completed_with_empty_choices(self):
        """choices 为空但有 finish_reason 时仍应发送 response.completed"""
        converter = ToResponseConverter()
        # 某些上游可能在最后一个 chunk 中 choices 为空数组
        # 但正确的实现应该在有 finish_reason 的 chunk 中处理
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        outputs.extend(converter.finalize_stream("openai-chat-completions"))
        # 验证 response.completed 被发送
        completed_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]
        assert len(completed_events) == 1

    def test_response_completed_incomplete_status(self):
        """finish_reason 为 length 时 response.completed 状态应为 incomplete"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"content": "Partial..."}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        outputs.extend(converter.finalize_stream("openai-chat-completions"))
        completed_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]
        assert len(completed_events) == 1
        assert completed_events[0]["response"]["status"] == "incomplete"
        assert completed_events[0]["response"]["incomplete_details"] == {
            "reason": "max_output_tokens"
        }

    def test_output_item_added_marks_message_and_function_call_in_progress(self):
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Hello"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": ""},
                                }
                            ]
                        }
                    }
                ],
            },
        ]

        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))

        added_items = [
            o["item"] for o in outputs if o.get("type") == "response.output_item.added"
        ]
        message = [item for item in added_items if item["type"] == "message"][0]
        function_call = [
            item for item in added_items if item["type"] == "function_call"
        ][0]
        assert message["status"] == "in_progress"
        assert function_call["status"] == "in_progress"

    def test_usage_chunk_with_empty_choices_is_ignored(self):
        """usage chunk（choices 为空数组）应被忽略，不产生事件，但 usage 数据被累积"""
        converter = ToResponseConverter()
        events = [
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
        result = converter.convert_stream_chunk(events[0], "openai-chat-completions")
        assert result is None
        # usage 数据应被累积到 stream_state 中
        assert converter._stream_state["input_tokens"] == 10
        assert converter._stream_state["output_tokens"] == 5

    def test_response_created_sent_when_first_chunk_has_no_role(self):
        """第一个 chunk 没有 role 字段时应自动发送 response.created"""
        converter = ToResponseConverter()
        # 模拟某些上游实现：第一个 chunk 直接有 content，没有 role
        events = [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        outputs.extend(converter.finalize_stream("openai-chat-completions"))
        # 应有 response.created
        created_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.created"
        ]
        assert len(created_events) == 1
        # 应有 response.completed
        completed_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]
        assert len(completed_events) == 1

    def test_response_created_sent_before_first_content(self):
        """response.created 应在第一个内容事件之前发送"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"content": "Hello"}}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        # 验证顺序：response.created 应该在 response.output_text.delta 之前
        event_types = [o.get("type") if isinstance(o, dict) else None for o in outputs]
        assert event_types[0] == "response.created"
        assert "response.output_text.delta" in event_types

    def test_output_text_delta_contains_response_indexes(self):
        """Responses text delta 应包含 item/content 索引和 sequence_number。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Hello"}}],
            },
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))

        delta = [o for o in outputs if o.get("type") == "response.output_text.delta"][0]
        assert delta["item_id"].startswith("msg_")
        assert delta["output_index"] == 0
        assert delta["content_index"] == 0
        assert delta["sequence_number"] > 0

    def test_text_stream_includes_content_part_lifecycle(self):
        """文本流应包含 content_part added/done 生命周期事件。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Hello"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))

        event_types = [o.get("type") for o in outputs]
        assert event_types.index("response.output_item.added") < event_types.index(
            "response.content_part.added"
        )
        assert event_types.index("response.content_part.added") < event_types.index(
            "response.output_text.delta"
        )
        assert event_types.index("response.output_text.done") < event_types.index(
            "response.content_part.done"
        )
        assert event_types.index("response.content_part.done") < event_types.index(
            "response.output_item.done"
        )

    def test_tool_stream_includes_arguments_done(self):
        """工具调用流结束时应输出 function_call_arguments.done。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": ""},
                                }
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"q":"x"}'}}
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))

        done_events = [
            o
            for o in outputs
            if o.get("type") == "response.function_call_arguments.done"
        ]
        assert len(done_events) == 1
        assert done_events[0]["item_id"].startswith("fc_")
        assert done_events[0]["output_index"] == 0
        assert done_events[0]["arguments"] == '{"q":"x"}'

    def test_output_item_added_before_text_delta(self):
        """response.output_item.added 应在第一个 text delta 之前发送"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        event_types = [o.get("type") if isinstance(o, dict) else None for o in outputs]
        # output_item.added 应在第一个 output_text.delta 之前
        item_added_idx = event_types.index("response.output_item.added")
        first_delta_idx = event_types.index("response.output_text.delta")
        assert item_added_idx < first_delta_idx, (
            f"output_item.added at {item_added_idx} should be before first delta at {first_delta_idx}"
        )
        # 验证 item 类型为 message
        item_added = outputs[item_added_idx]
        assert item_added["item"]["type"] == "message"

    def test_output_text_done_and_output_item_done_sent_on_finish(self):
        """finish_reason 时应发送 response.output_text.done 和 response.output_item.done"""
        converter = ToResponseConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        outputs.extend(converter.finalize_stream("openai-chat-completions"))
        event_types = [o.get("type") if isinstance(o, dict) else None for o in outputs]
        assert "response.output_text.done" in event_types
        assert "response.output_item.done" in event_types
        # 验证顺序：output_text.done → output_item.done → response.completed
        text_done_idx = event_types.index("response.output_text.done")
        item_done_idx = event_types.index("response.output_item.done")
        completed_idx = event_types.index("response.completed")
        assert text_done_idx < item_done_idx < completed_idx

    def test_usage_data_in_response_completed(self):
        """usage 数据应包含在 response.completed 中"""
        converter = ToResponseConverter()
        events = [
            {
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
                "usage": {"prompt_tokens": 100},
            },
            {"choices": [{"delta": {"content": "Hello"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 50, "total_tokens": 150},
            },
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)
        outputs.extend(converter.finalize_stream("openai-chat-completions"))
        completed_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]
        assert len(completed_events) == 1
        usage = completed_events[0]["response"]["usage"]
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["total_tokens"] == 150

    def test_usage_only_chunk_after_finish_updates_response_completed_usage(self):
        """finish_reason 后的 usage-only chunk 应进入 response.completed.usage。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Hi"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                },
            },
        ]

        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))
        outputs.extend(converter.finalize_stream("openai-chat-completions"))

        completed_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]
        assert len(completed_events) == 1
        assert completed_events[0]["response"]["usage"] == {
            "input_tokens": 7,
            "output_tokens": 2,
            "total_tokens": 9,
        }

    def test_usage_only_chunk_after_finish_preserves_usage_details(self):
        """释放 pending response.completed 时应保留 cached/reasoning 等 usage 详细字段。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Hi"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                    "prompt_tokens_details": {"cached_tokens": 5},
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
            },
        ]

        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))
        outputs.extend(converter.finalize_stream("openai-chat-completions"))

        completed = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ][0]
        assert completed["response"]["usage"] == {
            "input_tokens": 7,
            "output_tokens": 2,
            "total_tokens": 9,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 1},
        }

    def test_finish_without_usage_only_chunk_finalizes_completed_on_done(self):
        """没有 finish 后 usage-only chunk 时，finalize_stream 应释放 response.completed。"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "Hi"}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]

        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                outputs.extend(converter.get_extra_events(result or {}))
        assert not [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]

        outputs.extend(converter.finalize_stream("openai-chat-completions"))

        completed_events = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ]
        assert len(completed_events) == 1
        assert (
            completed_events[0]["response"]["output"][0]["content"][0]["text"] == "Hi"
        )

    def test_finalize_stream_emits_response_completed_without_finish_reason(self):
        """上游只返回内容和 [DONE] 时，收尾仍应补出 response.completed"""
        converter = ToResponseConverter()
        events = [
            {
                "id": "chatcmpl_1",
                "model": "glm-5",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl_1",
                "model": "glm-5",
                "choices": [{"delta": {"content": "Hello"}}],
            },
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-chat-completions")
            if result is not None:
                outputs.append(result)
                extra = converter.get_extra_events(result or {})
                outputs.extend(extra)

        outputs.extend(converter.finalize_stream("openai-chat-completions"))

        event_types = [o.get("type") if isinstance(o, dict) else None for o in outputs]
        assert "response.output_text.done" in event_types
        assert "response.output_item.done" in event_types
        assert "response.completed" in event_types
        completed = [
            o
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ][0]
        assert completed["response"]["status"] == "completed"
        assert completed["response"]["output"][0]["content"][0]["text"] == "Hello"


class TestAnthropicToChatStream:
    def test_text_stream(self):
        """Anthropic 文本流 -> OpenAI Chat 流"""
        converter = ToChatCompletionsConverter()
        events = [
            {
                "type": "message_start",
                "message": {"id": "msg_001", "model": "claude-opus-4-7"},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": " world"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)
        contents = [o["choices"][0]["delta"].get("content", "") for o in outputs]
        assert "Hello" in contents
        assert " world" in contents
        assert outputs[-1]["choices"][0]["finish_reason"] == "stop"

    def test_tool_use_stream(self):
        """Anthropic tool_use 流 -> OpenAI Chat tool_calls 流"""
        converter = ToChatCompletionsConverter()
        events = [
            {
                "type": "message_start",
                "message": {"id": "msg_001", "model": "claude-opus-4-7"},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_001",
                    "name": "search",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q": "test"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 10},
            },
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)
        tool_call_events = [
            o for o in outputs if o["choices"][0]["delta"].get("tool_calls")
        ]
        assert len(tool_call_events) >= 1
        assert outputs[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_thinking_stream(self):
        """Anthropic thinking 流 -> OpenAI reasoning_content 流"""
        converter = ToChatCompletionsConverter()
        events = [
            {
                "type": "message_start",
                "message": {"id": "msg_001", "model": "claude-opus-4-7"},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Let me think..."},
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
                "delta": {"type": "text_delta", "text": "The answer is 42"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 10},
            },
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)
        reasoning_parts = [
            o["choices"][0]["delta"].get("reasoning_content", "")
            for o in outputs
            if o["choices"][0]["delta"].get("reasoning_content")
        ]
        assert "Let me think..." in reasoning_parts
        content_parts = [
            o["choices"][0]["delta"].get("content", "")
            for o in outputs
            if o["choices"][0]["delta"].get("content")
        ]
        assert "The answer is 42" in content_parts


class TestAnthropicToChatStreamIncludeUsage:
    """Anthropic→Chat 流式 + stream_options.include_usage 末帧测试。"""

    def test_anthropic_to_chat_stream_emits_usage_when_include_usage(self):
        """当 set_stream_include_usage(True) 后，message_stop 应 emit usage chunk."""
        converter = ToChatCompletionsConverter()
        converter.set_stream_include_usage(True)
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_001",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 100, "cache_read_input_tokens": 20},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)

        # message_stop 应返回 usage chunk（choices 为空数组）
        last_chunk = outputs[-1]
        assert last_chunk["choices"] == [], f"Expected choices=[], got {last_chunk['choices']}"
        assert "usage" in last_chunk
        # prompt_tokens = input_tokens + cache_creation + cache_read = 100 + 0 + 20 = 120
        assert last_chunk["usage"]["prompt_tokens"] == 120
        assert last_chunk["usage"]["completion_tokens"] == 5
        assert last_chunk["usage"]["total_tokens"] == 125
        assert last_chunk["usage"]["prompt_tokens_details"]["cached_tokens"] == 20

    def test_anthropic_to_chat_stream_no_usage_when_flag_false(self):
        """当 include_usage=False（默认）时，message_stop 返回 None，不 emit usage chunk."""
        converter = ToChatCompletionsConverter()
        # 不调用 set_stream_include_usage，默认为 False
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_001",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 100},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)

        # message_stop 应返回 None，最后一个 chunk 是 message_delta（有 finish_reason）
        assert outputs[-1]["choices"][0]["finish_reason"] == "stop"
        # 不应有 choices=[] 的 usage chunk
        usage_chunks = [o for o in outputs if o.get("choices") == [] and "usage" in o]
        assert usage_chunks == [], f"Expected no usage chunk, got {usage_chunks}"

    def test_anthropic_usage_accumulated_from_message_start_and_delta(self):
        """usage 应从 message_start 和 message_delta 两处累积."""
        converter = ToChatCompletionsConverter()
        converter.set_stream_include_usage(True)
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_002",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 50, "cache_creation_input_tokens": 10},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3, "cache_read_input_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "anthropic")
            if result is not None:
                outputs.append(result)

        usage_chunk = outputs[-1]
        # prompt_tokens = 50 + 10 + 5 = 65
        assert usage_chunk["usage"]["prompt_tokens"] == 65
        assert usage_chunk["usage"]["completion_tokens"] == 3
        assert usage_chunk["usage"]["total_tokens"] == 68
        assert usage_chunk["usage"]["prompt_tokens_details"]["cached_tokens"] == 5


class TestAnthropicToResponseCacheTokens:
    """Anthropic→Response 的 cache token 映射测试。"""

    def test_anthropic_to_response_nonstream_cache_tokens(self):
        """非流式响应的 cache tokens 应正确映射到 OpenAI Response usage。"""
        from converters.to_response import ToResponseConverter
        conv = ToResponseConverter()
        anthropic_resp = {
            "id": "msg_y",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 50,
            },
        }
        result = conv.convert_response(anthropic_resp, source_type="anthropic")
        assert result["usage"]["input_tokens"] == 1110  # 10 + 100 + 1000
        assert result["usage"]["output_tokens"] == 50
        assert result["usage"]["total_tokens"] == 1160  # 1110 + 50
        assert result["usage"]["input_tokens_details"]["cached_tokens"] == 1000

    def test_anthropic_to_response_stream_emits_full_usage(self):
        """流式 message_delta 应在 response.completed 中携带完整 usage。"""
        from converters.to_response import ToResponseConverter
        conv = ToResponseConverter()
        events = [
            {"type": "message_start", "message": {"id": "msg_z", "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 50, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}},
            {"type": "message_stop"},
        ]
        completed_events = []
        for e in events:
            out = conv.convert_stream_chunk(e, source_type="anthropic")
            if isinstance(out, dict) and out.get("type") == "response.completed":
                completed_events.append(out)
        assert completed_events, "expected at least one response.completed event"
        usage = completed_events[-1]["response"].get("usage")
        assert usage is not None, "response.completed must carry usage"
        assert usage["input_tokens"] == 1110  # 10 + 100 + 1000
        assert usage["output_tokens"] == 50
        assert usage["total_tokens"] == 1160
        assert usage["input_tokens_details"]["cached_tokens"] == 1000


class TestAnthropicToResponseStreamIndexes:
    """Anthropic→Response 流式事件的索引字段回归测试。"""

    def test_delta_events_include_response_indexes(self):
        converter = ToResponseConverter()
        events = [
            {"type": "message_start", "message": {"id": "msg_idx", "model": "claude-opus-4-7"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_idx", "name": "lookup", "input": {}},
            },
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q":'}},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "hello"}},
        ]

        outputs = []
        for event in events:
            out = converter.convert_stream_chunk(event, "anthropic")
            if out is not None:
                outputs.append(out)
                outputs.extend(converter.get_extra_events(out))

        deltas = {
            event["type"]: event
            for event in outputs
            if event.get("type") in {
                "response.reasoning_summary_text.delta",
                "response.function_call_arguments.delta",
                "response.output_text.delta",
            }
        }

        assert deltas["response.reasoning_summary_text.delta"]["item_id"] == "rs_msg_idx"
        assert deltas["response.reasoning_summary_text.delta"]["output_index"] == 0
        assert deltas["response.reasoning_summary_text.delta"]["content_index"] == 0

        assert deltas["response.function_call_arguments.delta"]["item_id"] == "fc_toolu_idx"
        assert deltas["response.function_call_arguments.delta"]["output_index"] == 1

        assert deltas["response.output_text.delta"]["item_id"] == "msg_idx"
        assert deltas["response.output_text.delta"]["output_index"] == 2
        assert deltas["response.output_text.delta"]["content_index"] == 0


class TestReviewBugFixes:
    """REVIEW1.md C12-C16 五个流式转换 bug 的回归测试。"""

    # --- C12 ---
    def test_c12_repeated_tool_name_does_not_double_emit_content_block_start(self):
        """DeepSeek/Qwen 在每个 delta 重复发送 name 时，不应多次发 content_block_start。"""
        converter = ToAnthropicConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": "{"},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": '"q":"x"'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": "}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        outputs = feed_anthropic_events(converter, events)
        start_events = [e for et, e in outputs if et == "content_block_start"]
        assert len(start_events) == 1, (
            f"Expected exactly 1 content_block_start for the repeated tool_call, "
            f"got {len(start_events)} -> {start_events}"
        )
        # 参数应完整拼接
        arg_text = "".join(
            d.get("delta", {}).get("partial_json", "")
            for et, d in outputs
            if d.get("delta", {}).get("type") == "input_json_delta"
        )
        assert arg_text == '{"q":"x"}', f"Got partial_json={arg_text!r}"

    # --- C13 ---
    def test_c13_function_name_and_arguments_in_same_chunk_preserves_arguments(self):
        """DeepSeek/Qwen 同一 chunk 中给 name 和 arguments 时，首块 arguments 不能丢失。"""
        converter = ToResponseConverter()
        events = [
            {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant"}}]},
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": '{"q":'},
                                }
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"x"}'}}
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
        ]
        outputs = feed_response_events(converter, events)
        args_text = "".join(
            o.get("delta", "")
            for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.function_call_arguments.delta"
        )
        assert args_text == '{"q":"x"}', f"Expected '{{\"q\":\"x\"}}', got {args_text!r}"

        # 最终 completed 的 arguments 也应完整
        completed = [
            o for o in outputs
            if isinstance(o, dict) and o.get("type") == "response.completed"
        ][0]
        tool_call = [
            item for item in completed["response"]["output"]
            if item["type"] == "function_call"
        ][0]
        assert tool_call["arguments"] == '{"q":"x"}'

    # --- C14 ---
    def test_c14_usage_only_chunk_after_finish_does_not_double_message_stop(self):
        """finish_reason 后单独的 usage chunk 不应重复触发 message_stop。"""
        converter = ToAnthropicConverter()
        events = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},  # 无 usage
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
        ]
        outputs = feed_anthropic_events(converter, events)
        stop_events = [(et, d) for et, d in outputs if et == "message_stop"]
        assert len(stop_events) == 1, f"Expected 1 message_stop, got {len(stop_events)}"
        # usage 应反映在最终的 message_delta 中
        delta_events = [d for et, d in outputs if et == "message_delta"]
        assert delta_events, "Expected at least one message_delta"
        last_delta = delta_events[-1]
        assert last_delta["usage"]["output_tokens"] == 1, (
            f"Expected output_tokens=1, got {last_delta['usage']['output_tokens']}"
        )

    # --- C15 ---
    def test_c15_response_reasoning_event_reaches_anthropic_output(self):
        """OpenAI Response 的 reasoning_summary_text 事件应转换为 Anthropic thinking 事件。"""
        converter = ToAnthropicConverter()
        events = [
            {"type": "response.created", "response": {"id": "resp_1", "model": "o1"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "reasoning", "id": "rs_1", "summary": []},
            },
            {"type": "response.reasoning_summary_text.delta", "delta": "thinking step..."},
            {"type": "response.output_item.done", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "message", "id": "msg_1"},
            },
            {"type": "response.output_text.delta", "delta": "the answer"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ]
        outputs = []
        for evt in events:
            result = converter.convert_stream_chunk(evt, "openai-response")
            if result is not None:
                et = converter.get_stream_event_type(evt, "openai-response")
                outputs.append((et, result))
                extra = converter.get_extra_events(result or {})
                for extra_evt in extra:
                    if isinstance(extra_evt, tuple) and len(extra_evt) == 2:
                        outputs.append(extra_evt)
                    elif isinstance(extra_evt, dict):
                        outputs.append((extra_evt.get("type", ""), extra_evt))

        # thinking 文本到达
        thinking_text = "".join(
            d.get("delta", {}).get("thinking", "")
            for et, d in outputs
            if d.get("delta", {}).get("type") == "thinking_delta"
        )
        assert "thinking step..." in thinking_text, (
            f"Expected thinking text in Anthropic output, got events={[et for et, _ in outputs]}"
        )

        # 常规文本仍能到达
        text_content = "".join(
            d.get("delta", {}).get("text", "")
            for et, d in outputs
            if d.get("delta", {}).get("type") == "text_delta"
        )
        assert "the answer" in text_content

    # --- C16 ---
    def test_c16_text_to_tool_call_closes_text_output_first(self):
        """Chat→Response 文本之后切到 function_call，须先关闭 text output。"""
        converter = ToResponseConverter()
        events = [
            {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {"role": "assistant"}}]},
            {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {"content": "Searching"}}]},
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search", "arguments": ""},
                                }
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "{}"}}
                            ]
                        }
                    }
                ],
            },
            {"id": "chatcmpl_1", "model": "gpt-4o", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        outputs = feed_response_events(converter, events)
        event_seq = [o.get("type") for o in outputs if isinstance(o, dict)]

        added_indexes = [i for i, t in enumerate(event_seq) if t == "response.output_item.added"]
        # 至少 2 个 output_item.added：text message + function_call
        assert len(added_indexes) >= 2, f"Expected >=2 output_item.added, got {event_seq}"

        # 找 function_call 对应的 output_item.added
        function_call_added_idx = None
        for i, o in enumerate(outputs):
            if (
                isinstance(o, dict)
                and o.get("type") == "response.output_item.added"
                and o.get("item", {}).get("type") == "function_call"
            ):
                function_call_added_idx = i
                break
        assert function_call_added_idx is not None, "function_call output_item.added not found"

        # text 的关闭事件必须在 function_call 的 added 之前
        for ev_name in ("response.output_text.done", "response.content_part.done", "response.output_item.done"):
            assert ev_name in event_seq, f"Missing {ev_name} in stream"
            assert event_seq.index(ev_name) < function_call_added_idx, (
                f"{ev_name} ({event_seq.index(ev_name)}) must precede "
                f"function_call output_item.added ({function_call_added_idx})"
            )
