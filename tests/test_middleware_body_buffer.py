"""BodyBufferMiddleware 独立单测（413 / 缓冲 / 非代理直通 / 已缓冲复用 seam）。"""

import asyncio
import json

import pytest

import config
from middleware.body_buffer_middleware import BodyBufferMiddleware
from tests.middleware_test_utils import make_echo_app, make_scope, run_middleware


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 1024)
    yield


class TestBodyBufferMiddleware:
    def test_buffers_and_passes_body_downstream(self, small_limit):
        records = {}
        app = BodyBufferMiddleware(make_echo_app(records))
        body = json.dumps({"model": "gpt-4o"}).encode()
        sent, scope = run_middleware(app, make_scope(), body=body)
        assert sent[0]["status"] == 200
        assert records["body"] == body
        assert scope["state"]["body_bytes"] == body
        assert scope["state"]["model"] == "gpt-4o"
        assert scope["state"]["stream"] is False

    def test_chunked_body_reassembled(self, small_limit):
        records = {}
        app = BodyBufferMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(), chunks=[b"hello-", b"world"])
        assert sent[0]["status"] == 200
        assert records["body"] == b"hello-world"

    def test_content_length_over_limit_returns_413(self, small_limit):
        records = {}
        app = BodyBufferMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Content-Length": "2048"}),
            body=b"x" * 2048,
        )
        assert sent[0]["status"] == 413
        assert not records  # 下游未被调用

    def test_actual_body_over_limit_returns_413(self, small_limit):
        records = {}
        app = BodyBufferMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(), body=b"x" * 2048)
        assert sent[0]["status"] == 413
        assert not records

    def test_invalid_content_length_ignored(self, small_limit):
        records = {}
        app = BodyBufferMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Content-Length": "not-a-number"}),
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 200

    def test_non_proxy_path_passes_through(self, small_limit):
        records = {}
        app = BodyBufferMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/health"))
        assert sent[0]["status"] == 200

    def test_already_buffered_body_forwarded(self, small_limit):
        """上游已缓冲 body 到 state 时，透传上游 receive，不再自行读取。"""
        body = json.dumps({"model": "gpt-4o"}).encode()
        records = {}
        app = BodyBufferMiddleware(make_echo_app(records))
        scope = make_scope()
        scope["state"] = {"body_bytes": body, "model": "gpt-4o", "stream": False}

        calls = []
        received = []

        # 模拟上游 ProxyAuth 的 buffered_receive：首次重放 body，后续空
        async def receive():
            calls.append(1)
            if len(calls) == 1:
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            received.append(message)

        async def _run():
            await app(scope, receive, send)

        asyncio.run(_run())
        assert received[0]["status"] == 200
        assert records["body"] == body
        # BodyBuffer 只透传一次 receive（下游读完即停），未自行重读
        assert len(calls) == 1
