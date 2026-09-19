"""💭 think 过滤时序接缝（ADR-0015 D0 接缝 4；ADR-0016 D0 元组废除后仅 dict 事件）的直测。

三个模块级私有接缝（不进 `__all__`，直接 import 断言输出，先例同
`test_passthrough_reassembly.py`）：

- `_apply_think_filter_to_event`：过滤应用点——dict 事件直接过滤 /
  事件被吞（返回 None）语义；
- `_think_flush_residual_events`：flush 残余合成——按目标格式产出合法事件形状
  （Anthropic 目标复用最近 text 块 index 的 M2 语义）；
- `_format_think_filtered_finalize_events`：converter finalize 事件（两拍协议拍 2）
  的过滤编排（M2 语义：finalize 事件此前零过滤，现统一过滤，被吞事件跳过）。
"""

import json

from proxy.stream_executor import (
    _apply_think_filter_to_event,
    _format_think_filtered_finalize_events,
    _think_flush_residual_events,
)
from think_filter import ThinkFilter

# ─── fixtures ───


def _chat_chunk(content: str) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def _text_delta(index: int, text: str) -> dict:
    return {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}


def _filter_with_unclosed_think() -> ThinkFilter:
    tf = ThinkFilter()
    tf.feed("💭secret")  # 未闭合 💭 块：残余只能靠 flush 吐出
    return tf


# ─── 接缝 1：过滤应用点 _apply_think_filter_to_event ───


def test_apply_no_filter_returns_event_unchanged():
    evt = _chat_chunk("hello")
    assert _apply_think_filter_to_event(evt, None) is evt


def test_apply_chat_dict_filters_closed_think_block():
    out = _apply_think_filter_to_event(_chat_chunk("a💭secret💭b"), ThinkFilter())
    assert out["choices"][0]["delta"]["content"] == "ab"


def test_apply_chat_dict_unclosed_think_swallowed_returns_none():
    assert _apply_think_filter_to_event(_chat_chunk("💭secret"), ThinkFilter()) is None


def test_apply_anthropic_dict_fully_swallowed_returns_none():
    assert _apply_think_filter_to_event(_text_delta(0, "💭secret"), ThinkFilter()) is None


def test_apply_anthropic_dict_partial_filter_rewrites_text():
    out = _apply_think_filter_to_event(_text_delta(0, "a💭secret💭b"), ThinkFilter())
    assert out["delta"]["text"] == "ab"


def test_apply_non_dict_event_passthrough():
    tf = ThinkFilter()
    assert _apply_think_filter_to_event("raw data line", tf) == "raw data line"


# ─── 接缝 2：flush 残余合成 _think_flush_residual_events ───


def test_flush_residual_no_filter_returns_empty():
    assert _think_flush_residual_events(None, False, False, "test-model", 0) == []


def test_flush_residual_no_remaining_returns_empty():
    tf = ThinkFilter()
    tf.feed("plain text")  # 无 💭 标记，feed 已全部吐出
    assert _think_flush_residual_events(tf, False, False, "test-model", 0) == []


def test_flush_residual_chat_target_wire_bytes():
    out = _think_flush_residual_events(_filter_with_unclosed_think(), False, False, "test-model", 0)
    assert out == [
        'data: {"id": "chatcmpl-stream", "object": "chat.completion.chunk", "created": 0, "model": "test-model", '
        '"choices": [{"index": 0, "delta": {"content": "💭secret"}, "finish_reason": null}]}\n\n'
    ]


def test_flush_residual_responses_target_wire_bytes():
    out = _think_flush_residual_events(_filter_with_unclosed_think(), False, True, "test-model", 0)
    assert out == ['event: response.output_text.delta\ndata: {"type": "response.output_text.delta", "delta": "💭secret"}\n\n']


def test_flush_residual_anthropic_reuses_last_text_index_m2():
    out = _think_flush_residual_events(_filter_with_unclosed_think(), True, False, "test-model", 3)
    assert out == [
        'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 3, "delta": {"type": "text_delta", "text": "💭secret"}}\n\n'
    ]


# ─── 接缝 3：finalize 过滤编排 _format_think_filtered_finalize_events ───


def test_format_finalize_empty_returns_empty():
    assert _format_think_filtered_finalize_events([], ThinkFilter(), False) == []


def test_format_finalize_no_filter_formats_as_is():
    # 过滤器缺位（need_think_filter=False）：finalize 事件原样格式化（与历史零过滤等价）
    evt = _chat_chunk("hello")
    out = _format_think_filtered_finalize_events([evt], None, False)
    assert out == [f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"]


def test_format_finalize_chat_filters_and_skips_swallowed():
    clean = _chat_chunk("hello")
    dirty = _chat_chunk("a💭secret💭b")
    swallowed = _chat_chunk("💭secret")
    logs = []
    out = _format_think_filtered_finalize_events([clean, dirty, swallowed], ThinkFilter(), False, log_event=logs.append)
    assert out == [
        f"data: {json.dumps(clean, ensure_ascii=False)}\n\n",
        f"data: {json.dumps(_chat_chunk('ab'), ensure_ascii=False)}\n\n",
    ]
    # 被吞事件不产出也不记日志；过滤后事件逐条记日志
    assert len(logs) == 2


def test_format_finalize_anthropic_events_infer_event_line_from_type():
    """Anthropic 目标 finalize 事件（dict 形态，type 内嵌）经 infer_event_type 推断 event: 行。"""
    out = _format_think_filtered_finalize_events(
        [_text_delta(1, "a💭secret💭b"), _text_delta(2, "💭secret")],
        ThinkFilter(),
        True,
    )
    assert out == ['event: content_block_delta\ndata: {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "ab"}}\n\n']
