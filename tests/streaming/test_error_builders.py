"""02: 错误/终止 SSE 事件构建函数的逐字符 wire 断言（纯函数，无 mock/async/DB）。

字面字符串钉死字节级形状（key 顺序含在内），spec「Implementation Decisions」为唯一事实源。
"""

from converters.stream_events import (
    _build_anthropic_error_events,
    _build_anthropic_message_stop_event,
    _build_chat_done_event,
    _build_chat_error_chunk,
    _build_responses_completed_event,
    _build_responses_error_events,
    _build_responses_failed_event,
)

# ─── 终止事件函数 ───


def test_build_anthropic_message_stop_event_full_wire():
    assert _build_anthropic_message_stop_event() == ['event: message_stop\ndata: {"type": "message_stop"}\n\n']


def test_build_responses_failed_event_full_wire():
    assert _build_responses_failed_event("gpt-4o") == [
        "event: response.failed\n"
        'data: {"type": "response.failed", "response": {'
        '"id": "", "object": "response", "status": "failed", "model": "gpt-4o", '
        '"output": [], '
        '"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}'
        "}}\n\n"
    ]


def test_build_chat_done_event_full_wire():
    assert _build_chat_done_event() == ["data: [DONE]\n\n"]


def test_build_responses_completed_event_full_wire():
    assert _build_responses_completed_event("gpt-4o", 12, 34) == [
        "event: response.completed\n"
        'data: {"type": "response.completed", "response": {'
        '"id": "", "object": "response", "status": "completed", "model": "gpt-4o", '
        '"output": [], '
        '"usage": {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46}'
        "}}\n\n"
    ]


def test_build_responses_completed_event_zero_usage_defaults():
    # 累计 usage 缺省为 0，与 failed 工厂同构（ADR-0015 t05：终端事件单一住所）
    assert _build_responses_completed_event("gpt-4o") == [
        "event: response.completed\n"
        'data: {"type": "response.completed", "response": {'
        '"id": "", "object": "response", "status": "completed", "model": "gpt-4o", '
        '"output": [], '
        '"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}'
        "}}\n\n"
    ]


# ─── 错误构建函数：anthropic ───


def test_build_anthropic_error_events_emits_error_then_message_stop():
    assert _build_anthropic_error_events("Upstream returned unparseable response") == [
        'event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "Upstream returned unparseable response"}}\n\n',
        'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]


def test_build_anthropic_error_events_non_ascii_message_passes_through():
    assert _build_anthropic_error_events("流式传输错误: boom") == [
        'event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "流式传输错误: boom"}}\n\n',
        'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]


# ─── 错误构建函数：responses ───


def test_build_responses_error_events_emits_error_then_response_failed():
    assert _build_responses_error_events("流式传输错误: boom", "gpt-4o") == [
        'event: error\ndata: {"type": "error", "error": {"message": "流式传输错误: boom", "type": "api_error"}}\n\n',
        "event: response.failed\n"
        'data: {"type": "response.failed", "response": {'
        '"id": "", "object": "response", "status": "failed", "model": "gpt-4o", '
        '"output": [], '
        '"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}'
        "}}\n\n",
    ]


def test_build_responses_error_events_uses_ascii_message_for_scenario_a():
    assert _build_responses_error_events("Upstream returned unparseable response", "gpt-4o") == [
        'event: error\ndata: {"type": "error", "error": {"message": "Upstream returned unparseable response", "type": "api_error"}}\n\n',
        "event: response.failed\n"
        'data: {"type": "response.failed", "response": {'
        '"id": "", "object": "response", "status": "failed", "model": "gpt-4o", '
        '"output": [], '
        '"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}'
        "}}\n\n",
    ]


# ─── 错误构建函数：chat ───


def test_build_chat_error_chunk_emits_single_data_line():
    assert _build_chat_error_chunk("Upstream returned unparseable response") == [
        'data: {"error": {"message": "Upstream returned unparseable response", "type": "api_error"}}\n\n'
    ]


def test_build_chat_error_chunk_non_ascii_message_passes_through():
    assert _build_chat_error_chunk("流式传输错误: boom") == ['data: {"error": {"message": "流式传输错误: boom", "type": "api_error"}}\n\n']
