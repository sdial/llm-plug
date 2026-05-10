import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from models.api_types import APIType


@pytest.fixture
def client():
    from fastapi import FastAPI
    from routers.proxy_response import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_post_responses_streaming(client):
    async def mock_stream():
        yield b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
        yield b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","object":"response","status":"completed","output":[]}}\n\n'

    with patch("routers.proxy_response.proxy_request") as mock_proxy:
        mock_proxy.return_value = (
            mock_stream(),
            MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
        )

        resp = client.post("/v1/responses", json={
            "model": "gpt-4o",
            "input": "Hello",
            "stream": True,
        })

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


def test_post_responses_basic_uses_proxy_core_converted_response(client):
    """proxy_core already returns Responses format; the route must not convert it again."""
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.put = AsyncMock()
        mock_store.get_conversation = AsyncMock(return_value=None)

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "chatcmpl-123",
                    "object": "response",
                    "created_at": 123,
                    "model": "gpt-4o",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_chatcmpl-123",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Hello!"}],
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post("/v1/responses", json={
                "model": "gpt-4o",
                "input": "Hello",
                "stream": False,
            })

            assert resp.status_code == 200
            data = resp.json()
            assert data["object"] == "response"
            assert data["id"] == "chatcmpl-123"
            assert data["output"][0]["content"][0]["text"] == "Hello!"
            assert data["usage"]["input_tokens"] == 10
            mock_store.put.assert_awaited_once()


def test_post_responses_with_previous_response_id_expands_history(client):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.get_conversation = AsyncMock(return_value={
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
            "instructions": "Be terse.",
        })
        mock_store.put = AsyncMock()

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "resp_2",
                    "object": "response",
                    "created_at": 123,
                    "model": "gpt-4o",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_resp_2",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Fine"}],
                        }
                    ],
                    "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post("/v1/responses", json={
                "model": "gpt-4o",
                "input": "How are you?",
                "previous_response_id": "resp_1",
            })

            assert resp.status_code == 200
            sent_body = mock_proxy.await_args.args[1]
            assert sent_body["instructions"] == "Be terse."
            assert sent_body["input"] == [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
                {"role": "user", "content": "How are you?"},
            ]


def test_post_responses_store_false_does_not_save_state(client):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.put = AsyncMock()
        mock_store.get_conversation = AsyncMock(return_value=None)

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 123,
                    "model": "gpt-4o",
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post("/v1/responses", json={
                "model": "gpt-4o",
                "input": "Hello",
                "store": False,
            })

            assert resp.status_code == 200
            mock_store.put.assert_not_awaited()


def test_post_responses_streaming_saves_completed_response(client):
    async def mock_stream():
        yield (
            'event: response.created\n'
            'data: {"type":"response.created","response":{"id":"resp_stream","object":"response","status":"in_progress","model":"gpt-4o","output":[]}}\n\n'
        )
        yield (
            'event: response.completed\n'
            'data: {"type":"response.completed","response":{"id":"resp_stream","object":"response","created_at":123,"model":"gpt-4o","status":"completed","output":[{"type":"message","id":"msg_resp_stream","status":"completed","role":"assistant","content":[{"type":"output_text","text":"Hello stream"}]}],"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6}}}\n\n'
        )

    with patch("routers.proxy_response._store") as mock_store:
        mock_store.get_conversation = AsyncMock(return_value=None)
        mock_store.put = AsyncMock()

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                mock_stream(),
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post("/v1/responses", json={
                "model": "gpt-4o",
                "input": "Hello",
                "stream": True,
            })

            assert resp.status_code == 200
            assert "event: response.completed" in resp.text
            mock_store.put.assert_awaited_once()
            response_id, conversation, response = mock_store.put.await_args.args
            assert response_id == "resp_stream"
            assert response["output"][0]["content"][0]["text"] == "Hello stream"
            assert conversation["messages"] == [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hello stream"},
            ]
