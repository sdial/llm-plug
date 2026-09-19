"""tool_choice / tools / finish_reason 映射表源×目标矩阵直测（ADR-0016 D2 二期）。

映射收敛为两段："源语法 → 中间条目"（converters/parsing_*）与
"中间条目 → 目标语法"（各目标转换器的 render_* 函数）。本文件是二期唯一授权的
converters 域内部缝直测：表驱动，一行 = 一对源/目标取值；不探测 converter 实例状态。

同格式直通（Native Match）不经 converter，矩阵仍按 3×3 全表列出——渲染函数是
格式级的，同格式行同时充当词表恒等性的文档。
"""

from typing import Any

import pytest

from converters.parsing_anthropic import parse_anthropic_stop_reason, parse_anthropic_tool_choice, parse_anthropic_tools
from converters.parsing_chat import parse_chat_finish_reason, parse_chat_tool_choice, parse_chat_tools
from converters.parsing_responses import parse_responses_finish, parse_responses_tool_choice, parse_responses_tools
from converters.to_anthropic import render_finish_anthropic, render_tool_choice_anthropic, render_tools_anthropic
from converters.to_chat import render_finish_chat, render_tool_choice_chat, render_tools_chat
from converters.to_response import render_tool_choice_response, render_tools_response

# --- tool_choice：源语法 → 中间条目 ---

TOOL_CHOICE_PARSE_ROWS = [
    # (source, 源取值, 期望中间条目)
    ("chat", "auto", "auto"),
    ("chat", "none", "none"),
    ("chat", "required", "required"),
    ("chat", "unexpected_string", None),
    ("chat", {"type": "auto"}, "auto"),
    ("chat", {"type": "none"}, "none"),
    ("chat", {"type": "function", "function": {"name": "get_weather"}}, {"type": "function", "name": "get_weather"}),
    ("chat", {"type": "function", "name": "get_weather"}, {"type": "function", "name": "get_weather"}),
    ("anthropic", {"type": "auto"}, "auto"),
    ("anthropic", {"type": "any"}, "required"),
    ("anthropic", {"type": "none"}, "none"),
    ("anthropic", {"type": "tool", "name": "get_weather"}, {"type": "function", "name": "get_weather"}),
    ("anthropic", {"type": "mystery"}, None),
    ("responses", "auto", "auto"),
    ("responses", "none", "none"),
    ("responses", "required", "required"),
    ("responses", {"type": "function", "name": "get_weather"}, {"type": "function", "name": "get_weather"}),
]

# --- tool_choice：中间条目 → 目标语法 ---

TOOL_CHOICE_RENDER_ROWS = [
    # (target, 中间条目, 期望目标取值)
    ("chat", "auto", "auto"),
    ("chat", "none", "none"),
    ("chat", "required", "required"),
    ("chat", {"type": "function", "name": "get_weather"}, {"type": "function", "function": {"name": "get_weather"}}),
    ("chat", {"type": "function", "name": ""}, {"type": "function", "function": {"name": ""}}),
    ("chat", None, None),
    ("anthropic", "auto", {"type": "auto"}),
    ("anthropic", "none", {"type": "none"}),
    ("anthropic", "required", {"type": "any"}),
    ("anthropic", {"type": "function", "name": "get_weather"}, {"type": "tool", "name": "get_weather"}),
    ("anthropic", {"type": "function", "name": ""}, None),  # 空名会被 Anthropic 拒收，丢弃
    ("anthropic", None, None),
    ("responses", "auto", "auto"),
    ("responses", "none", "none"),
    ("responses", "required", "required"),
    ("responses", {"type": "function", "name": "get_weather"}, {"type": "function", "name": "get_weather"}),
    ("responses", {"type": "function", "name": ""}, {"type": "function", "name": ""}),
    ("responses", None, None),
]

PARSE_FUNCS_TOOL_CHOICE = {"chat": parse_chat_tool_choice, "anthropic": parse_anthropic_tool_choice, "responses": parse_responses_tool_choice}
RENDER_FUNCS_TOOL_CHOICE = {"chat": render_tool_choice_chat, "anthropic": render_tool_choice_anthropic, "responses": render_tool_choice_response}


@pytest.mark.parametrize(("source", "raw_value", "expected"), TOOL_CHOICE_PARSE_ROWS, ids=lambda v: repr(v))
def test_tool_choice_parse_matrix(source: str, raw_value: Any, expected: Any):
    parse = PARSE_FUNCS_TOOL_CHOICE[source]
    if expected is None and source == "responses":
        with pytest.raises(ValueError):
            parse(raw_value)
        return
    assert parse(raw_value) == expected


@pytest.mark.parametrize(("target", "entry", "expected"), TOOL_CHOICE_RENDER_ROWS, ids=lambda v: repr(v))
def test_tool_choice_render_matrix(target: str, entry: Any, expected: Any):
    assert RENDER_FUNCS_TOOL_CHOICE[target](entry) == expected


# --- tools：源语法 → 中间条目 ---

TOOLS_PARSE_ROWS = [
    # (source, 源取值, 期望中间条目)
    (
        "chat",
        [{"type": "function", "function": {"name": "f1", "description": "d", "parameters": {"type": "object"}, "strict": True}}],
        [{"type": "function", "name": "f1", "description": "d", "parameters": {"type": "object"}, "strict": True}],
    ),
    ("chat", [{"type": "web_search"}], []),  # 非 function 工具不收录
    (
        "anthropic",
        [{"name": "f1", "description": "d", "input_schema": {"type": "object"}}],
        [{"type": "function", "name": "f1", "description": "d", "parameters": {"type": "object"}}],
    ),
    ("anthropic", [{"name": "f1"}], [{"type": "function", "name": "f1", "description": "", "parameters": None}]),
    (
        "anthropic",
        [{"type": "custom", "name": "f1", "input_schema": {"type": "object"}}],
        [{"type": "function", "name": "f1", "description": "", "parameters": {"type": "object"}}],
    ),
    (
        "responses",
        [{"type": "function", "name": "f1", "parameters": {"type": "object"}}],
        ([{"type": "function", "name": "f1", "description": "", "parameters": {"type": "object"}}], [], []),
    ),
    ("responses", [{"type": "web_search"}, {"type": "code_interpreter"}], ([], ["web_search", "code_interpreter"], [])),
    ("responses", [{"type": "mystery_tool"}], ([], [], ["mystery_tool"])),
]


@pytest.mark.parametrize(("source", "raw_tools", "expected"), TOOLS_PARSE_ROWS, ids=lambda v: repr(v)[:60])
def test_tools_parse_matrix(source: str, raw_tools: list, expected: Any):
    if source == "chat":
        assert parse_chat_tools(raw_tools) == expected
    elif source == "anthropic":
        assert parse_anthropic_tools(raw_tools) == expected
    else:
        assert parse_responses_tools(raw_tools) == expected


# --- tools：中间条目 → 目标语法（无 schema 工具的处置按目标分化） ---

TOOL_ENTRY_WITH_SCHEMA = {"type": "function", "name": "f1", "description": "d", "parameters": {"type": "object"}}
TOOL_ENTRY_NO_SCHEMA = {"type": "function", "name": "f1", "description": "", "parameters": None}

TOOLS_RENDER_ROWS = [
    # (target, 中间条目列表, 期望目标 tools)
    ("chat", [TOOL_ENTRY_WITH_SCHEMA], [{"type": "function", "function": {"name": "f1", "description": "d", "parameters": {"type": "object"}}}]),
    ("chat", [TOOL_ENTRY_NO_SCHEMA], []),  # Chat 目标要求 schema，无 schema 不收录
    ("anthropic", [TOOL_ENTRY_WITH_SCHEMA], [{"name": "f1", "description": "d", "input_schema": {"type": "object"}}]),
    ("anthropic", [TOOL_ENTRY_NO_SCHEMA], [{"name": "f1", "description": "", "input_schema": {"type": "object", "properties": {}}}]),
    ("responses", [TOOL_ENTRY_WITH_SCHEMA], [{"type": "function", "name": "f1", "description": "d", "parameters": {"type": "object"}}]),
    ("responses", [TOOL_ENTRY_NO_SCHEMA], [{"type": "function", "name": "f1", "description": "", "parameters": {}}]),
]


@pytest.mark.parametrize(("target", "entries", "expected"), TOOLS_RENDER_ROWS, ids=lambda v: repr(v)[:60])
def test_tools_render_matrix(target: str, entries: list, expected: list):
    if target == "chat":
        assert render_tools_chat(entries) == expected
    elif target == "anthropic":
        assert render_tools_anthropic(entries) == expected
    else:
        assert render_tools_response(entries) == expected


# --- finish_reason：源语法 → 中间条目（Chat 词表即中间词表） ---

FINISH_PARSE_ROWS = [
    # (source, 源取值, 期望中间 finish 条目)
    ("chat", "stop", "stop"),
    ("chat", "length", "length"),
    ("chat", "tool_calls", "tool_calls"),
    ("chat", "content_filter", "content_filter"),
    ("chat", "function_call", "tool_calls"),  # 废弃别名归一
    ("chat", None, None),
    ("anthropic", "end_turn", "stop"),
    ("anthropic", "max_tokens", "length"),
    ("anthropic", "stop_sequence", "stop"),
    ("anthropic", "tool_use", "tool_calls"),
    ("anthropic", "pause_turn", "stop"),
    ("anthropic", "refusal", "content_filter"),
    ("anthropic", "mystery", "stop"),  # 未知值兜底
    ("anthropic", None, "stop"),
    ("responses", ("incomplete", []), "length"),
    ("responses", ("completed", [{"type": "function_call"}]), "tool_calls"),
    ("responses", ("completed", [{"type": "message"}]), "stop"),
    ("responses", (None, None), "stop"),
]


@pytest.mark.parametrize(("source", "raw_value", "expected"), FINISH_PARSE_ROWS, ids=lambda v: repr(v))
def test_finish_parse_matrix(source: str, raw_value: Any, expected: Any):
    if source == "chat":
        assert parse_chat_finish_reason(raw_value) == expected
    elif source == "anthropic":
        assert parse_anthropic_stop_reason(raw_value) == expected
    else:
        status, output = raw_value
        assert parse_responses_finish(status, output) == expected


# --- finish_reason：中间条目 → 目标语法 ---

FINISH_RENDER_ROWS = [
    # (target, 中间 finish 条目, 期望目标取值)
    ("chat", "stop", "stop"),
    ("chat", "length", "length"),
    ("chat", "tool_calls", "tool_calls"),
    ("chat", "content_filter", "content_filter"),
    ("chat", None, None),
    ("anthropic", "stop", "end_turn"),
    ("anthropic", "length", "max_tokens"),
    ("anthropic", "tool_calls", "tool_use"),
    ("anthropic", "content_filter", "refusal"),
    ("anthropic", "mystery", "end_turn"),  # 未知值兜底
    ("anthropic", None, "end_turn"),
]


@pytest.mark.parametrize(("target", "entry", "expected"), FINISH_RENDER_ROWS, ids=lambda v: repr(v))
def test_finish_render_matrix(target: str, entry: Any, expected: Any):
    render = render_finish_chat if target == "chat" else render_finish_anthropic
    assert render(entry) == expected
