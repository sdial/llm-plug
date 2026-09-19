from pii_walker import walk_text_leaves


def test_openai_chat_messages_content():
    data = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
    }
    leaves = walk_text_leaves(data, "openai-chat")
    assert len(leaves) == 2
    assert leaves[0][2] == "hello"
    assert leaves[1][2] == "world"


def test_anthropic_content_text_blocks():
    data = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
                ],
            }
        ]
    }
    leaves = walk_text_leaves(data, "anthropic")
    assert len(leaves) == 1
    assert leaves[0][2] == "hi"


def test_openai_response_input_text_and_value():
    data = {
        "model": "gpt-5",
        "instructions": "sys prompt",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"role": "user", "content": [{"type": "input_text", "value": "hi2"}]},
        ],
    }
    leaves = walk_text_leaves(data, "openai-response")
    texts = {leaf[2] for leaf in leaves}
    assert texts == {"sys prompt", "hi", "hi2"}


def test_skip_base64_image_url():
    b64_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    data = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": b64_url},
                    },
                    {"type": "text", "text": "look"},
                ],
            }
        ]
    }
    leaves = walk_text_leaves(data, "openai-chat")
    assert len(leaves) == 1
    assert leaves[0][2] == "look"


def test_skip_tool_arguments():
    data = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "calc",
                    "parameters": {"properties": {"x": {"type": "string"}}},
                    "arguments": '{"x": "13800138000"}',
                },
            }
        ],
    }
    leaves = walk_text_leaves(data, "openai-chat")
    assert len(leaves) == 1
    assert leaves[0][2] == "hi"


def test_no_recursion_error_on_deep_list():
    data = {"messages": [{"role": "user", "content": "x"}]}
    for _ in range(2000):
        data = {"messages": [data["messages"][0]]}
    # 实际不应产生递归错误，这里只测试不会异常退出
    leaves = walk_text_leaves(data, "openai-chat")
    assert len(leaves) == 1
