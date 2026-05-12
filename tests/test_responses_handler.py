import pytest
from responses_handler import (
    parse_responses_request,
    build_input_messages,
    generate_response_id,
)


def test_parse_basic_request():
    body = {
        "model": "gpt-4o",
        "input": "Hello",
        "instructions": "You are helpful.",
        "stream": False,
    }
    result = parse_responses_request(body)
    assert result["model"] == "gpt-4o"
    assert result["instructions"] == "You are helpful."
    assert result["stream"] is False
    assert result["previous_response_id"] is None


def test_parse_request_with_previous_id():
    body = {
        "model": "gpt-4o",
        "input": "Follow up",
        "previous_response_id": "resp_abc123",
    }
    result = parse_responses_request(body)
    assert result["previous_response_id"] == "resp_abc123"


def test_parse_request_missing_model():
    body = {"input": "Hello"}
    with pytest.raises(ValueError, match="model"):
        parse_responses_request(body)


def test_parse_request_missing_input():
    body = {"model": "gpt-4o"}
    with pytest.raises(ValueError, match="input"):
        parse_responses_request(body)


def test_build_input_messages_string():
    messages = build_input_messages("Hello")
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello"


def test_build_input_messages_list():
    input_list = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "How are you?"},
    ]
    messages = build_input_messages(input_list)
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[2]["content"] == "How are you?"


def test_build_input_messages_with_developer():
    input_list = [
        {"role": "developer", "content": "Be helpful"},
        {"role": "user", "content": "Hello"},
    ]
    messages = build_input_messages(input_list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "Be helpful"


def test_generate_response_id():
    rid = generate_response_id()
    assert rid.startswith("resp_")
    assert len(rid) == 29
