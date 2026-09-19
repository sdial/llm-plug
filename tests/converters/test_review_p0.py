"""REVIEW P0 修复回归测试：跨格式多模态 / Responses 事件 / 流式收尾。

覆盖 C1（Responses→Anthropic 列表 content）、H1（Anthropic→Responses 流式
completed 补 output 与 done 事件）、H2（同 delta content+tool_calls）、
H3（双向多模态转 Responses）、H5（Chat→Anthropic delta:null 与 finish 缺失）。
H4（[DONE] 终止行）走 tests/test_review_p0_e2e.py 的端到端用例。
"""

from converters.to_anthropic import ToAnthropicConverter
from converters.to_response import ToResponseConverter


def feed_chat_chunks(converter, chunks):
    """逐 chunk 走公共两拍协议输入 Chat 流并收集全部输出事件（含拍 2 finalize）。"""
    outputs = []
    for chunk in chunks:
        outputs.extend(converter.convert_stream_chunk(chunk, "openai-chat-completions"))
    outputs.extend(converter.finalize_stream("openai-chat-completions"))
    return outputs


def feed_anthropic_chunks(converter, chunks):
    """逐 chunk 走公共两拍协议输入 Anthropic SSE 事件并收集全部 Responses 输出。"""
    outputs = []
    for chunk in chunks:
        outputs.extend(converter.convert_stream_chunk(chunk, "anthropic"))
    outputs.extend(converter.finalize_stream("anthropic"))
    return outputs


def event_types(events):
    return [e.get("type", "") for e in events if isinstance(e, dict)]


class TestC1ResponsesToAnthropicListContent:
    """C1：Responses→Anthropic 请求的列表 content 必须转换为 Anthropic 块。"""

    def test_text_and_image_blocks_converted(self):
        converter = ToAnthropicConverter()
        request = {
            "model": "claude-3-5-sonnet",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "看这张图"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,QUJD",
                        },
                    ],
                }
            ],
        }
        result = converter.convert_request(request, "openai-response")
        content = result["messages"][0]["content"]
        assert isinstance(content, list)
        types = [b.get("type") for b in content]
        assert types == ["text", "image"]
        assert content[0]["text"] == "看这张图"
        assert content[1]["source"]["type"] == "base64"
        assert content[1]["source"]["media_type"] == "image/png"
        assert content[1]["source"]["data"] == "QUJD"

    def test_output_text_and_http_image(self):
        converter = ToAnthropicConverter()
        request = {
            "model": "claude-3-5-sonnet",
            "input": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "之前的内容"},
                        {"type": "input_image", "image_url": "https://example.com/a.png"},
                    ],
                }
            ],
        }
        result = converter.convert_request(request, "openai-response")
        content = result["messages"][0]["content"]
        types = [b.get("type") for b in content]
        assert "text" in types
        assert content[types.index("image")]["source"] == {
            "type": "url",
            "url": "https://example.com/a.png",
        }

    def test_no_openai_block_types_leak(self):
        converter = ToAnthropicConverter()
        request = {
            "model": "claude-3-5-sonnet",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "hi"},
                        {"type": "input_image", "image_url": "https://x/y.png"},
                    ],
                }
            ],
        }
        result = converter.convert_request(request, "openai-response")
        for block in result["messages"][0]["content"]:
            assert block.get("type") not in ("input_text", "output_text", "input_image")


class TestH3MultimodalToResponses:
    """H3：Chat/Anthropic 多模态请求转 Responses 上游。"""

    def test_chat_image_url_becomes_input_image(self):
        converter = ToResponseConverter()
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "这是什么？"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64,QUJD"},
                        },
                    ],
                }
            ],
        }
        result = converter.convert_request(request, "openai-chat-completions")
        content = result["input"][0]["content"]
        assert isinstance(content, list)
        types = [p.get("type") for p in content]
        assert types == ["input_text", "input_image"]
        assert content[1]["image_url"] == "data:image/jpeg;base64,QUJD"

    def test_chat_audio_and_file(self):
        converter = ToResponseConverter()
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "wav"}},
                        {"type": "file", "file": {"filename": "a.pdf", "file_data": "data:application/pdf;base64,QUJD"}},
                    ],
                }
            ],
        }
        result = converter.convert_request(request, "openai-chat-completions")
        content = result["input"][0]["content"]
        types = [p.get("type") for p in content]
        assert types == ["input_audio", "input_file"]
        assert content[0]["input_audio"] == {"data": "QUJD", "format": "wav"}
        assert content[1]["file_data"] == "data:application/pdf;base64,QUJD"

    def test_anthropic_image_becomes_input_image_not_dict_repr(self):
        converter = ToResponseConverter()
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                        },
                    ],
                }
            ],
        }
        result = converter.convert_request(request, "anthropic")
        content = result["input"][0]["content"]
        assert isinstance(content, list)
        types = [p.get("type") for p in content]
        assert types == ["input_text", "input_image"]
        assert content[1]["image_url"] == "data:image/png;base64,QUJD"
        # 不允许 dict repr 字符串污染 prompt
        joined = str(content)
        assert "'type': 'image'" not in joined

    def test_anthropic_image_url_source(self):
        converter = ToResponseConverter()
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
                    ],
                }
            ],
        }
        result = converter.convert_request(request, "anthropic")
        content = result["input"][0]["content"]
        assert content == [{"type": "input_image", "image_url": "https://x/y.png"}]

    def test_chat_text_only_stays_string(self):
        converter = ToResponseConverter()
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "纯文本"}]},
            ],
        }
        result = converter.convert_request(request, "openai-chat-completions")
        assert result["input"][0]["content"] == "纯文本"


class TestH1AnthropicToResponsesStream:
    """H1：Anthropic→Responses 流式必须有 done 事件与含 output 的 completed。"""

    def _events(self):
        return [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_001",
                    "model": "claude-3-5-sonnet",
                    "usage": {"input_tokens": 10, "output_tokens": 3},
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
                "delta": {"type": "text_delta", "text": "Hello "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "world"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]

    def test_done_events_present(self):
        converter = ToResponseConverter()
        outputs = feed_anthropic_chunks(converter, self._events())
        types = event_types(outputs)
        assert "response.output_item.done" in types
        assert "response.output_text.done" in types
        assert "response.content_part.done" in types

    def test_completed_contains_output(self):
        converter = ToResponseConverter()
        outputs = feed_anthropic_chunks(converter, self._events())
        completed = next(e for e in outputs if isinstance(e, dict) and e.get("type") == "response.completed")
        output = completed["response"]["output"]
        assert output, "completed.output 不能为空（_save_response_state 依赖它）"
        message_items = [i for i in output if i.get("type") == "message"]
        assert message_items and message_items[0]["content"][0]["text"] == "Hello world"

    def test_usage_not_double_counted(self):
        """message_delta 的 output_tokens 是累计值，不能加上 message_start 的初始值。"""
        converter = ToResponseConverter()
        outputs = feed_anthropic_chunks(converter, self._events())
        completed = next(e for e in outputs if isinstance(e, dict) and e.get("type") == "response.completed")
        assert completed["response"]["usage"]["output_tokens"] == 5

    def test_tool_use_done_events(self):
        converter = ToResponseConverter()
        events = [
            {
                "type": "message_start",
                "message": {"id": "msg_t", "model": "claude", "usage": {"input_tokens": 1}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": ' "SF"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        outputs = feed_anthropic_chunks(converter, events)
        types = event_types(outputs)
        assert "response.function_call_arguments.done" in types
        assert types.count("response.output_item.done") == 1
        completed = next(e for e in outputs if e.get("type") == "response.completed")
        fc = [i for i in completed["response"]["output"] if i.get("type") == "function_call"]
        assert fc and fc[0]["arguments"] == '{"city": "SF"}'

    def test_missing_message_delta_finalizes(self):
        """上游断流（无 message_delta）时 finalize 补 completed。"""
        converter = ToResponseConverter()
        events = [
            {
                "type": "message_start",
                "message": {"id": "msg_x", "model": "claude", "usage": {"input_tokens": 1}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial"},
            },
            # 没有 content_block_stop / message_delta，直接断流
        ]
        outputs = feed_anthropic_chunks(converter, events)
        completed = [e for e in outputs if isinstance(e, dict) and e.get("type") == "response.completed"]
        assert completed, "断流时 finalize 必须补 response.completed"
        assert any(i.get("type") == "message" and i["content"][0]["text"] == "partial" for i in completed[-1]["response"]["output"])


class TestH2ChatDeltaWithContentAndToolCalls:
    """H2：同一 delta 同时含 content 与 tool_calls 时不能丢工具调用。"""

    def test_content_and_tool_calls_same_delta(self):
        converter = ToResponseConverter()
        chunks = [
            {
                "id": "chatcmpl-1",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl-1",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "content": "先查天气",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "get_weather", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-1",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            },
            {"id": "chatcmpl-1", "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}},
        ]
        outputs = feed_chat_chunks(converter, chunks)
        types = event_types(outputs)
        assert "response.output_text.delta" in types
        assert types.count("response.output_item.added") == 2  # message + function_call
        completed = next(e for e in outputs if e.get("type") == "response.completed")
        output = completed["response"]["output"]
        messages = [i for i in output if i.get("type") == "message"]
        calls = [i for i in output if i.get("type") == "function_call"]
        assert messages and messages[0]["content"][0]["text"] == "先查天气"
        assert calls and calls[0]["name"] == "get_weather"

    def test_content_and_finish_reason_same_delta(self):
        """M5①：content 与 finish_reason 同 chunk 时 finish 不能丢。"""
        converter = ToResponseConverter()
        chunks = [
            {
                "id": "chatcmpl-2",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl-2",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "被打截断的回答"}, "finish_reason": "length"}],
            },
        ]
        outputs = feed_chat_chunks(converter, chunks)
        completed = next(e for e in outputs if e.get("type") == "response.completed")
        assert completed["response"]["status"] == "incomplete"
        assert completed["response"]["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_text_tool_text_no_duplication(self):
        """M6①：text→tool→text 交错时两个 message 各带自己的文本。"""
        converter = ToResponseConverter()
        chunks = [
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "第一段"}}],
            },
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_9",
                                    "function": {"name": "f", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ],
            },
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "第二段"}}],
            },
            {
                "id": "chatcmpl-3",
                "model": "gpt-4o",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ]
        outputs = feed_chat_chunks(converter, chunks)
        completed = next(e for e in outputs if e.get("type") == "response.completed")
        messages = [i for i in completed["response"]["output"] if i.get("type") == "message"]
        assert len(messages) == 2
        texts = [m["content"][0]["text"] for m in messages]
        assert texts == ["第一段", "第二段"]

    def test_empty_stream_public_protocol_no_crash(self):
        """M5②：空流场景（未喂任何 chunk 即收尾）走公共两拍协议不能抛异常。"""
        converter = ToResponseConverter()
        assert converter.convert_stream_chunk({"choices": []}, "openai-chat-completions") == []
        # 空流 finalize 走安全网补 response.completed，不抛 AttributeError
        final_events = converter.finalize_stream("openai-chat-completions")
        assert [e.get("type") for e in final_events] == ["response.completed"]


class TestH5ChatToAnthropicStreamRobustness:
    """H5：Chat→Anthropic 流式 delta:null 与 finish 缺失。"""

    def test_delta_null_chunk_no_crash(self):
        converter = ToAnthropicConverter()
        chunks = [
            {
                "id": "chatcmpl-x",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl-x",
                "model": "gpt-4o",
                "choices": [{"delta": None, "finish_reason": None}],
            },
            {
                "id": "chatcmpl-x",
                "model": "gpt-4o",
                "choices": [{"delta": None, "finish_reason": "stop"}],
            },
        ]
        outputs = []
        for chunk in chunks:
            for out in converter.convert_stream_chunk(chunk, "openai-chat-completions"):
                outputs.append((out.get("type", ""), out))
        outputs.extend((e.get("type", ""), e) for e in converter.finalize_stream("openai-chat-completions"))
        event_names = [e[0] for e in outputs]
        assert "message_stop" in event_names

    def test_missing_finish_reason_finalizes_with_message_stop(self):
        converter = ToAnthropicConverter()
        chunks = [
            {
                "id": "chatcmpl-y",
                "model": "gpt-4o",
                "choices": [{"delta": {"role": "assistant", "content": ""}}],
            },
            {
                "id": "chatcmpl-y",
                "model": "gpt-4o",
                "choices": [{"delta": {"content": "回答"}}],
            },
            # 上游不发 finish_reason 直接结束
        ]
        outputs = []
        for chunk in chunks:
            for out in converter.convert_stream_chunk(chunk, "openai-chat-completions"):
                outputs.append((out.get("type", ""), out))
        outputs.extend((e.get("type", ""), e) for e in converter.finalize_stream("openai-chat-completions"))
        event_names = [e[0] for e in outputs]
        assert "message_delta" in event_names
        assert event_names[-1] == "message_stop"
        delta_evt = next(e[1] for e in outputs if e[0] == "message_delta")
        assert delta_evt["delta"]["stop_reason"] == "end_turn"
