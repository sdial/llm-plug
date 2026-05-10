"""
测试 OpenAI Responses API → Chat Completions API 转换

基于官方文档：
- https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/responses/response_create_params.py
- https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/responses/response.py
- https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/responses/response_stream_event.py
"""

from converters.to_chat import ToChatCompletionsConverter
from models.api_types import APIType


class TestResponseRequestToChat:
    """Responses API 请求 → Chat Completions API 请求"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_basic_request_instructions_to_system(self):
        """instructions 字段映射为 system message"""
        request = {
            "model": "gpt-4o",
            "input": [{"role": "user", "content": "Hello"}],
            "instructions": "You are a helpful assistant.",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert result["model"] == "gpt-4o"
        assert result["messages"][0] == {"role": "system", "content": "You are a helpful assistant."}
        assert result["messages"][1] == {"role": "user", "content": "Hello"}

    def test_request_without_instructions(self):
        """无 instructions 时，直接转换 input"""
        request = {
            "model": "gpt-4o",
            "input": [{"role": "user", "content": "Hello"}],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert len(result["messages"]) == 1
        assert result["messages"][0] == {"role": "user", "content": "Hello"}

    def test_input_as_string(self):
        """input 为字符串时，转换为单条 user message"""
        request = {
            "model": "gpt-4o",
            "input": "What is the weather?",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert result["messages"] == [{"role": "user", "content": "What is the weather?"}]

    def test_function_call_input_to_assistant_tool_calls(self):
        """function_call 类型输入转换为 assistant message 的 tool_calls"""
        request = {
            "model": "gpt-4o",
            "input": [
                {"role": "user", "content": "Search for weather"},
                {
                    "type": "function_call",
                    "call_id": "call_abc123",
                    "name": "search",
                    "arguments": '{"query": "weather"}',
                },
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert result["messages"][0] == {"role": "user", "content": "Search for weather"}
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] is None
        assert len(result["messages"][1]["tool_calls"]) == 1
        tc = result["messages"][1]["tool_calls"][0]
        assert tc["id"] == "call_abc123"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "search"
        assert tc["function"]["arguments"] == '{"query": "weather"}'

    def test_function_call_output_to_tool_message(self):
        """function_call_output 类型输入转换为 tool message"""
        request = {
            "model": "gpt-4o",
            "input": [
                {"role": "user", "content": "Search for weather"},
                {"type": "function_call", "call_id": "call_abc123", "name": "search", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_abc123", "output": "Sunny, 25°C"},
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        tool_msg = result["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_abc123"
        assert tool_msg["content"] == "Sunny, 25°C"

    def test_tools_conversion(self):
        """工具定义转换： Responses → Chat"""
        request = {
            "model": "gpt-4o",
            "input": "Hello",
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "Get weather info",
                    "parameters": {"type": "object", "properties": {"location": {"type": "string"}}},
                }
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert "tools" in result
        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_weather"
        assert tool["function"]["description"] == "Get weather info"
        assert tool["function"]["parameters"]["properties"]["location"]["type"] == "string"

    def test_tool_choice_string(self):
        """tool_choice 字符串值直接传递"""
        request = {
            "model": "gpt-4o",
            "input": "Hello",
            "tool_choice": "auto",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)
        assert result["tool_choice"] == "auto"

    def test_tool_choice_required(self):
        """tool_choice required 传递"""
        request = {
            "model": "gpt-4o",
            "input": "Hello",
            "tool_choice": "required",
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)
        assert result["tool_choice"] == "required"

    def test_tool_choice_function_object(self):
        """tool_choice 为 function 对象时直接传递"""
        request = {
            "model": "gpt-4o",
            "input": "Hello",
            "tool_choice": {"type": "function", "name": "get_weather"},
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)
        assert result["tool_choice"] == {"type": "function", "name": "get_weather"}

    def test_max_output_tokens_to_max_tokens(self):
        """max_output_tokens 映射为 max_tokens"""
        request = {
            "model": "gpt-4o",
            "input": "Hello",
            "max_output_tokens": 1000,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)
        assert result["max_tokens"] == 1000

    def test_temperature_and_top_p_passthrough(self):
        """temperature 和 top_p 直接传递"""
        request = {
            "model": "gpt-4o",
            "input": "Hello",
            "temperature": 0.7,
            "top_p": 0.9,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9

    def test_stream_passthrough(self):
        """stream 字段直接传递"""
        request = {
            "model": "gpt-4o",
            "input": "Hello",
            "stream": True,
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)
        assert result["stream"] is True

    def test_multiple_input_messages(self):
        """多轮对话转换"""
        request = {
            "model": "gpt-4o",
            "input": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
                {"role": "user", "content": "How are you?"},
            ],
        }
        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert len(result["messages"]) == 3
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][2]["role"] == "user"

    def test_developer_role_maps_to_system(self):
        request = {
            "model": "gpt-4o",
            "input": [
                {"role": "developer", "content": "Use concise answers."},
                {"role": "user", "content": "Hello"},
            ],
        }

        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert result["messages"][0] == {"role": "system", "content": "Use concise answers."}
        assert result["messages"][1] == {"role": "user", "content": "Hello"}

    def test_structured_input_text_content_is_preserved(self):
        request = {
            "model": "gpt-4o",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Hello"},
                        {"type": "input_text", "text": "world"},
                    ],
                },
            ],
        }

        result = self.converter.convert_request(request, APIType.OPENAI_RESPONSE)

        assert result["messages"] == [{"role": "user", "content": "Hello\nworld"}]


class TestResponseResponseToChat:
    """Responses API 响应 → Chat Completions API 响应"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_basic_text_response(self):
        """基本文本响应转换"""
        response = {
            "id": "resp_abc123",
            "object": "response",
            "created_at": 1234567890,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_xyz",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello, I am an AI assistant."}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_RESPONSE)

        assert result["id"] == "resp_abc123"
        assert result["object"] == "chat.completion"
        assert result["model"] == "gpt-4o"
        assert len(result["choices"]) == 1
        choice = result["choices"][0]
        assert choice["index"] == 0
        assert choice["message"]["role"] == "assistant"
        assert choice["message"]["content"] == "Hello, I am an AI assistant."
        assert choice["finish_reason"] == "stop"
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 20

    def test_response_with_function_call(self):
        """包含 function_call 的响应转换为 tool_calls"""
        response = {
            "id": "resp_abc123",
            "object": "response",
            "created_at": 1234567890,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_def456",
                    "name": "get_weather",
                    "arguments": '{"location": "Beijing"}',
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["finish_reason"] == "tool_calls"
        message = result["choices"][0]["message"]
        assert message["content"] is None
        assert len(message["tool_calls"]) == 1
        tc = message["tool_calls"][0]
        assert tc["id"] == "call_def456"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "get_weather"
        assert tc["function"]["arguments"] == '{"location": "Beijing"}'

    def test_response_with_text_and_function_call(self):
        """同时包含文本和 function_call 的响应"""
        response = {
            "id": "resp_abc123",
            "object": "response",
            "created_at": 1234567890,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_xyz",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Let me check the weather."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_def456",
                    "name": "get_weather",
                    "arguments": '{"location": "Beijing"}',
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 15},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_RESPONSE)

        message = result["choices"][0]["message"]
        assert message["content"] == "Let me check the weather."
        assert len(message["tool_calls"]) == 1
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_response_incomplete_status(self):
        """status 为 incomplete 时 finish_reason 为 length"""
        response = {
            "id": "resp_abc123",
            "object": "response",
            "created_at": 1234567890,
            "model": "gpt-4o",
            "status": "incomplete",
            "output": [
                {
                    "type": "message",
                    "id": "msg_xyz",
                    "content": [{"type": "output_text", "text": "Partial response..."}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 100},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["finish_reason"] == "length"

    def test_empty_output(self):
        """空 output 时的处理"""
        response = {
            "id": "resp_abc123",
            "object": "response",
            "created_at": 1234567890,
            "model": "gpt-4o",
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["message"]["content"] is None
        assert result["choices"][0]["finish_reason"] == "stop"

    def test_function_call_with_id_fallback(self):
        """function_call 使用 id 作为 call_id 的 fallback"""
        response = {
            "id": "resp_abc123",
            "object": "response",
            "created_at": 1234567890,
            "model": "gpt-4o",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_xyz",
                    "call_id": "call_def456",
                    "name": "search",
                    "arguments": "{}",
                }
            ],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
        result = self.converter.convert_response(response, APIType.OPENAI_RESPONSE)

        tc = result["choices"][0]["message"]["tool_calls"][0]
        # 应优先使用 call_id
        assert tc["id"] == "call_def456"


class TestResponseStreamToChat:
    """Responses API 流式事件 → Chat Completions API 流式 chunk"""

    def setup_method(self):
        self.converter = ToChatCompletionsConverter()

    def test_response_created_event(self):
        """response.created 事件转换为首块"""
        chunk = {
            "type": "response.created",
            "response": {
                "id": "resp_abc123",
                "object": "response",
                "status": "in_progress",
                "model": "gpt-4o",
            },
        }
        result = self.converter.convert_stream_chunk(chunk, APIType.OPENAI_RESPONSE)

        assert result is not None
        assert result["id"] == "chatcmpl-resp_abc123"
        assert result["object"] == "chat.completion.chunk"
        assert result["model"] == "gpt-4o"
        assert result["choices"][0]["delta"]["role"] == "assistant"
        assert result["choices"][0]["delta"]["content"] == ""
        assert result["choices"][0]["finish_reason"] is None

    def test_output_text_delta_event(self):
        """response.output_text.delta 事件转换为文本增量"""
        self.converter._reset_stream_state()
        self.converter._stream_state["msg_id"] = "chatcmpl-test"
        self.converter._stream_state["model"] = "gpt-4o"

        chunk = {
            "type": "response.output_text.delta",
            "delta": "Hello",
        }
        result = self.converter.convert_stream_chunk(chunk, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["delta"]["content"] == "Hello"

    def test_output_item_added_function_call(self):
        """response.output_item.added (function_call) 转换为 tool_calls 开始"""
        self.converter._reset_stream_state()
        self.converter._stream_state["msg_id"] = "chatcmpl-test"
        self.converter._stream_state["model"] = "gpt-4o"

        chunk = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "call_id": "call_xyz",
                "name": "search",
                "arguments": "",
            },
        }
        result = self.converter.convert_stream_chunk(chunk, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
        assert result["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_xyz"
        assert result["choices"][0]["delta"]["tool_calls"][0]["type"] == "function"
        assert result["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "search"
        assert result["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == ""

    def test_function_call_arguments_delta(self):
        """response.function_call_arguments.delta 转换为参数增量"""
        self.converter._reset_stream_state()
        self.converter._stream_state["msg_id"] = "chatcmpl-test"
        self.converter._stream_state["model"] = "gpt-4o"
        self.converter._stream_state["output_index_to_tc_index"] = {0: 0}

        chunk = {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"query"',
        }
        result = self.converter.convert_stream_chunk(chunk, APIType.OPENAI_RESPONSE)

        tc = result["choices"][0]["delta"]["tool_calls"][0]
        assert tc["index"] == 0
        assert tc["function"]["arguments"] == '{"query"'

    def test_response_completed_stop(self):
        """response.completed (stop) 事件"""
        self.converter._reset_stream_state()
        self.converter._stream_state["msg_id"] = "chatcmpl-test"
        self.converter._stream_state["model"] = "gpt-4o"

        chunk = {
            "type": "response.completed",
            "response": {
                "id": "resp_abc123",
                "status": "completed",
                "output": [],
            },
        }
        result = self.converter.convert_stream_chunk(chunk, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["finish_reason"] == "stop"
        assert result["choices"][0]["delta"] == {}

    def test_response_completed_with_function_call(self):
        """response.completed 包含 function_call 时 finish_reason 为 tool_calls"""
        self.converter._reset_stream_state()
        self.converter._stream_state["msg_id"] = "chatcmpl-test"
        self.converter._stream_state["model"] = "gpt-4o"

        chunk = {
            "type": "response.completed",
            "response": {
                "id": "resp_abc123",
                "status": "completed",
                "output": [
                    {"type": "function_call", "call_id": "call_xyz", "name": "search", "arguments": "{}"},
                ],
            },
        }
        result = self.converter.convert_stream_chunk(chunk, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_response_completed_incomplete(self):
        """response.completed (incomplete) 事件 → finish_reason 为 length"""
        self.converter._reset_stream_state()
        self.converter._stream_state["msg_id"] = "chatcmpl-test"
        self.converter._stream_state["model"] = "gpt-4o"

        chunk = {
            "type": "response.completed",
            "response": {
                "id": "resp_abc123",
                "status": "incomplete",
            },
        }
        result = self.converter.convert_stream_chunk(chunk, APIType.OPENAI_RESPONSE)

        assert result["choices"][0]["finish_reason"] == "length"

    def test_multiple_function_calls_output_index(self):
        """多个 function_call 的 output_index 正确映射到 tool_calls index"""
        self.converter._reset_stream_state()
        self.converter._stream_state["msg_id"] = "chatcmpl-test"
        self.converter._stream_state["model"] = "gpt-4o"

        # 第一个 function_call
        chunk1 = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "call_1", "name": "func_a", "arguments": ""},
        }
        result1 = self.converter.convert_stream_chunk(chunk1, APIType.OPENAI_RESPONSE)
        assert result1["choices"][0]["delta"]["tool_calls"][0]["index"] == 0

        # 第二个 function_call
        chunk2 = {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"type": "function_call", "call_id": "call_2", "name": "func_b", "arguments": ""},
        }
        result2 = self.converter.convert_stream_chunk(chunk2, APIType.OPENAI_RESPONSE)
        assert result2["choices"][0]["delta"]["tool_calls"][0]["index"] == 1

        # 第二个 function_call 的参数 delta
        chunk3 = {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": '{"x": 1',
        }
        result3 = self.converter.convert_stream_chunk(chunk3, APIType.OPENAI_RESPONSE)
        assert result3["choices"][0]["delta"]["tool_calls"][0]["index"] == 1
