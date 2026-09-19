"""03: passthrough SSE 行重组装单一内部函数的逐字符 wire 断言（纯函数，无 mock/async/DB）。

`_reassemble_passthrough_sse`（ADR-0015 D3 第 3 对，原为 `_do_stream_request` 内两处
逐字 9 行复制块）把格式化结果中的 event: / data: 行提取出来追加到上游 passthrough
行之后重排。字面字符串钉死字节级形状，golden（tests/streaming/test_streaming.py）
钉死两个调用点（流中上游错误透传 / 正常透传）的端到端行为不变。
"""

from proxy.stream_executor import _reassemble_passthrough_sse

# ─── 重排：passthrough 行在前，event:/data: 行从格式化结果提取追加 ───


def test_reassemble_passthrough_lines_first_then_event_and_data():
    formatted = 'event: foo\ndata: {"a": 1}\n\n'
    assert _reassemble_passthrough_sse(formatted, [": keep-alive", ": ping"], "anthropic") == (': keep-alive\n: ping\nevent: foo\ndata: {"a": 1}\n\n')


def test_reassemble_appends_event_lines_then_data_lines_in_order():
    formatted = 'data: {"x": 1}\nevent: bar\ndata: {"x": 2}\n\n'
    result = _reassemble_passthrough_sse(formatted, ["data: raw"], "openai-chat-completions")
    # event: 行全部在前、data: 行全部在后，各自保持格式化结果内的相对顺序；
    # 尾部空行（split 后的空串）不匹配前缀，不进入重排结果
    assert result == 'data: raw\nevent: bar\ndata: {"x": 1}\ndata: {"x": 2}\n\n'


def test_reassemble_preserves_trailing_newlines_of_formatted_block():
    formatted = "event: foo\n\n"
    assert _reassemble_passthrough_sse(formatted, ["data: raw"], "anthropic") == "data: raw\nevent: foo\n\n"


# ─── 例外分支：Responses 上游 / 空 passthrough 原样返回 ───


def test_reassemble_responses_upstream_returns_formatted_unchanged():
    formatted = 'event: foo\ndata: {"a": 1}\n\n'
    assert _reassemble_passthrough_sse(formatted, ["data: raw"], "openai-response") == formatted


def test_reassemble_empty_passthrough_returns_formatted_unchanged():
    formatted = 'event: foo\ndata: {"a": 1}\n\n'
    assert _reassemble_passthrough_sse(formatted, [], "anthropic") == formatted


# ─── data: 前缀匹配不带空格（与原复制块逐字一致：startswith("data:")） ───


def test_reassemble_matches_data_prefix_without_space():
    formatted = "data:no-space\n\n"
    assert _reassemble_passthrough_sse(formatted, ["data: raw"], "anthropic") == "data: raw\ndata:no-space\n\n"


def test_reassemble_ignores_lines_not_starting_with_event_or_data_prefix():
    formatted = "x-event: foo\nmetadata: bar\n\n"
    # "x-event: " 不以 "event: " 开头，"metadata: " 不以 "data:" 开头——均不提取
    assert _reassemble_passthrough_sse(formatted, ["data: raw"], "anthropic") == "data: raw\n\n"
