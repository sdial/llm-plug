"""ProxyAuthMiddleware 独立单测（401 / 403 / allowed_models / 413 / 直通 seam）。"""

import json

import pytest
from loguru import logger

import config
import storage
from middleware import proxy_auth_middleware as pam
from middleware.proxy_auth_middleware import ProxyAuthMiddleware
from tests.middleware_test_utils import make_echo_app, make_scope, run_middleware


@pytest.fixture
def captured_logs():
    """捕获 loguru 输出（format="{message}"，records 为 str 列表）。"""
    records = []
    handler_id = logger.add(records.append, format="{message}")
    yield records
    logger.remove(handler_id)


@pytest.fixture
def api_keys_env(tmp_path, monkeypatch):
    """构建带 API Key 的隔离环境，并重置中间件 API key 索引。"""
    keys_file = tmp_path / "api_keys.json"
    keys_data = {
        "api_keys": [
            {"id": "k1", "name": "open-key", "key": "sk-open", "allowed_models": []},
            {
                "id": "k2",
                "name": "restricted",
                "key": "sk-restricted",
                "allowed_models": ["gpt-4o"],
            },
        ]
    }
    keys_file.write_text(json.dumps(keys_data), encoding="utf-8")
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_file))
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 1024)
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    pam._api_key_index = None
    yield
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    pam._api_key_index = None


class TestProxyAuthMiddleware:
    def test_missing_auth_returns_401(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(),
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 401
        assert not records
        body = json.loads(sent[1]["body"])
        assert body["error"]["type"] == "auth_error"

    def test_missing_auth_on_messages_uses_anthropic_format(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(path="/v1/messages"),
            body=json.dumps({"model": "claude"}).encode(),
        )
        assert sent[0]["status"] == 401
        body = json.loads(sent[1]["body"])
        assert body["type"] == "error"
        assert body["error"]["type"] == "authentication_error"

    def test_invalid_token_returns_401(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-nope"}),
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 401

    def test_empty_bearer_prefix_returns_401(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Authorization": "Basic dXNlcjpwYXNz"}),
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 401

    def test_valid_token_passes_and_sets_state(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        body = json.dumps({"model": "gpt-4o"}).encode()
        sent, scope = run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-open"}),
            body=body,
        )
        assert sent[0]["status"] == 200
        assert scope["state"]["proxy_auth_checked"] is True
        assert scope["state"]["body_bytes"] == body
        assert scope["state"]["api_key_id"] == "open-key"
        assert records["body"] == body

    def test_x_api_key_header_passes(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"x-api-key": "sk-open"}),
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 200

    def test_allowed_models_denied_returns_403(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-restricted"}),
            body=json.dumps({"model": "gpt-3.5-turbo"}).encode(),
        )
        assert sent[0]["status"] == 403
        assert not records
        body = json.loads(sent[1]["body"])
        assert "not allowed" in body["error"]["message"].lower()

    def test_allowed_models_allowed_passes(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-restricted"}),
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 200

    def test_restricted_key_cannot_access_model_less_response_lifecycle_endpoint(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(path="/v1/responses/resp_123", headers={"Authorization": "Bearer sk-restricted"}),
            body=b"",
        )

        assert sent[0]["status"] == 403
        assert not records

    def test_no_api_keys_means_open_proxy(self, tmp_path, monkeypatch):
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
        monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_file))
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        pam._api_key_index = None
        try:
            records = {}
            app = ProxyAuthMiddleware(make_echo_app(records))
            sent, scope = run_middleware(app, make_scope(), body=json.dumps({"model": "gpt-4o"}).encode())
            assert sent[0]["status"] == 200
            assert scope["state"]["proxy_auth_checked"] is True
        finally:
            storage._keys_cache = None
            storage._keys_cache_ts = 0
            pam._api_key_index = None

    def test_content_length_over_limit_returns_413(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(
                headers={
                    "Authorization": "Bearer sk-open",
                    "Content-Length": "2048",
                }
            ),
            body=b"x" * 2048,
        )
        assert sent[0]["status"] == 413

    def test_chunked_body_over_limit_returns_413(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-open"}),
            chunks=[b"x" * 700, b"y" * 700],
        )
        assert sent[0]["status"] == 413

    def test_non_proxy_path_passes_through(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(method="GET", path="/health"),
        )
        assert sent[0]["status"] == 200

    def test_response_subresource_path_is_protected(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(
            app,
            make_scope(method="GET", path="/v1/responses/resp_123"),
        )
        assert sent[0]["status"] == 401

    def test_non_utf8_header_bytes_do_not_crash(self, api_keys_env):
        records = {}
        app = ProxyAuthMiddleware(make_echo_app(records))
        # X-Custom 头含 0xE9（单字节非法 UTF-8），headers 按 latin-1 解码不应崩溃
        scope = make_scope(headers={"Authorization": "Bearer sk-open"})
        scope["headers"].append((b"x-custom", b"caf\xe9"))
        sent, _ = run_middleware(
            app,
            scope,
            body=json.dumps({"model": "gpt-4o"}).encode(),
        )
        assert sent[0]["status"] == 200


class TestLogOnFailure:
    """认证失败 / 413 路径照常产生 [REQ]/[RES] 日志（与旧组合类一致）。"""

    def test_missing_auth_logs_401(self, api_keys_env, captured_logs):
        app = ProxyAuthMiddleware(make_echo_app({}))
        run_middleware(app, make_scope(), body=json.dumps({"model": "gpt-4o"}).encode())
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(res_lines) == 1
        assert "-> 401 ERR" in res_lines[0]

    def test_forbidden_model_logs_403(self, api_keys_env, captured_logs):
        app = ProxyAuthMiddleware(make_echo_app({}))
        run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-restricted"}),
            body=json.dumps({"model": "gpt-3.5-turbo"}).encode(),
        )
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(res_lines) == 1
        assert "-> 403 ERR" in res_lines[0]

    def test_oversized_body_logs_413(self, api_keys_env, captured_logs):
        app = ProxyAuthMiddleware(make_echo_app({}))
        run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-open"}),
            body=b"x" * 2048,
        )
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(res_lines) == 1
        assert "-> 413 ERR" in res_lines[0]
