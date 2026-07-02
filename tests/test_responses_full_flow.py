import httpx
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from models.api_types import APIType
from proxy_core import ConverterError


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    import routers.proxy_response as proxy_response
    from routers.proxy_response import router

    monkeypatch.setattr(
        proxy_response,
        "check_proxy_authorization",
        lambda authorization, request_state=None: True,
    )
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

        resp = client.post(
            "/v1/responses",
            json={
                "model": "gpt-4o",
                "input": "Hello",
                "stream": True,
            },
        )

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
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Hello",
                    "stream": False,
                },
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["object"] == "response"
            assert data["id"] == "chatcmpl-123"
            assert data["output"][0]["content"][0]["text"] == "Hello!"
            assert data["usage"]["input_tokens"] == 10
            mock_store.put.assert_awaited_once()


def test_post_responses_saves_function_call_output_as_history_item(client):
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
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc_weather",
                            "call_id": "call_weather",
                            "name": "get_weather",
                            "arguments": '{"location":"Beijing"}',
                            "status": "completed",
                        }
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Search weather",
                },
            )

            assert resp.status_code == 200
            _, conversation, _ = mock_store.put.await_args.args
            assert conversation["messages"] == [
                {"role": "user", "content": "Search weather"},
                {
                    "type": "function_call",
                    "call_id": "call_weather",
                    "name": "get_weather",
                    "arguments": '{"location":"Beijing"}',
                },
            ]


def test_post_responses_with_previous_response_id_expands_history(client):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.get_conversation = AsyncMock(
            return_value={
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi there"},
                ],
                "instructions": "Be terse.",
            }
        )
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
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "total_tokens": 15,
                    },
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "How are you?",
                    "previous_response_id": "resp_1",
                },
            )

            assert resp.status_code == 200
            sent_body = mock_proxy.await_args.args[1]
            assert sent_body["input"] == "How are you?"
            assert sent_body["previous_response_id"] == "resp_1"
            _, conversation, _ = mock_store.put.await_args.args
            assert conversation["instructions"] == "Be terse."
            assert conversation["messages"] == [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
                {"role": "user", "content": "How are you?"},
                {"role": "assistant", "content": "Fine"},
            ]


def test_post_responses_same_type_previous_response_id_passthrough_when_local_state_missing(
    client,
):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.get_conversation = AsyncMock(return_value=None)
        mock_store.put = AsyncMock()

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "resp_remote_2",
                    "object": "response",
                    "created_at": 123,
                    "model": "gpt-4o",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_resp_remote_2",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Remote state worked"}
                            ],
                        }
                    ],
                    "output_text": "Remote state worked",
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "total_tokens": 15,
                    },
                },
                MagicMock(
                    id="ch_resp", name="Responses", api_type=APIType.OPENAI_RESPONSE
                ),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Continue",
                    "previous_response_id": "resp_remote_1",
                },
            )

            assert resp.status_code == 200
            sent_body = mock_proxy.await_args.args[1]
            assert sent_body["input"] == "Continue"
            assert sent_body["previous_response_id"] == "resp_remote_1"
            mock_store.get_conversation.assert_awaited_once_with("resp_remote_1")


def test_post_responses_saves_reasoning_item_in_history(client):
    """reasoning 输出项应保存在历史消息中"""
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.put = AsyncMock()
        mock_store.get_conversation = AsyncMock(return_value=None)

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "resp_reasoning",
                    "object": "response",
                    "created_at": 123,
                    "model": "gpt-4o",
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "rs_abc",
                            "summary": [
                                {"type": "summary_text", "text": "Thinking..."}
                            ],
                        },
                        {
                            "type": "message",
                            "id": "msg_resp_reasoning",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "The answer is 42"}
                            ],
                        },
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "What is the answer?",
                },
            )

            assert resp.status_code == 200
            _, conversation, _ = mock_store.put.await_args.args
            # reasoning 项应出现在历史中
            reasoning_items = [
                m
                for m in conversation["messages"]
                if isinstance(m, dict) and m.get("type") == "reasoning"
            ]
            assert len(reasoning_items) == 1
            assert reasoning_items[0]["id"] == "rs_abc"
            # message 项也应出现
            assistant_items = [
                m
                for m in conversation["messages"]
                if isinstance(m, dict) and m.get("role") == "assistant"
            ]
            assert len(assistant_items) == 1
            assert assistant_items[0]["content"] == "The answer is 42"


def test_previous_response_id_expands_function_call_history(client):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.get_conversation = AsyncMock(
            return_value={
                "messages": [
                    {"role": "user", "content": "Search weather"},
                    {
                        "type": "function_call",
                        "call_id": "call_weather",
                        "name": "get_weather",
                        "arguments": '{"location":"Beijing"}',
                    },
                ],
                "instructions": "",
            }
        )
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
                            "content": [{"type": "output_text", "text": "Sunny"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "total_tokens": 15,
                    },
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "call_weather",
                            "output": "Sunny, 25C",
                        }
                    ],
                    "previous_response_id": "resp_1",
                },
            )

            assert resp.status_code == 200
            sent_body = mock_proxy.await_args.args[1]
            assert sent_body["input"] == [
                {
                    "type": "function_call_output",
                    "call_id": "call_weather",
                    "output": "Sunny, 25C",
                }
            ]
            assert sent_body["previous_response_id"] == "resp_1"
            _, conversation, _ = mock_store.put.await_args.args
            assert conversation["messages"] == [
                {"role": "user", "content": "Search weather"},
                {
                    "type": "function_call",
                    "call_id": "call_weather",
                    "name": "get_weather",
                    "arguments": '{"location":"Beijing"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_weather",
                    "output": "Sunny, 25C",
                },
                {"role": "assistant", "content": "Sunny"},
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

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Hello",
                    "store": False,
                },
            )

            assert resp.status_code == 200
            mock_store.put.assert_not_awaited()


def test_post_responses_hosted_tools_are_degraded_instead_of_400(client):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.get_conversation = AsyncMock(return_value=None)
        mock_store.put = AsyncMock()

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "resp_compat_1",
                    "object": "response",
                    "created_at": 123,
                    "model": "gpt-4o",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_resp_compat_1",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Search fallback answer",
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                },
                MagicMock(id="ch1", name="test", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Search",
                    "tools": [{"type": "web_search"}],
                },
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["output"][0]["content"][0]["text"] == "Search fallback answer"
            mock_store.put.assert_awaited_once()


def test_post_responses_returns_400_for_proxy_core_converter_error(client):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.get_conversation = AsyncMock(return_value=None)
        mock_store.put = AsyncMock()

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.side_effect = ConverterError(
                "请求转换失败: Responses tool 'web_search' is not supported when upstream is Chat Completions"
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Search",
                    "tools": [{"type": "web_search"}],
                },
            )

            assert resp.status_code == 400
            data = resp.json()
            assert data["error"]["type"] == "invalid_request_error"
            assert "web_search" in data["error"]["message"]
            mock_store.put.assert_not_awaited()


def test_post_responses_chat_upstream_compatible_response_is_saved_after_degrade(
    client,
):
    with patch("routers.proxy_response._store") as mock_store:
        mock_store.put = AsyncMock()
        mock_store.get_conversation = AsyncMock(return_value=None)

        with patch("routers.proxy_response.proxy_request") as mock_proxy:
            mock_proxy.return_value = (
                {
                    "id": "resp_saved_after_degrade",
                    "object": "response",
                    "created_at": 123,
                    "model": "gpt-4o",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_resp_saved_after_degrade",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Fallback completed"}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 9,
                        "output_tokens": 5,
                        "total_tokens": 14,
                    },
                },
                MagicMock(id="ch1", name="chat-upstream", api_type=APIType.OPENAI_CHAT),
            )

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Hello",
                    "background": True,
                    "tools": [{"type": "web_search"}],
                },
            )

            assert resp.status_code == 200
            response_id, conversation, response = mock_store.put.await_args.args
            assert response_id == "resp_saved_after_degrade"
            assert conversation["messages"] == [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Fallback completed"},
            ]
            assert response["id"] == "resp_saved_after_degrade"


def test_post_responses_streaming_saves_completed_response(client):
    async def mock_stream():
        yield (
            "event: response.created\n"
            'data: {"type":"response.created","response":{"id":"resp_stream","object":"response","status":"in_progress","model":"gpt-4o","output":[]}}\n\n'
        )
        yield (
            "event: response.completed\n"
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

            resp = client.post(
                "/v1/responses",
                json={
                    "model": "gpt-4o",
                    "input": "Hello",
                    "stream": True,
                },
            )

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


def test_post_responses_all_channels_exhausted_returns_upstream_status(client):
    from proxy_core import AllChannelsExhausted

    upstream_resp = MagicMock()
    upstream_resp.status_code = 429
    upstream_resp.headers = {"content-type": "application/json"}
    upstream_resp.content = b'{"error":{"message":"rate limited"}}'
    upstream_resp.text = '{"error":{"message":"rate limited"}}'
    last_error = httpx.HTTPStatusError(
        "429", request=MagicMock(), response=upstream_resp
    )

    with patch("routers.proxy_response.proxy_request") as mock_proxy:
        mock_proxy.side_effect = AllChannelsExhausted(
            "all channels exhausted", last_error=last_error
        )

        resp = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": "hi"},
        )

        assert resp.status_code == 429


def test_post_responses_all_channels_exhausted_non_http_returns_502(client):
    from proxy_core import AllChannelsExhausted

    with patch("routers.proxy_response.proxy_request") as mock_proxy:
        mock_proxy.side_effect = AllChannelsExhausted(
            "all channels exhausted", last_error=RuntimeError("network down")
        )

        resp = client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": "hi"},
        )

        assert resp.status_code == 502
