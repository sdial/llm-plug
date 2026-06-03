"""REVIEW.md 中已确认的转换器相关问题的回归测试。

这些测试断言**当前（有缺陷）的行为**，每个测试的 docstring 描述具体问题。
当问题被修复后，相关测试会失败，提示修改者同时更新测试以匹配新行为。
"""
import json
import pytest

from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.usage import openai_chat_to_anthropic
from models.api_types import APIType


# ─────────────────────────── N2 ───────────────────────────


class TestN2ThinkingSignature:
    """N2: Anthropic 上游收到 signature='' 的 thinking 块会被官方 API 400。

    Chat → Anthropic 转换会无条件生成 signature="" 的 thinking 块。
    当前部署非官方上游不触发，但代码行为本身有损。
    """

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_chat_request_assistant_reasoning_emits_empty_signature(self):
        """Bug N2: 历史 assistant 消息带 reasoning_content 时, 输出 signature=''. """
        request = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Q"},
                {
                    "role": "assistant",
                    "reasoning_content": "private chain",
                    "content": "A",
                },
                {"role": "user", "content": "follow up"},
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)

        assistant_msg = result["messages"][1]
        thinking_blocks = [
            c for c in assistant_msg["content"]
            if isinstance(c, dict) and c.get("type") == "thinking"
        ]
        assert thinking_blocks, "expected a thinking block to be produced"
        # 当前行为：signature 始终是空字符串，官方 Anthropic 会 400
        assert thinking_blocks[0]["signature"] == ""

    def test_chat_response_thinking_emits_empty_signature(self):
        """Bug N2: 响应转换同样无条件产生 signature=''。"""
        response = {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "private",
                        "content": "visible",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_CHAT)
        thinking_blocks = [c for c in result["content"] if c.get("type") == "thinking"]
        assert thinking_blocks
        assert thinking_blocks[0]["signature"] == ""


# ─────────────────────────── M1 ───────────────────────────


class TestM1StopReasonMappings:
    """M1: finish_reason / stop_reason 双向映射存在死角。"""

    def setup_method(self):
        self.to_anthropic = ToAnthropicConverter()
        self.to_chat = ToChatCompletionsConverter()

    def test_chat_function_call_finish_reason_falls_back_to_end_turn(self):
        """Bug M1: OpenAI 旧 'function_call' finish_reason 不在映射表，回落到 end_turn。"""
        response = {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "calling tool"},
                    "finish_reason": "function_call",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = self.to_anthropic.convert_response(response, APIType.OPENAI_CHAT)
        # 当前 bug: function_call 应该映射到 tool_use 但实际落入默认 end_turn
        assert result["stop_reason"] == "end_turn"

    def test_chat_content_filter_finish_reason_falls_back_to_end_turn(self):
        """Bug M1: OpenAI content_filter finish_reason 丢失为 end_turn。"""
        response = {
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "[filtered]"},
                    "finish_reason": "content_filter",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        result = self.to_anthropic.convert_response(response, APIType.OPENAI_CHAT)
        # 当前 bug: content_filter 应映射到 refusal 但回落到 end_turn
        assert result["stop_reason"] == "end_turn"

    def test_anthropic_pause_turn_maps_to_stop(self):
        """Bug M1: Anthropic 长任务暂停 pause_turn → stop, 导致 agent loop 终止。"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "let me continue later"}],
            "model": "claude-opus-4-7",
            "stop_reason": "pause_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.to_chat.convert_response(response, APIType.ANTHROPIC)
        # 当前 bug: pause_turn 被映射为 stop, agent 框架以为对话已自然结束
        assert result["choices"][0]["finish_reason"] == "stop"


# ─────────────────────────── M2 ───────────────────────────


class TestM2ParallelToolCalls:
    """M2: tool_choice 中的并行控制字段双向丢失。"""

    def setup_method(self):
        self.to_anthropic = ToAnthropicConverter()
        self.to_chat = ToChatCompletionsConverter()

    def test_chat_parallel_tool_calls_false_not_projected_to_anthropic(self):
        """Bug M2: OpenAI 顶层 parallel_tool_calls=false 没有读取, 转 Anthropic 后丢失。"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        result = self.to_anthropic.convert_request(request, APIType.OPENAI_CHAT)
        # 当前 bug: 转换后的 tool_choice 不带 disable_parallel_tool_use=true
        tc = result.get("tool_choice", {})
        assert isinstance(tc, dict)
        assert tc.get("disable_parallel_tool_use") is not True

    def test_anthropic_disable_parallel_tool_use_not_projected_to_chat(self):
        """Bug M2: Anthropic tool_choice.disable_parallel_tool_use 转 Chat 后丢失。"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "Hello"}],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "tools": [
                {
                    "name": "test",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
        result = self.to_chat.convert_request(request, APIType.ANTHROPIC)
        # 当前 bug: 顶层 parallel_tool_calls 不会被设置为 False
        assert result.get("parallel_tool_calls") is not False


# ─────────────────────────── M3 ───────────────────────────


class TestM3TextToolInterleaveOrder:
    """M3: Anthropic → Chat 响应丢失 text/tool_use 交错顺序。"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_text_tool_text_tool_collapses_to_concat_text_and_flat_tool_calls(self):
        """Bug M3: text/tool_use 交错的原始顺序在转 Chat 后无法还原。"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "First I call A. "},
                {"type": "tool_use", "id": "tool_A", "name": "A", "input": {}},
                {"type": "text", "text": "Then I call B."},
                {"type": "tool_use", "id": "tool_B", "name": "B", "input": {}},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        msg = result["choices"][0]["message"]
        # 当前 bug: text 被简单拼接，tool_use 排成扁平数组，无任何顺序信息
        assert msg["content"] == "First I call A. Then I call B."
        assert len(msg["tool_calls"]) == 2
        # 客户端没法知道哪段 text 在哪个 tool 调用之前
        assert "x_content_order" not in msg
        assert "x_content_order" not in result["choices"][0]


# ─────────────────────────── M4 ───────────────────────────


class TestM4ToolResultOrderInUserMessage:
    """M4: Anthropic user 消息内 tool_result 被前置, 破坏时间线。"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_user_text_then_tool_result_then_text_collapses_order(self):
        """Bug M4: [user_text, tool_result, more_user_text] → [tool_msg, user_msg(合并)]"""
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hi"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "result data",
                        },
                        {"type": "text", "text": "Now what?"},
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        messages = result["messages"]
        # 当前 bug: tool 消息排在所有 user text 之前
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "tu_1"
        # 第二条 user 消息把 "Hi" 和 "Now what?" 拼到一起，丢失中间 tool_result 的位置
        assert messages[1]["role"] == "user"
        user_content = messages[1]["content"]
        if isinstance(user_content, list):
            texts = [c.get("text") for c in user_content if c.get("type") == "text"]
        else:
            texts = [user_content]
        joined = "\n".join(t for t in texts if t)
        # 两段 text 被合并为一段
        assert "Hi" in joined
        assert "Now what?" in joined


# ─────────────────────────── M5 ───────────────────────────


class TestM5CacheCreationZeroed:
    """M5: 跨格式后 cache_creation_input_tokens 永久归零。"""

    def test_openai_chat_to_anthropic_loses_cache_creation(self):
        """Bug M5: openai_chat_to_anthropic 强制 cache_creation_input_tokens=0。"""
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
        result = openai_chat_to_anthropic(usage)
        assert result["cache_read_input_tokens"] == 800
        # 当前 bug: cache_creation_input_tokens 信息在 OpenAI 格式中没有载体，归零
        assert result["cache_creation_input_tokens"] == 0


# ─────────────────────────── M6 ───────────────────────────


class TestM6TemperatureRange:
    """M6: temperature 范围未做跨格式缩放, OpenAI 1.5 直接透传给 Anthropic。"""

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_openai_temperature_above_1_passthrough_to_anthropic(self):
        """Bug M6: OpenAI temperature=1.5 被直接透传, 超过 Anthropic [0,1] 范围。"""
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 1.5,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        # 当前 bug: 1.5 没有被 clip 到 1.0
        assert result["temperature"] == 1.5


# ─────────────────────────── M7 ───────────────────────────


class TestM7ResponseFormatSilentlyDropped:
    """M7: response_format / json_schema 在 Chat → Anthropic 方向静默丢失。"""

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_response_format_json_schema_dropped_not_projected_to_tool(self):
        """Bug M7: json_schema 既不出现在结果, 也没有被转换为虚拟工具。"""
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "give json"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            },
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        # 当前 bug: response_format 没有被翻译为 tool + tool_choice
        assert "response_format" not in result
        assert "tools" not in result
        assert "tool_choice" not in result


# ─────────────────────────── M8 ───────────────────────────


class TestM8ContentBlockDowngrade:
    """M8: 多种内容块单向降级为占位文本。"""

    def setup_method(self):
        self.to_chat = ToChatCompletionsConverter()
        self.to_anthropic = ToAnthropicConverter()

    def test_anthropic_document_base64_to_chat_becomes_placeholder_text(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": "JVBERi0xLjQ=",
                            },
                        }
                    ],
                }
            ],
        }
        result = self.to_chat.convert_request(request, APIType.ANTHROPIC)
        content = result["messages"][0]["content"]
        # 当前 bug: PDF 内容退化为 "[DOCUMENT: ...]"
        assert "[DOCUMENT: application/pdf]" in (
            content if isinstance(content, str) else json.dumps(content)
        )

    def test_anthropic_document_url_to_chat_becomes_placeholder_text(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/x.pdf",
                            },
                        }
                    ],
                }
            ],
        }
        result = self.to_chat.convert_request(request, APIType.ANTHROPIC)
        content = result["messages"][0]["content"]
        text_blob = content if isinstance(content, str) else json.dumps(content)
        assert "[DOCUMENT URL:" in text_blob

    def test_anthropic_redacted_thinking_response_silently_dropped(self):
        """Bug M8: redacted_thinking 在响应转换中被完全丢弃, 客户端不知存在过。"""
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "ENCRYPTED"},
                {"type": "text", "text": "answer"},
            ],
            "model": "claude-opus-4-7",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.to_chat.convert_response(response, APIType.ANTHROPIC)
        msg = result["choices"][0]["message"]
        # 当前 bug: redacted_thinking 既未出现在 content, 也未出现在 reasoning_content
        assert msg.get("reasoning_content") is None
        assert msg["content"] == "answer"

    def test_openai_input_audio_to_anthropic_becomes_placeholder_text(self):
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "abc", "format": "mp3"},
                        }
                    ],
                }
            ],
        }
        result = self.to_anthropic.convert_request(request, APIType.OPENAI_CHAT)
        content = result["messages"][0]["content"]
        text_blob = json.dumps(content)
        # 当前 bug: 音频内容降级成方括号占位
        assert "Audio input not supported" in text_blob

    def test_openai_file_no_data_uri_to_anthropic_becomes_placeholder_text(self):
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "file", "file": {"filename": "x.txt"}}
                    ],
                }
            ],
        }
        result = self.to_anthropic.convert_request(request, APIType.OPENAI_CHAT)
        content = result["messages"][0]["content"]
        text_blob = json.dumps(content)
        assert "File input not supported" in text_blob

    def test_openai_refusal_block_to_anthropic_becomes_placeholder_text(self):
        request = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "refusal", "refusal": "no"}
                    ],
                }
            ],
        }
        result = self.to_anthropic.convert_request(request, APIType.OPENAI_CHAT)
        content = result["messages"][0]["content"]
        text_blob = json.dumps(content)
        assert "[REFUSAL]" in text_blob


# ─────────────────────────── M9 ───────────────────────────


class TestM9XStopSequenceFieldPollutesChoice:
    """M9: x_stop_sequence 被塞进 OpenAI Choice 结构, 污染官方 schema。"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_x_stop_sequence_appears_inside_choice_object(self):
        response = {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi STOP"}],
            "model": "claude-opus-4-7",
            "stop_reason": "stop_sequence",
            "stop_sequence": "STOP",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        result = self.converter.convert_response(response, APIType.ANTHROPIC)
        # 当前 bug: 非标字段污染 choices[0]
        assert result["choices"][0].get("x_stop_sequence") == "STOP"

    def test_x_stop_sequence_appears_in_stream_message_delta_choice(self):
        chunks = [
            {"type": "message_start", "message": {"id": "msg_x", "model": "claude", "usage": {}}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "stop_sequence", "stop_sequence": "STOP"},
                "usage": {"output_tokens": 1},
            },
        ]
        outputs = []
        for c in chunks:
            converted = self.converter.convert_stream_chunk(c, APIType.ANTHROPIC.value)
            if converted is not None:
                outputs.append(converted)
        delta_chunks = [
            o for o in outputs
            if o.get("choices") and o["choices"][0].get("finish_reason") == "stop"
        ]
        assert delta_chunks, "should have a finishing chunk"
        assert delta_chunks[-1]["choices"][0].get("x_stop_sequence") == "STOP"


# ─────────────────────────── M10 ──────────────────────────


class TestM10OpenAIParamsSilentlyDropped:
    """M10: OpenAI 独有调参字段批量丢失, 客户端无任何提示。"""

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_seed_dropped_silently(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "seed": 12345,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        # 当前 bug: seed 没有被透传, 也没有任何告警字段
        assert "seed" not in result
        assert "x_unsupported_params" not in result

    def test_logprobs_dropped_silently(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "logprobs": True,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "logprobs" not in result
        assert "x_unsupported_params" not in result

    def test_n_greater_than_1_dropped_silently(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 5,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        # 当前 bug: n=5 直接丢, 没有 fan-out, 也没有 400 拒绝
        assert "n" not in result
        assert "x_unsupported_params" not in result

    def test_frequency_penalty_dropped_silently(self):
        request = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "frequency_penalty": 0.5,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_CHAT)
        assert "frequency_penalty" not in result
        assert "x_unsupported_params" not in result


# ─────────────────────────── M11 ──────────────────────────


class TestM11AnthropicSpecificParamsDropped:
    """M11: Anthropic → Chat 反向 top_k 和 cache_control 单向丢失。"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_top_k_dropped_silently(self):
        request = {
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "top_k": 50,
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        # 当前 bug: top_k 未透传, 即便部分 OpenAI 兼容上游 (vLLM/SGLang) 支持
        assert "top_k" not in result

    def test_cache_control_in_messages_dropped_silently(self):
        request = {
            "model": "claude-opus-4-7",
            "system": [
                {
                    "type": "text",
                    "text": "system prompt",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "hi",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.ANTHROPIC)
        # 当前 bug: cache_control 没有任何投射或告警
        text_blob = json.dumps(result)
        assert "cache_control" not in text_blob


# ─────────────────────────── M12 ──────────────────────────


class TestM12StreamMessageStartInputTokensZero:
    """M12: 流式 message_start 的 input_tokens 起始几乎总是 0。"""

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_first_role_chunk_emits_message_start_with_zero_input_tokens(self):
        """Bug M12: OpenAI 上游首 chunk 没有 usage, message_start.usage.input_tokens=0。"""
        chunk = {
            "id": "chatcmpl-a",
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        events = self.converter._chat_stream_chunk_to_anthropic(chunk)
        message_starts = [e for e in events if e[0] == "message_start"]
        assert message_starts
        usage = message_starts[0][1]["message"]["usage"]
        # 当前 bug: 首帧 message_start 的 input_tokens 永远是 0
        assert usage["input_tokens"] == 0


# ─────────────────────────── M13 ──────────────────────────


class TestM13StreamFirstToolCallEmptyId:
    """M13: 流式首个 tool_call chunk 缺 id 时块 ID 为空字符串。"""

    def setup_method(self):
        self.converter = ToAnthropicConverter()

    def test_tool_call_without_id_emits_content_block_start_with_empty_id(self):
        """Bug M13: 上游延迟给 id, content_block_start 已 emit, 携带 id=''。"""
        # 模拟上游：tool_call 首 chunk 只带 index/name, 不带 id
        chunks = [
            {"id": "chatcmpl-a", "model": "gpt-4o",
             "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {
                "id": "chatcmpl-a",
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    # 没有 id 字段
                                    "function": {"name": "search"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
        ]
        events = []
        for c in chunks:
            events.extend(self.converter._chat_stream_chunk_to_anthropic(c))
        starts = [e for e in events if e[0] == "content_block_start"]
        tool_starts = [s for s in starts if s[1]["content_block"].get("type") == "tool_use"]
        assert tool_starts
        # 当前 bug: id 为空字符串, 后续 tool_result.tool_use_id 无法匹配
        assert tool_starts[0][1]["content_block"]["id"] == ""
