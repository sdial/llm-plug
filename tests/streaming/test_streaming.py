"""P0-1: SSE 协议解析、流式 chunk 构建、preflight 错误处理专项测试"""

import asyncio
import json
import time

import pytest

from proxy_core import (
    _iter_sse_blocks,
    _format_raw_sse,
    _build_chat_stream_chunks_from_object,
    _build_responses_stream_events_from_object,
    _prime_stream,
    _EmptyStreamError,
    _StreamPreflightError,
)


# ─── helpers ───

async def _lines_from_list(lines: list[str]):
    for line in lines:
        yield line


def _collect_sse_blocks(lines: list[str], coalesce: bool = True):
    """同步辅助：把 lines 喂给 _iter_sse_blocks 并收集结果"""

    async def _run():
        blocks = []
        async for event_type, data_lines, passthrough in _iter_sse_blocks(
            _lines_from_list(lines), coalesce_data_lines=coalesce
        ):
            blocks.append((event_type, data_lines, passthrough))
        return blocks

    return asyncio.run(_run())


# ═══════════════════════════════════════════
#  _iter_sse_blocks — SSE 解析
# ═══════════════════════════════════════════

class TestIterSseBlocks:
    """SSE 解析器边界情况"""

    def test_empty_input(self):
        blocks = _collect_sse_blocks([])
        assert blocks == []

    def test_single_data_line(self):
        blocks = _collect_sse_blocks(["data: hello", ""])
        assert len(blocks) == 1
        event_type, data_lines, _ = blocks[0]
        assert event_type is None
        assert data_lines == ["hello"]

    def test_event_and_data(self):
        blocks = _collect_sse_blocks(["event: message", "data: payload", ""])
        assert len(blocks) == 1
        event_type, data_lines, _ = blocks[0]
        assert event_type == "message"
        assert data_lines == ["payload"]

    def test_multiple_data_lines_coalesced(self):
        """多行 data: 默认合并为一个 block"""
        blocks = _collect_sse_blocks([
            "data: part1",
            "data: part2",
            "data: part3",
            "",
        ])
        assert len(blocks) == 1
        _, data_lines, _ = blocks[0]
        assert data_lines == ["part1", "part2", "part3"]

    def test_multiple_data_lines_not_coalesced(self):
        """coalesce_data_lines=False 时每个 data: 独立成 block"""
        blocks = _collect_sse_blocks(
            ["data: part1", "data: part2", ""],
            coalesce=False,
        )
        assert len(blocks) == 2
        assert blocks[0][1] == ["part1"]
        assert blocks[1][1] == ["part2"]

    def test_comment_lines_are_passthrough(self):
        """SSE 注释行（:开头）进入 passthrough"""
        blocks = _collect_sse_blocks([":ping", "data: ok", ""])
        assert len(blocks) == 1
        _, data_lines, passthrough = blocks[0]
        assert data_lines == ["ok"]
        assert passthrough == [":ping"]

    def test_blank_line_separates_blocks(self):
        blocks = _collect_sse_blocks([
            "data: first",
            "",
            "data: second",
            "",
        ])
        assert len(blocks) == 2
        assert blocks[0][1] == ["first"]
        assert blocks[1][1] == ["second"]

    def test_event_line_triggers_early_yield(self):
        """新 event: 行出现时，即使没有空行也提前 yield"""
        blocks = _collect_sse_blocks([
            "event: a",
            "data: 1",
            "event: b",
            "data: 2",
            "",
        ])
        assert len(blocks) == 2
        assert blocks[0][0] == "a"
        assert blocks[0][1] == ["1"]
        assert blocks[1][0] == "b"
        assert blocks[1][1] == ["2"]

    def test_trailing_block_without_blank_line(self):
        """流结束时无空行，最后一个 block 仍然被 yield"""
        blocks = _collect_sse_blocks(["data: last"])
        assert len(blocks) == 1
        assert blocks[0][1] == ["last"]

    def test_only_comment_lines(self):
        blocks = _collect_sse_blocks([":ping", ":pong", ""])
        assert len(blocks) == 1
        _, data_lines, passthrough = blocks[0]
        assert data_lines == []
        assert passthrough == [":ping", ":pong"]

    def test_data_colon_space_stripped(self):
        """data: 后面有一个空格被 lstrip 掉"""
        blocks = _collect_sse_blocks(["data:  hello world", ""])
        assert blocks[0][1] == ["hello world"]

    def test_data_no_space_after_colon(self):
        """data:hello（无空格）也能解析"""
        blocks = _collect_sse_blocks(["data:hello", ""])
        assert blocks[0][1] == ["hello"]

    def test_multiple_events_with_comments(self):
        blocks = _collect_sse_blocks([
            ":keep-alive",
            "event: message_start",
            "data: {\"type\":\"message_start\"}",
            "",
            "event: content_block_delta",
            "data: {\"type\":\"content_block_delta\"}",
            "",
            "event: message_stop",
            "data: {\"type\":\"message_stop\"}",
            "",
        ])
        # 注释行在没有 event/data 前会独立成 block（空行分隔）
        # 此处 :keep-alive 与后续 event 之间无空行，所以合并为同一 block 的 passthrough
        # 实际解析出 4 个 block: [:keep-alive独立block] + 3 个 event block
        assert len(blocks) >= 3
        event_types = [b[0] for b in blocks if b[0] is not None]
        assert event_types == ["message_start", "content_block_delta", "message_stop"]

    def test_unknown_field_goes_to_passthrough(self):
        """非 event:/data:/:开头的行进入 passthrough"""
        blocks = _collect_sse_blocks(["id: 12345", "data: ok", ""])
        assert len(blocks) == 1
        _, data_lines, passthrough = blocks[0]
        assert data_lines == ["ok"]
        assert passthrough == ["id: 12345"]


# ═══════════════════════════════════════════
#  _format_raw_sse — SSE 格式化
# ═══════════════════════════════════════════

class TestFormatRawSse:

    def test_data_only(self):
        result = _format_raw_sse(None, "hello")
        assert result == "data: hello\n\n"

    def test_event_and_data(self):
        result = _format_raw_sse("message", "payload")
        assert result == "event: message\ndata: payload\n\n"

    def test_multiline_data(self):
        result = _format_raw_sse(None, "line1\nline2")
        assert "data: line1\n" in result
        assert "data: line2\n" in result


# ═══════════════════════════════════════════
#  _build_chat_stream_chunks_from_object — 整块 JSON → chunk 拆分
# ═══════════════════════════════════════════

class TestBuildChatStreamChunksFromObject:

    def _make_response(self, content="Hello", tool_calls=None, reasoning=None, finish="stop"):
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": "chatcmpl-test123",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "gpt-4",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }

    def test_basic_text_response(self):
        resp = self._make_response("Hello world")
        chunks = _build_chat_stream_chunks_from_object(resp, "gpt-4")
        assert len(chunks) >= 2  # role chunk + content chunk + finish chunk
        # 第一帧包含 role
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        # 有 content 帧
        content_chunks = [c for c in chunks if "content" in c["choices"][0].get("delta", {})]
        assert len(content_chunks) == 1
        assert content_chunks[0]["choices"][0]["delta"]["content"] == "Hello world"

    def test_response_id_preserved(self):
        resp = self._make_response()
        chunks = _build_chat_stream_chunks_from_object(resp, "gpt-4")
        assert all(c["id"] == "chatcmpl-test123" for c in chunks)

    def test_reasoning_content_emits_separate_chunk(self):
        resp = self._make_response("Answer", reasoning="Let me think...")
        chunks = _build_chat_stream_chunks_from_object(resp, "gpt-4")
        reasoning_chunks = [
            c for c in chunks
            if "reasoning_content" in c["choices"][0].get("delta", {})
        ]
        assert len(reasoning_chunks) == 1
        assert reasoning_chunks[0]["choices"][0]["delta"]["reasoning_content"] == "Let me think..."

    def test_tool_calls_emits_chunks(self):
        tool_calls = [{
            "id": "call_123",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
        }]
        resp = self._make_response(None, tool_calls=tool_calls)
        chunks = _build_chat_stream_chunks_from_object(resp, "gpt-4")
        tool_chunks = [
            c for c in chunks
            if "tool_calls" in c["choices"][0].get("delta", {})
        ]
        assert len(tool_chunks) >= 1

    def test_empty_choices_returns_empty(self):
        resp = {"id": "x", "choices": []}
        chunks = _build_chat_stream_chunks_from_object(resp, "gpt-4")
        assert chunks == []

    def test_no_choices_key_returns_empty(self):
        chunks = _build_chat_stream_chunks_from_object({}, "gpt-4")
        assert chunks == []

    def test_finish_reason_in_last_chunk(self):
        resp = self._make_response("Hi", finish="stop")
        chunks = _build_chat_stream_chunks_from_object(resp, "gpt-4")
        last = chunks[-1]
        assert last["choices"][0]["finish_reason"] == "stop"

    def test_model_fallback_to_arg(self):
        """response 里没有 model 字段时使用传入参数"""
        resp = {"id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}
        chunks = _build_chat_stream_chunks_from_object(resp, "fallback-model")
        assert all(c["model"] == "fallback-model" for c in chunks)


# ═══════════════════════════════════════════
#  _build_responses_stream_events_from_object — Response SSE 事件拆分
# ═══════════════════════════════════════════

class TestBuildResponsesStreamEventsFromObject:

    @staticmethod
    def _parse_events(events: list[str]) -> list[dict]:
        """从 SSE 事件文本中提取 data JSON"""
        parsed = []
        for e in events:
            for line in e.split("\n"):
                if line.startswith("data: "):
                    parsed.append(json.loads(line[6:]))
        return parsed

    def test_creates_response_created_event(self):
        resp = {"id": "resp_test", "output": [], "status": "completed"}
        events = _build_responses_stream_events_from_object(resp)
        parsed = self._parse_events(events)
        types = [p["type"] for p in parsed]
        assert "response.created" in types

    def test_creates_completed_event(self):
        resp = {"id": "resp_test", "output": [], "status": "completed"}
        events = _build_responses_stream_events_from_object(resp)
        parsed = self._parse_events(events)
        types = [p["type"] for p in parsed]
        assert "response.completed" in types

    def test_message_output_generates_text_delta(self):
        resp = {
            "id": "resp_test",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello"}],
            }],
            "status": "completed",
        }
        events = _build_responses_stream_events_from_object(resp)
        parsed = self._parse_events(events)
        types = [p["type"] for p in parsed]
        assert "response.output_text.delta" in types
        assert "response.output_item.added" in types

    def test_empty_output_minimal_events(self):
        resp = {"id": "resp_test", "output": [], "status": "completed"}
        events = _build_responses_stream_events_from_object(resp)
        # 至少 response.created + response.completed
        assert len(events) >= 2


# ═══════════════════════════════════════════
#  _prime_stream — 首 chunk 预取
# ═══════════════════════════════════════════

class TestPrimeStream:

    @pytest.mark.asyncio
    async def test_empty_stream_raises_empty_stream_error(self):
        async def empty_gen():
            return
            yield  # unreachable

        with pytest.raises(_EmptyStreamError):
            await _prime_stream(empty_gen())

    @pytest.mark.asyncio
    async def test_normal_stream_replays_first_chunk(self):
        async def gen():
            yield "chunk1"
            yield "chunk2"

        replay = await _prime_stream(gen())
        collected = []
        async for chunk in replay:
            collected.append(chunk)
        assert collected == ["chunk1", "chunk2"]

    @pytest.mark.asyncio
    async def test_preflight_error_is_unwrapped(self):
        """_StreamPreflightError 应被解包为原始异常"""
        original = ValueError("upstream 500")

        async def gen():
            raise _StreamPreflightError(original)
            yield  # unreachable

        with pytest.raises(ValueError, match="upstream 500"):
            await _prime_stream(gen())

    @pytest.mark.asyncio
    async def test_single_chunk_stream(self):
        async def gen():
            yield "only"

        replay = await _prime_stream(gen())
        collected = []
        async for chunk in replay:
            collected.append(chunk)
        assert collected == ["only"]
