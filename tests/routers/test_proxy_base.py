import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app
from models.api_types import APIType
from models.channel import Channel


@pytest.fixture(autouse=True)
def setup_test_data():
    """为每个测试设置隔离的数据目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建空的 channels.json 和 api_keys.json
        channels_file = os.path.join(tmpdir, "channels.json")
        api_keys_file = os.path.join(tmpdir, "api_keys.json")

        with open(channels_file, "w") as f:
            json.dump({"channels": []}, f)
        with open(api_keys_file, "w") as f:
            json.dump({"api_keys": []}, f)

        import config
        import storage

        old_data_dir = config.DATA_DIR
        old_channels_file = config.CHANNELS_FILE
        old_api_keys_file = config.API_KEYS_FILE

        config.DATA_DIR = tmpdir
        config.CHANNELS_FILE = channels_file
        config.API_KEYS_FILE = api_keys_file
        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None
        import main

        main._api_key_index = None

        yield

        config.DATA_DIR = old_data_dir
        config.CHANNELS_FILE = old_channels_file
        config.API_KEYS_FILE = old_api_keys_file
        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None
        main._api_key_index = None


def test_invalid_json_request_returns_400():
    """测试无效JSON请求返回400错误"""
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"not valid json {",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "Invalid JSON" in response.text or "invalid" in response.text.lower()


def test_missing_model_returns_error():
    """测试缺失model字段返回错误"""
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        # 应该返回错误（没有渠道支持空模型）
        assert response.status_code in (400, 500)


def test_anthropic_without_stream_defaults_to_non_stream():
    """Anthropic Messages 未显式传 stream 时应保持官方默认的非流式。"""
    from unittest.mock import patch

    channel = Channel(
        id="ch_anthropic",
        name="Anthropic",
        api_type=APIType.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="ak-test",
        models=["claude-3-5-sonnet-20241022"],
    )
    seen = {}

    async def fake_proxy_request(
        model, request_data, target_api_type, is_stream, **kwargs
    ):
        seen["is_stream"] = is_stream
        if is_stream:

            async def stream():
                yield 'event: message_start\ndata: {"type": "message_start"}\n\n'

            return stream(), channel
        return {"id": "msg_1", "type": "message", "content": []}, channel

    with (
        TestClient(app) as client,
        patch("routers.proxy_base.proxy_request", fake_proxy_request),
    ):
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert seen["is_stream"] is False
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_proxy_api_key_auth_uses_cached_index_after_first_load():
    """代理 API Key 校验应复用内存索引，避免每个请求都加载并线性匹配。"""
    import config

    with open(config.API_KEYS_FILE, "w") as f:
        json.dump(
            {
                "api_keys": [
                    {"id": "key_1", "name": "first", "key": "sk-first"},
                    {"id": "key_2", "name": "second", "key": "sk-second"},
                ]
            },
            f,
        )

    channel = Channel(
        id="ch_openai",
        name="OpenAI",
        api_type=APIType.OPENAI_CHAT,
        base_url="https://api.openai.com",
        api_key="sk-upstream",
        models=["gpt-4o"],
    )

    async def fake_proxy_request(*args, **kwargs):
        return {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }, channel

    with (
        TestClient(app) as client,
        patch("routers.proxy_base.proxy_request", fake_proxy_request),
        patch("main.load_api_keys", new_callable=AsyncMock) as load_api_keys,
    ):
        load_api_keys.return_value = json.load(open(config.API_KEYS_FILE))
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"Authorization": "Bearer sk-second"},
        )
        second_response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers={"Authorization": "Bearer sk-second"},
        )

    assert response.status_code == 200
    assert second_response.status_code == 200
    assert load_api_keys.await_count == 1


def test_content_length_over_limit_is_rejected_before_body_read():
    """Content-Length 超限时应提前 413，不继续读取请求体或加载 API keys。"""
    import config

    with open(config.API_KEYS_FILE, "w") as f:
        json.dump({"api_keys": [{"id": "key_1", "name": "test", "key": "sk-test"}]}, f)

    async def exploding_receive():
        raise AssertionError("body should not be read")

    sent = []

    async def capture_send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [
            (b"content-length", str(10**9).encode()),
            (b"authorization", b"Bearer sk-test"),
        ],
        "query_string": b"",
    }

    from main import CombinedMiddleware

    async def app(scope, receive, send):
        raise AssertionError("downstream app should not be called")

    with patch("main.load_api_keys", new_callable=AsyncMock) as load_api_keys:
        import anyio

        anyio.run(CombinedMiddleware(app), scope, exploding_receive, capture_send)

    assert sent[0]["status"] == 413
    load_api_keys.assert_not_awaited()


def test_body_stream_over_limit_is_logged_as_413():
    """分块读取时超限也应记录 413 日志。"""
    logged = []
    from config import MAX_BODY_SIZE

    async def receive():
        return {"type": "http.request", "body": b"x" * (MAX_BODY_SIZE + 1), "more_body": False}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"authorization", b"Bearer sk-test")],
        "query_string": b"",
    }

    from main import CombinedMiddleware

    async def downstream(scope, receive, send):
        raise AssertionError("downstream app should not be called")

    middleware = CombinedMiddleware(downstream)
    with (
        patch("main.load_api_keys", new_callable=AsyncMock, return_value={"api_keys": []}),
        patch.object(middleware, "_log_request", side_effect=lambda *args: logged.append(args)),
    ):
        import anyio

        anyio.run(middleware, scope, receive, send)

    assert logged and logged[0][7] == 413


def test_exception_before_response_start_is_logged_as_500():
    """下游异常且未发 response.start 时，日志状态不应默认为 200。"""
    sent = []
    logged = []

    async def receive():
        return {"type": "http.request", "body": b'{"model":"gpt-4o"}', "more_body": False}

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        raise RuntimeError("boom")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
        "query_string": b"",
    }

    from main import CombinedMiddleware

    middleware = CombinedMiddleware(app)
    with (
        patch("main.load_api_keys", new_callable=AsyncMock, return_value={"api_keys": []}),
        patch.object(middleware, "_log_request", side_effect=lambda *args: logged.append(args)),
    ):
        import anyio

        with pytest.raises(RuntimeError):
            anyio.run(middleware, scope, receive, send)

    assert logged[0][7] == 500


def test_upstream_http_error_status_and_body_are_passed_through():
    """上游非重试 HTTP 错误应保持状态码和错误体，不被包装成 502。"""
    from unittest.mock import patch

    request = httpx.Request("POST", "https://upstream.example/v1/chat/completions")
    upstream_response = httpx.Response(
        401,
        json={
            "error": {
                "message": "invalid upstream key",
                "type": "invalid_request_error",
            }
        },
        request=request,
        headers={"content-type": "application/json"},
    )
    upstream_error = httpx.HTTPStatusError(
        "Client error '401 Unauthorized'",
        request=request,
        response=upstream_response,
    )

    async def fake_proxy_request(*args, **kwargs):
        raise upstream_error

    with (
        TestClient(app) as client,
        patch("routers.proxy_base.proxy_request", fake_proxy_request),
    ):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "error": {"message": "invalid upstream key", "type": "invalid_request_error"}
    }


def test_stream_upstream_http_error_before_first_chunk_is_passed_through():
    """流式请求首个 chunk 前的上游 400 应返回 HTTP 400，而不是 200 SSE error。"""
    from unittest.mock import patch

    request = httpx.Request("POST", "https://upstream.example/v1/chat/completions")
    upstream_response = httpx.Response(
        400,
        json={"error": {"message": "bad stream request"}},
        request=request,
        headers={"content-type": "application/json"},
    )
    upstream_error = httpx.HTTPStatusError(
        "Client error '400 Bad Request'",
        request=request,
        response=upstream_response,
    )
    async def fake_proxy_request(*args, **kwargs):
        raise upstream_error

    with (
        TestClient(app) as client,
        patch("routers.proxy_base.proxy_request", fake_proxy_request),
    ):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "bad stream request"}}


def test_stream_response_is_not_primed_again_at_router_layer():
    """proxy_core 已完成流式预取，路由层不应再次消费首个 chunk。"""
    import anyio
    from starlette.requests import Request

    from routers.proxy_base import make_proxy_router

    channel = Channel(
        id="ch_openai",
        name="OpenAI",
        api_type=APIType.OPENAI_CHAT,
        base_url="https://api.openai.com",
        api_key="sk-test",
        models=["gpt-4o"],
    )
    consumed_before_response = 0

    async def stream():
        nonlocal consumed_before_response
        consumed_before_response += 1
        yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'

    async def fake_proxy_request(*args, **kwargs):
        return stream(), channel

    async def call_endpoint():
        body = json.dumps({
            "model": "gpt-4o",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/test",
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
                "scheme": "http",
                "state": {"proxy_auth_checked": True},
            },
            receive=receive,
        )
        endpoint = make_proxy_router("/test", APIType.OPENAI_CHAT).routes[0].endpoint
        with patch("routers.proxy_base.proxy_request", fake_proxy_request):
            return await endpoint(request)

    response = anyio.run(call_endpoint)

    assert consumed_before_response == 0
    assert response.media_type == "text/event-stream"
