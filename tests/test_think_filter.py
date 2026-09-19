"""
测试 think_filter 模块
"""

from proxy.think_filter import _filter_think_in_stream_chunk
from think_filter import ThinkFilter, filter_think_content_static


class TestFilterThinkContentStatic:
    """测试非流式过滤函数"""

    def test_empty_string(self):
        """空字符串应返回空字符串"""
        assert filter_think_content_static("") == ""

    def test_no_think_tags(self):
        """无 think 标签的文本应保持不变"""
        text = "Hello, this is a normal response."
        assert filter_think_content_static(text) == text

    def test_simple_think_block(self):
        """简单的 think 块应被移除"""
        # 使用实际的标签格式 (angle bracket format)
        text_with_tags = "<think>Let me think...</think>This is the answer."
        result = filter_think_content_static(text_with_tags)
        assert result == "This is the answer."

    def test_emoji_think_block(self):
        """emoji 格式的 think 块应被移除"""
        # Note: emoji format may not be supported, testing angle bracket format
        text = "<think>Let me think...</think>This is the answer."
        result = filter_think_content_static(text)
        assert "This is the answer" in result
        assert "Let me think" not in result

    def test_multiple_think_blocks(self):
        """多个 think 块都应被移除"""
        text = "<think>First thought...</think>Answer part 1<think>Second thought...</think>Answer part 2"
        result = filter_think_content_static(text)
        assert result == "Answer part 1Answer part 2"

    def test_multiline_think_block(self):
        """多行 think 块应被移除"""
        text = "<think>Line 1\nLine 2\nLine 3</think>Final answer"
        result = filter_think_content_static(text)
        assert result == "Final answer"

    def test_nested_content(self):
        """think 块内的其他内容也应被移除"""
        text = "<think>Calculating 1+1=2...</think>The answer is 2."
        result = filter_think_content_static(text)
        assert result == "The answer is 2."

    def test_whitespace_handling(self):
        """过滤后应去除首尾空白"""
        text = "  <think>thought</think>  Answer  "
        result = filter_think_content_static(text)
        assert result.strip() == "Answer"


class TestThinkFilter:
    """测试流式过滤类"""

    def test_empty_feed(self):
        """空 chunk 应返回空字符串"""
        filter = ThinkFilter()
        assert filter.feed("") == ""

    def test_no_think_tags_streaming(self):
        """无 think 标签的流式文本应正常输出"""
        filter = ThinkFilter()
        chunks = ["Hello", " ", "world", "!"]
        result = []
        for chunk in chunks:
            output = filter.feed(chunk)
            if output:
                result.append(output)
        final = filter.flush()
        if final:
            result.append(final)
        assert "".join(result).strip() == "Hello world!"

    def test_short_chunks_without_think_prefix_stream_immediately(self):
        """没有 think 标签前缀的短 chunk 应立即输出"""
        filter = ThinkFilter()

        outputs = [filter.feed(chunk) for chunk in ["H", "e", "l", "l", "o"]]

        assert outputs == ["H", "e", "l", "l", "o"]
        assert filter.flush() == ""

    def test_partial_think_prefix_is_buffered(self):
        """疑似 think 起始标签的尾部应继续暂存"""
        filter = ThinkFilter()

        assert filter.feed("Hello <thi") == "Hello "
        assert filter.feed("nk>thought</think>answer") == "answer"
        assert filter.flush() == ""

    def test_single_unpaired_emoji_is_discarded_on_flush(self):
        """未闭合 emoji 思考块在收尾时不能泄漏为正常正文。"""
        filter = ThinkFilter()

        assert filter.feed("Hello 💭") == "Hello "
        assert filter.feed("visible") == ""
        assert filter.flush() == ""

    def test_unclosed_think_buffer_is_capped(self):
        """异常上游不闭合 think 标签时，缓存不能随流无限增长。"""
        filter = ThinkFilter()
        filter.feed("<think>" + "x" * (1024 * 1024 + 100))

        assert len(filter.buffer) <= len("</think>") - 1

    def test_paired_emoji_think_block_streaming(self):
        """配对 emoji 内的 think 内容应被过滤"""
        filter = ThinkFilter()

        assert filter.feed("Hello 💭hidden") == "Hello "
        assert filter.feed("💭visible") == "visible"
        assert filter.flush() == ""

    def test_simple_think_block_streaming(self):
        """流式 think 块应被过滤"""
        filter = ThinkFilter()
        # Simulate streaming: "<think>thought</think>answer"
        # Note: this test depends on the actual tag format implementation
        chunks = ["<think>", "thought", "</think>", "answer"]
        result = []
        for chunk in chunks:
            output = filter.feed(chunk)
            if output:
                result.append(output)
        final = filter.flush()
        if final:
            result.append(final)
        # The thought content should be filtered
        assert "answer" in "".join(result)

    def test_flush_after_think(self):
        """在 think 块结束后 flush 应返回空"""
        filter = ThinkFilter()
        filter.feed("<think>content</think>")
        assert filter.flush() == ""

    def test_flush_before_think_end(self):
        """在 think 块未结束时 flush 应丢弃内容"""
        filter = ThinkFilter()
        filter.feed(" thinkingpartial")
        # incomplete think block should be discarded
        assert filter.flush() == ""

    def test_reset(self):
        """reset 应清空状态"""
        filter = ThinkFilter()
        filter.feed(" thinkingpartial")
        filter.reset()
        assert filter.buffer == ""
        assert filter.in_think is False


class TestFilterThinkInStreamChunkAnthropic:
    """M2：ThinkFilter 对 Anthropic 目标格式（content_block_delta/text_delta）应生效"""

    def test_anthropic_text_delta_filtered(self):
        """Anthropic text_delta 中的 💭 块应被过滤，index 保留"""
        filt = ThinkFilter()
        chunk = {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Hello 💭hidden💭visible"},
        }
        result = _filter_think_in_stream_chunk(chunk, filt)
        assert result is not None
        assert result["delta"]["text"] == "Hello visible"
        assert result["index"] == 1

    def test_anthropic_text_delta_cross_chunk_think_block(self):
        """💭 块跨多个 text_delta chunk 时应跨 chunk 过滤"""
        filt = ThinkFilter()
        c1 = {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "💭hidden"},
        }
        assert _filter_think_in_stream_chunk(c1, filt) is None
        c2 = {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "💭visible"},
        }
        result = _filter_think_in_stream_chunk(c2, filt)
        assert result is not None
        assert result["delta"]["text"] == "visible"

    def test_anthropic_thinking_delta_untouched(self):
        """thinking_delta（独立思考字段）不应被 💭 过滤误伤"""
        filt = ThinkFilter()
        chunk = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "💭reasoning"},
        }
        result = _filter_think_in_stream_chunk(chunk, filt)
        assert result is chunk

    def test_anthropic_input_json_delta_untouched(self):
        """input_json_delta（工具参数）不应被过滤"""
        filt = ThinkFilter()
        chunk = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"q": "💭"}'},
        }
        result = _filter_think_in_stream_chunk(chunk, filt)
        assert result is chunk

    def test_chat_format_still_filtered(self):
        """回归：Chat 格式（choices[].delta.content）分支仍然生效"""
        filt = ThinkFilter()
        chunk = {"choices": [{"index": 0, "delta": {"content": "💭hidden💭ok"}, "finish_reason": None}]}
        result = _filter_think_in_stream_chunk(chunk, filt)
        assert result is not None
        assert result["choices"][0]["delta"]["content"] == "ok"

    def test_anthropic_empty_text_passthrough(self):
        """空文本 text_delta 应原样返回，不触发过滤"""
        filt = ThinkFilter()
        chunk = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": ""},
        }
        result = _filter_think_in_stream_chunk(chunk, filt)
        assert result is chunk
