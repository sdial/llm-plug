import pytest
from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture
def client():
    from fastapi import FastAPI
    from routers.proxy_response import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_post_responses_streaming(client):
    """POST /v1/responses 流式请求应返回 StreamingResponse"""
    async def mock_stream():
        yield b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\n'
        yield b'data: [DONE]\n\n'

    with patch("routers.proxy_response.proxy_request") as mock_proxy:
        mock_proxy.return_value = (
            mock_stream(),
            MagicMock(id="ch1", name="test", api_type=MagicMock(value="openai-chat-completions")),
        )

        resp = client.post("/v1/responses", json={
            "model": "gpt-4o",
            "input": "Hello",
            "stream": True,
        })
        # 流式响应应返回 200 且 content-type 为 text/event-stream
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


def test_post_responses_basic(client):
    """POST /v1/responses 基本请求"""
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.put = AsyncMock()
        mock_store.get_conversation = AsyncMock(return_value=None)
        mock_store.generate_response_id = MagicMock(return_value="resp_abcdefghijklmnopqrstuvwxyz012345")

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "chatcmpl-123",
                    "choices": [{"message": {"role": "assistant", "content": "Hello!"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
                MagicMock(id="ch1", name="test", api_type=MagicMock(value="openai-chat-completions")),
            )

            resp = client.post("/v1/responses", json={
                "model": "gpt-4o",
                "input": "Hello",
                "stream": False,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["object"] == "response"
            assert "resp_" in data["id"]
