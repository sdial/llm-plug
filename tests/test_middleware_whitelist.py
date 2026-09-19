"""WhitelistMiddleware 独立单测（403 seam + 直通 seam）。"""

import json

import pytest
from loguru import logger

import whitelist as _whitelist
from middleware.whitelist_middleware import WhitelistMiddleware
from tests.middleware_test_utils import make_echo_app, make_scope, run_middleware


@pytest.fixture
def captured_logs():
    """捕获 loguru 输出（format="{message}"，records 为 str 列表）。"""
    records = []
    handler_id = logger.add(records.append, format="{message}")
    yield records
    logger.remove(handler_id)


def _install_whitelist(tmp_path, text):
    """写临时白名单 CSV 并替换模块级缓存。"""
    wl_file = tmp_path / "whitelist.csv"
    wl_file.write_text(text, encoding="utf-8")
    import middleware.whitelist_middleware as mod

    mod._whitelist_cache = _whitelist.WhitelistCache(str(wl_file))


class TestWhitelistMiddleware:
    def test_denied_ip_returns_403(self, tmp_path):
        _install_whitelist(tmp_path, "*,*,10.0.0.0/8,allow 10.x only\n")
        records = {}
        app = WhitelistMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(client=("1.2.3.4", 5555)),
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 403
        assert not records  # 下游未被调用
        body = json.loads(sent[1]["body"])
        assert body["error"]["type"] == "ip_whitelist_error"

    def test_denied_ip_on_messages_uses_anthropic_format(self, tmp_path):
        _install_whitelist(tmp_path, "*,*,10.0.0.0/8,allow 10.x only\n")
        records = {}
        app = WhitelistMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(path="/v1/messages", client=("1.2.3.4", 5555)),
            body=json.dumps({"model": "claude"}).encode(),
        )
        assert sent[0]["status"] == 403
        body = json.loads(sent[1]["body"])
        assert body["type"] == "error"
        assert body["error"]["type"] == "permission_error"

    def test_allowed_ip_passes_through(self, tmp_path):
        _install_whitelist(tmp_path, "*,*,10.0.0.0/8,allow 10.x only\n")
        records = {}
        app = WhitelistMiddleware(make_echo_app(records))
        body = json.dumps({"model": "gpt-4o"}).encode()
        sent, _ = run_middleware(
            app,
            make_scope(client=("10.1.2.3", 5555)),
            body=body,
        )
        assert sent[0]["status"] == 200
        assert records["body"] == body

    def test_empty_whitelist_allows_all(self, tmp_path):
        _install_whitelist(tmp_path, "")
        records = {}
        app = WhitelistMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope())
        assert sent[0]["status"] == 200

    def test_normalizes_path_and_sets_state(self, tmp_path):
        _install_whitelist(tmp_path, "")
        records = {}
        app = WhitelistMiddleware(make_echo_app(records))
        scope = make_scope(path="/v1/v1/chat/completions")  # 重复 /v1
        run_middleware(app, scope)
        assert scope["path"] == "/v1/chat/completions"
        assert scope["state"]["client_ip"] == "127.0.0.1"
        assert scope["state"]["original_path"] == "/v1/v1/chat/completions"

    def test_non_http_scope_passes_through(self, tmp_path):
        records = {}
        app = WhitelistMiddleware(make_echo_app(records))
        scope = {"type": "websocket", "path": "/ws"}
        run_middleware(app, scope)
        assert records["body"] == b""

    def test_denied_request_not_logged(self, tmp_path, captured_logs):
        """白名单 403 不产生请求日志（与旧组合类一致）。"""
        _install_whitelist(tmp_path, "*,*,10.0.0.0/8,allow 10.x only\n")
        app = WhitelistMiddleware(make_echo_app({}))
        run_middleware(app, make_scope(client=("1.2.3.4", 5555)))
        assert captured_logs == []
