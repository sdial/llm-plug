"""AnthropicStreamState 直测（ADR-0026 D1）：脱离 converter 构造 state，钉死不变量方法。

覆盖 header 幂等（ensure_started）、块开闭配对与 index 推进（open_block / append_delta / close_block）、
usage 等待 stash→补发（queue_stop / handle_usage_chunk）、EOF 补偿与幂等（finalize）
及 responses 源 no-op 语义。先例：tests/converters/test_response_stream_state.py（ADR-0022 确立）
"""

from converters.anthropic_stream_state import AnthropicStreamState


def _state(**overrides) -> AnthropicStreamState:
    state = AnthropicStreamState()
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class TestEnsureStarted:
    def test_chat_header_idempotent_only_once(self):
        state = AnthropicStreamState()
        chunk = {"id": "chatcmpl-abc", "model": "gpt-4o", "usage": {"prompt_tokens": 5}}
        first = state.ensure_started(chunk)
        assert len(first) == 1
        assert first[0]["type"] == "message_start"
        assert first[0]["message"]["id"] == "msg_abc"
        assert first[0]["message"]["model"] == "gpt-4o"
        assert first[0]["message"]["usage"]["input_tokens"] == 5
        assert state.started is True
        # 二度幂等
        assert state.ensure_started(chunk) == []
        assert state.ensure_started({"id": "chatcmpl-xyz", "model": "other"}) == []

    def test_chat_header_uses_chatcmpl_prefix_replace(self):
        state = AnthropicStreamState()
        events = state.ensure_started({"id": "chatcmpl-123", "model": "m"})
        assert events[0]["message"]["id"] == "msg_123"

    def test_responses_header_via_response_created_chunk(self):
        state = AnthropicStreamState()
        chunk = {"type": "response.created", "response": {"id": "resp_1", "model": "o1"}}
        events = state.ensure_started(chunk)
        assert events[0]["type"] == "message_start"
        assert events[0]["message"]["id"] == "msg_resp_1"
        assert events[0]["message"]["model"] == "o1"
        assert events[0]["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
        # 幂等
        assert state.ensure_started(chunk) == []


class TestOpenBlockAppendDeltaCloseBlock:
    def test_thinking_open_append_close_includes_signature(self):
        state = AnthropicStreamState()
        state.started = True
        start = state.open_block("thinking")
        assert [e["type"] for e in start] == ["content_block_start"]
        assert start[0]["index"] == 0
        assert start[0]["content_block"]["type"] == "thinking"
        delta = state.append_delta("Think step")
        assert delta[0]["delta"] == {"type": "thinking_delta", "thinking": "Think step"}
        # 关块应先补 signature_delta 再 stop，且 advance
        close = state.close_block(advance_index=True)
        assert [e["type"] for e in close] == ["content_block_delta", "content_block_stop"]
        assert close[0]["delta"]["type"] == "signature_delta"
        assert close[1]["index"] == 0
        assert state.content_block_started is False
        assert state.content_block_index == 1

    def test_text_block_switch_closes_thinking_with_signature_and_advances(self):
        state = AnthropicStreamState()
        state.started = True
        state.open_block("thinking")
        state.append_delta("r")
        # 切换到 text：open_block 应先关 thinking（补 signature）
        events = state.open_block("text")
        assert [e["type"] for e in events] == ["content_block_delta", "content_block_stop", "content_block_start"]
        assert events[0]["delta"]["type"] == "signature_delta"
        assert events[2]["content_block"]["type"] == "text"
        assert events[2]["index"] == 1
        assert state.current_content_type == "text"

    def test_same_type_open_is_noop(self):
        state = AnthropicStreamState()
        state.started = True
        state.open_block("text")
        # 同类型重复 open 应无产出
        assert state.open_block("text") == []
        assert state.content_block_index == 0

    def test_close_without_open_is_noop(self):
        state = AnthropicStreamState()
        assert state.close_block() == []

    def test_tool_block_via_ensure_tool_block_and_delta_index_mapping(self):
        state = AnthropicStreamState()
        state.started = True
        # 首个 tool index 0
        start0 = state.ensure_tool_block(0, "call_1", "search")
        assert start0[0]["index"] == 0
        assert start0[0]["content_block"]["id"] == "call_1"
        delta0 = state.append_delta('{"q":"x"}', tool_index=0)
        assert delta0[0]["index"] == 0
        # 同 index 再次 ensure 为 no-op
        assert state.ensure_tool_block(0, "call_1", "search") == []
        # 新 index 1 应关旧块并 index 自增
        start1 = state.ensure_tool_block(1, "call_2", "calc")
        assert [e["type"] for e in start1] == ["content_block_stop", "content_block_start"]
        assert start1[1]["index"] == 1
        assert state.tool_call_indices[0] == 0
        assert state.tool_call_indices[1] == 1
        delta1 = state.append_delta("{}", tool_index=1)
        assert delta1[0]["index"] == 1

    def test_index_monotonic_across_multiple_blocks(self):
        state = AnthropicStreamState()
        state.started = True
        state.open_block("thinking")
        state.append_delta("r")
        state.open_block("text")
        state.append_delta("t")
        state.ensure_tool_block(0, "c1", "f")
        state.append_delta("{}", tool_index=0)
        state.ensure_tool_block(1, "c2", "g")
        state.append_delta("{}", tool_index=1)
        # 已开 4 块，均已通过 open 时自动 close 上一块，最后一块仍开启
        # 手动 close 最后一个
        state.close_block(advance_index=False)
        # index 推进应为 0->1->2->3，未自增最后一次
        assert state.content_block_index == 3

    def test_responses_function_call_arguments_delta_without_start_uses_current_index(self):
        state = AnthropicStreamState()
        state.started = True
        state.content_block_started = True  # 标记已在块中，但未设类型（fallback 场景）
        # 此时 append_delta 应走 fallback 的 input_json_delta 且 index 为当前
        delta = state.append_delta('{"fallback":true}')
        assert delta[0]["index"] == 0
        assert delta[0]["delta"]["type"] == "input_json_delta"


class TestQueueStop:
    def test_finish_without_usage_stashes_pending(self):
        state = AnthropicStreamState()
        state.started = True
        result = state.queue_stop(usage=None, finish_reason="stop")
        assert result == []
        assert state.pending_finish_reason == "stop"
        assert state.message_stop_sent is False

    def test_usage_arrives_with_pending_releases_stop(self):
        state = AnthropicStreamState()
        state.started = True
        state.content_block_started = True
        state.current_content_type = "text"
        state.pending_finish_reason = "tool_calls"
        # usage 到来应先关块（advance False）再发 stop
        events = state.queue_stop(usage={"completion_tokens": 4}, finish_reason=None)
        # 若显式 finish_reason 未传，应使用 pending 的 tool_calls → tool_use
        assert [e["type"] for e in events] == ["content_block_stop", "message_delta", "message_stop"]
        assert events[1]["delta"]["stop_reason"] == "tool_use"
        assert events[1]["usage"] == {"output_tokens": 4}
        assert state.message_stop_sent is True
        assert state.pending_finish_reason is None

    def test_immediate_finish_with_usage_emits_without_stash(self):
        state = AnthropicStreamState()
        state.started = True
        events = state.queue_stop(usage={"completion_tokens": 10}, finish_reason="stop")
        assert [e["type"] for e in events] == ["message_delta", "message_stop"]
        assert events[0]["delta"]["stop_reason"] == "end_turn"
        assert events[0]["usage"]["output_tokens"] == 10
        assert state.pending_finish_reason is None

    def test_handle_usage_chunk_with_pending(self):
        state = AnthropicStreamState()
        state.started = True
        state.pending_finish_reason = "stop"
        events = state.handle_usage_chunk({"prompt_tokens": 5, "completion_tokens": 3})
        assert [e["type"] for e in events] == ["message_delta", "message_stop"]
        assert events[0]["usage"]["output_tokens"] == 3

    def test_handle_usage_chunk_without_pending_closes_and_uses_default_stop(self):
        state = AnthropicStreamState()
        state.started = True
        state.content_block_started = True
        state.current_content_type = "text"
        events = state.handle_usage_chunk({"completion_tokens": 2})
        assert [e["type"] for e in events] == ["content_block_stop", "message_delta", "message_stop"]
        assert events[1]["delta"]["stop_reason"] == "end_turn"

    def test_queue_stop_idempotent_after_stop_sent(self):
        state = AnthropicStreamState()
        state.started = True
        state.message_stop_sent = True
        assert state.queue_stop(usage={"completion_tokens": 1}, finish_reason="stop") == []
        assert state.handle_usage_chunk({"completion_tokens": 1}) == []

    def test_usage_incremental_output_tokens_via_prev_cumulative(self):
        state = AnthropicStreamState()
        state.started = True
        # 首次 usage 5，增量应为 5
        e1 = state.queue_stop(usage={"completion_tokens": 5}, finish_reason="stop")
        assert e1[0]["usage"]["output_tokens"] == 5
        # 重置后再次 stash→release 应以增量差值产出
        state2 = AnthropicStreamState()
        state2.started = True
        state2.queue_stop(usage=None, finish_reason="stop")
        # 先发一次 usage 3（pending 释放）
        state2.handle_usage_chunk({"completion_tokens": 3})
        assert state2._prev_completion_tokens == 3
        # 再次调用 finalize/shim 不应重复，但直接测 _build 的增量
        state3 = AnthropicStreamState()
        state3.started = True
        state3._prev_completion_tokens = 3
        events = state3._build_message_stop_events(usage={"completion_tokens": 8})
        assert events[0]["usage"]["output_tokens"] == 5  # 8-3

    def test_dead_fields_cleared(self):
        state = AnthropicStreamState()
        assert not hasattr(state, "tool_id")
        assert not hasattr(state, "tool_name")
        assert "tool_id" not in state.__dataclass_fields__
        assert "tool_name" not in state.__dataclass_fields__


class TestFinalize:
    def test_eof_truncation_closes_open_block_and_emits_stop(self):
        state = AnthropicStreamState()
        state.started = True
        state.content_block_started = True
        state.current_content_type = "text"
        finalize = state.finalize()
        assert [e["type"] for e in finalize] == ["content_block_stop", "message_delta", "message_stop"]
        assert finalize[1]["delta"]["stop_reason"] == "end_turn"
        assert finalize[1]["usage"]["output_tokens"] == 0
        # 幂等
        assert state.finalize() == []

    def test_eof_thinking_block_includes_signature_delta(self):
        state = AnthropicStreamState()
        state.started = True
        state.content_block_started = True
        state.current_content_type = "thinking"
        finalize = state.finalize()
        assert [e["type"] for e in finalize] == ["content_block_delta", "content_block_stop", "message_delta", "message_stop"]
        assert finalize[0]["delta"]["type"] == "signature_delta"

    def test_finalize_stash_pending_emits_with_pending_reason(self):
        state = AnthropicStreamState()
        state.started = True
        state.content_block_started = True
        state.current_content_type = "text"
        state.pending_finish_reason = "tool_calls"
        finalize = state.finalize()
        # 关块 + 按 pending 的 tool_calls 映射为 tool_use
        assert finalize[1]["delta"]["stop_reason"] == "tool_use"

    def test_finalize_noop_when_not_started(self):
        state = AnthropicStreamState()
        assert state.finalize() == []
        state.started = False
        state.content_block_started = True
        assert state.finalize() == []

    def test_finalize_noop_when_already_sent(self):
        state = AnthropicStreamState()
        state.started = True
        state.message_stop_sent = True
        assert state.finalize() == []

    def test_finalize_idempotent_after_pending_release(self):
        state = AnthropicStreamState()
        state.started = True
        state.pending_finish_reason = "stop"
        first = state.finalize()
        assert len(first) == 2  # message_delta + message_stop (无开块)
        assert state.finalize() == []
        assert state.finalize() == []
