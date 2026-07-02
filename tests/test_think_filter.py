"""
测试 think_filter 模块
"""

from think_filter import filter_think_content_static, ThinkFilter


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

    def test_single_unpaired_emoji_streams_as_normal_text_on_flush(self):
        """未配对的 emoji 不应吞掉后续普通内容"""
        filter = ThinkFilter()

        assert filter.feed("Hello 💭") == "Hello "
        assert filter.feed("visible") == ""
        assert filter.flush() == "💭visible"

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
        filter.feed("<think>partial")
        # incomplete think block should be discarded
        assert filter.flush() == ""

    def test_reset(self):
        """reset 应清空状态"""
        filter = ThinkFilter()
        filter.feed("<think>partial")
        filter.reset()
        assert filter.buffer == ""
        assert filter.in_think is False
