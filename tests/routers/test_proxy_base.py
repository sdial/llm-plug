import json
import os
import tempfile

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


def test_invalid_json_request_returns_400():
    """测试无效JSON请求返回400错误"""
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"not valid json {",
            headers={"Content-Type": "application/json"}
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

    async def fake_proxy_request(model, request_data, target_api_type, is_stream, **kwargs):
        seen["is_stream"] = is_stream
        if is_stream:
            async def stream():
                yield 'event: message_start\ndata: {"type": "message_start"}\n\n'

            return stream(), channel
        return {"id": "msg_1", "type": "message", "content": []}, channel

    with TestClient(app) as client, patch("routers.proxy_base.proxy_request", fake_proxy_request):
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


def test_upstream_http_error_status_and_body_are_passed_through():
    """上游非重试 HTTP 错误应保持状态码和错误体，不被包装成 502。"""
    from unittest.mock import patch

    request = httpx.Request("POST", "https://upstream.example/v1/chat/completions")
    upstream_response = httpx.Response(
        401,
        json={"error": {"message": "invalid upstream key", "type": "invalid_request_error"}},
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

    with TestClient(app) as client, patch("routers.proxy_base.proxy_request", fake_proxy_request):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 401
    assert response.json() == {"error": {"message": "invalid upstream key", "type": "invalid_request_error"}}


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
    channel = Channel(
        id="ch_openai",
        name="OpenAI",
        api_type=APIType.OPENAI_CHAT,
        base_url="https://api.openai.com",
        api_key="sk-test",
        models=["gpt-4o"],
    )

    async def stream():
        raise upstream_error
        yield ""

    async def fake_proxy_request(*args, **kwargs):
        return stream(), channel

    with TestClient(app) as client, patch("routers.proxy_base.proxy_request", fake_proxy_request):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "bad stream request"}}
