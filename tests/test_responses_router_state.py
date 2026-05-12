import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch


def test_response_input_to_items_uses_router_path():
    from routers.proxy_response import _input_to_items

    assert _input_to_items("Hello") == [{"role": "user", "content": "Hello"}]
    assert _input_to_items(["Hi", {"role": "assistant", "content": "Hello"}]) == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]


def test_save_response_state_appends_request_and_response_items():
    from routers.proxy_response import _save_response_state

    response = {
        "id": "resp_2",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Fine"}],
            },
            {
                "type": "function_call",
                "call_id": "call_weather",
                "name": "get_weather",
                "arguments": '{"city":"Beijing"}',
            },
        ],
    }
    previous_conversation = {
        "messages": [{"role": "user", "content": "Hello"}],
        "instructions": "Be terse.",
    }

    with patch("routers.proxy_response._store") as mock_store:
        mock_store.put = AsyncMock()

        asyncio.run(
            _save_response_state(
                {"input": "How are you?"},
                previous_conversation,
                response,
            )
        )

        response_id, conversation, saved_response = mock_store.put.await_args.args
        assert response_id == "resp_2"
        assert saved_response is response
        assert conversation["instructions"] == "Be terse."
        assert conversation["messages"] == [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "Fine"},
            {
                "type": "function_call",
                "call_id": "call_weather",
                "name": "get_weather",
                "arguments": '{"city":"Beijing"}',
            },
        ]


def test_save_response_state_generates_missing_response_id():
    from routers.proxy_response import _save_response_state

    response = {"output": []}

    with patch("routers.proxy_response._store") as mock_store:
        mock_store.generate_response_id.return_value = "resp_generated"
        mock_store.put = AsyncMock()

        asyncio.run(_save_response_state({"input": "Hello"}, None, response))

        assert response["id"] == "resp_generated"
        mock_store.put.assert_awaited_once()


def test_legacy_responses_handler_module_removed():
    assert not (Path(__file__).parents[1] / "responses_handler.py").exists()
