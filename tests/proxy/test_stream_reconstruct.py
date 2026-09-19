from proxy.stream_reconstruct import build_anthropic_stream_response


def test_reconstruct_missing_start_preserves_thinking_type():
    result = build_anthropic_stream_response(
        [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "reasoning"}},
        ],
        "claude",
    )

    assert result is not None
    assert result["content"] == [{"type": "thinking", "thinking": "reasoning"}]


def test_reconstruct_ignores_non_integer_block_indexes():
    result = build_anthropic_stream_response(
        [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": "0", "delta": {"type": "text_delta", "text": "bad"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "good"}},
        ],
        "claude",
    )

    assert result is not None
    assert result["content"] == [{"type": "text", "text": "good"}]
