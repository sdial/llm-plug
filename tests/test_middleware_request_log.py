"""RequestLogMiddleware 独立单测（日志捕获 seam：成功 / 下游异常 / 非代理直通）。"""

import asyncio
import json

import pytest
from loguru import logger

from middleware.request_log_middleware import RequestLogMiddleware
from tests.middleware_test_utils import make_scope, run_middleware


@pytest.fixture
def captured_logs():
    records = []
    handler_id = logger.add(records.append, format="{message}")
    yield records
    logger.remove(handler_id)


def _ok_app(scope, receive, send):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


class TestRequestLogMiddleware:
    def test_logs_request_and_response(self, captured_logs):
        scope = make_scope()
        scope["state"] = {"model": "gpt-4o", "stream": True}
        app = RequestLogMiddleware(_ok_app(None, None, None))
        run_middleware(
            app,
            scope,
            body=json.dumps({"model": "gpt-4o", "stream": True}).encode(),
        )
        req_lines = [m for m in captured_logs if "[REQ]" in m]
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(req_lines) == 1
        assert len(res_lines) == 1
        assert "POST /v1/chat/completions" in req_lines[0]
        assert "model=gpt-4o" in req_lines[0]
        assert "stream=True" in req_lines[0]
        assert "-> 200 OK" in res_lines[0]

    def test_logs_channel_and_original_path(self, captured_logs):
        # Whitelist 归一化后 scope["path"] 为 /v1/...，state 保留原始路径
        scope = make_scope(path="/v1/chat/completions")
        scope["state"] = {
            "model": "gpt-4o",
            "stream": False,
            "selected_channel_name": "ch-1",
            "original_path": "//v1/chat/completions",
        }
        app = RequestLogMiddleware(_ok_app(None, None, None))
        run_middleware(app, scope)
        req_lines = [m for m in captured_logs if "[REQ]" in m]
        assert "channel=ch-1" in req_lines[0]
        assert "original=//v1/chat/completions" in req_lines[0]

    def test_logs_500_when_downstream_raises(self, captured_logs):
        async def boom(scope, receive, send):
            raise RuntimeError("boom")

        scope = make_scope()
        scope["state"] = {"model": "", "stream": False}
        app = RequestLogMiddleware(boom)
        with pytest.raises(RuntimeError):
            run_middleware(app, scope)
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(res_lines) == 1
        assert "-> 500 ERR" in res_lines[0]

    def test_non_proxy_path_not_logged(self, captured_logs):
        scope = make_scope(method="GET", path="/health")
        app = RequestLogMiddleware(_ok_app(None, None, None))
        run_middleware(app, scope)
        assert captured_logs == []

    def test_non_http_scope_passes_through(self, captured_logs):
        async def app(scope, receive, send):
            await send({"type": "websocket.accept"})

        mw = RequestLogMiddleware(app)
        scope = {"type": "websocket", "path": "/ws"}
        asyncio_msgs = []

        async def receive():
            return {"type": "websocket.receive", "text": ""}

        async def send(message):
            asyncio_msgs.append(message)

        async def _run():
            await mw(scope, receive, send)

        asyncio.run(_run())
        assert asyncio_msgs == [{"type": "websocket.accept"}]
        assert captured_logs == []
