"""路径归一化测试

覆盖 ASGI 层路径去重：
1. normalize_path 函数单元测试（/v1/v1/* → /v1/*）
2. CombinedMiddleware 端到端集成测试（重复 /v1 路径仍能正确路由和鉴权）
"""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


# ═══════════════════════════════════════════
#  单元测试：normalize_path 函数
# ═══════════════════════════════════════════

class TestNormalizePath:

    def test_double_v1_messages(self):
        """/v1/v1/messages → /v1/messages"""
        from main import normalize_path
        assert normalize_path("/v1/v1/messages") == "/v1/messages"

    def test_double_v1_chat_completions(self):
        """/v1/v1/chat/completions → /v1/chat/completions"""
        from main import normalize_path
        assert normalize_path("/v1/v1/chat/completions") == "/v1/chat/completions"

    def test_double_v1_responses(self):
        """/v1/v1/responses → /v1/responses"""
        from main import normalize_path
        assert normalize_path("/v1/v1/responses") == "/v1/responses"

    def test_double_v1_models(self):
        """/v1/v1/models → /v1/models"""
        from main import normalize_path
        assert normalize_path("/v1/v1/models") == "/v1/models"

    def test_triple_v1_messages(self):
        """/v1/v1/v1/messages → /v1/messages"""
        from main import normalize_path
        assert normalize_path("/v1/v1/v1/messages") == "/v1/messages"

    def test_single_v1_unchanged(self):
        """/v1/messages 保持不变"""
        from main import normalize_path
        assert normalize_path("/v1/messages") == "/v1/messages"

    def test_non_proxy_path_unchanged(self):
        """非代理路径不受影响"""
        from main import normalize_path
        assert normalize_path("/admin") == "/admin"
        assert normalize_path("/health") == "/health"
        assert normalize_path("/admin/channels") == "/admin/channels"

    def test_root_path_unchanged(self):
        """根路径保持不变"""
        from main import normalize_path
        assert normalize_path("/") == "/"

    def test_double_v1_anthropic_models(self):
        """/v1/v1/anthropic/models → /v1/anthropic/models"""
        from main import normalize_path
        assert normalize_path("/v1/v1/anthropic/models") == "/v1/anthropic/models"

    # ── 裸路径支持 ──

    def test_bare_messages(self):
        """/messages → /v1/messages"""
        from main import normalize_path
        assert normalize_path("/messages") == "/v1/messages"

    def test_bare_chat_completions(self):
        """/chat/completions → /v1/chat/completions"""
        from main import normalize_path
        assert normalize_path("/chat/completions") == "/v1/chat/completions"

    def test_bare_responses(self):
        """/responses → /v1/responses"""
        from main import normalize_path
        assert normalize_path("/responses") == "/v1/responses"

    def test_bare_responses_with_id(self):
        """/responses/resp_abc123 → /v1/responses/resp_abc123"""
        from main import normalize_path
        assert normalize_path("/responses/resp_abc123") == "/v1/responses/resp_abc123"

    def test_bare_models(self):
        """/models → /v1/models"""
        from main import normalize_path
        assert normalize_path("/models") == "/v1/models"

    def test_bare_anthropic_models(self):
        """/anthropic/models → /v1/anthropic/models"""
        from main import normalize_path
        assert normalize_path("/anthropic/models") == "/v1/anthropic/models"

    def test_unknown_bare_path_unchanged(self):
        """未知的裸路径不应被补 /v1"""
        from main import normalize_path
        assert normalize_path("/admin") == "/admin"
        assert normalize_path("/health") == "/health"
        assert normalize_path("/some/random/path") == "/some/random/path"

    # ── 尾部斜杠 & 多重 v1 组合 ──

    def test_double_v1_with_trailing_slash(self):
        """/v1/v1/chat/completions/ → /v1/chat/completions/"""
        from main import normalize_path
        assert normalize_path("/v1/v1/chat/completions/") == "/v1/chat/completions/"

    def test_triple_v1_responses(self):
        """/v1/v1/v1/responses → /v1/responses"""
        from main import normalize_path
        assert normalize_path("/v1/v1/v1/responses") == "/v1/responses"

    def test_triple_v1_with_trailing_slash(self):
        """/v1/v1/v1/chat/completions/ → /v1/chat/completions/"""
        from main import normalize_path
        assert normalize_path("/v1/v1/v1/chat/completions/") == "/v1/chat/completions/"

    def test_bare_path_with_trailing_slash(self):
        """/chat/completions/ → /v1/chat/completions/"""
        from main import normalize_path
        assert normalize_path("/chat/completions/") == "/v1/chat/completions/"

    def test_single_v1_with_trailing_slash_unchanged(self):
        """/v1/messages/ 保持不变（已有 /v1 前缀）"""
        from main import normalize_path
        assert normalize_path("/v1/messages/") == "/v1/messages/"


# ═══════════════════════════════════════════
#  集成测试：CombinedMiddleware 路径归一化
# ═══════════════════════════════════════════

@pytest.fixture
def middleware_app(tmp_path, monkeypatch):
    """构建一个最小 FastAPI 应用 + CombinedMiddleware 的测试环境。"""
    import config
    import storage
    import whitelist as _whitelist
    import main as _main

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    channels_data = {
        "channels": [
            {
                "id": "ch_pn_1",
                "name": "PN Test",
                "api_type": "openai-chat-completions",
                "base_url": "http://127.0.0.1:19876",
                "api_key": "test-key",
                "models": ["gpt-4o"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-06-01T00:00:00Z",
            }
        ]
    }

    api_keys_data = {
        "api_keys": [
            {
                "id": "key_pn_1",
                "name": "pn-test-key",
                "key": "sk-pn-test",
                "allowed_models": [],
            },
        ]
    }
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)
    with open(api_keys_file, "w") as f:
        json.dump(api_keys_data, f)

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 10 * 1024 * 1024)
    monkeypatch.setattr(_main, "MAX_BODY_SIZE", 10 * 1024 * 1024)

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None
    _main._api_key_index = None

    wl_file = data_dir / "whitelist.csv"
    wl_file.write_text("")
    _main._whitelist_cache = _whitelist.WhitelistCache(str(wl_file))

    inner_app = FastAPI()

    @inner_app.post("/v1/chat/completions")
    async def echo_chat(request: Request):
        return JSONResponse({"route": "chat", "auth_checked": getattr(request.state, "proxy_auth_checked", False)})

    @inner_app.post("/v1/messages")
    async def echo_anthropic(request: Request):
        return JSONResponse({"route": "anthropic", "auth_checked": getattr(request.state, "proxy_auth_checked", False)})

    @inner_app.post("/v1/responses")
    async def echo_responses(request: Request):
        return JSONResponse({"route": "responses", "auth_checked": getattr(request.state, "proxy_auth_checked", False)})

    inner_app.add_middleware(_main.CombinedMiddleware)

    with TestClient(inner_app, raise_server_exceptions=False) as client:
        yield client

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None
    _main._api_key_index = None


class TestMiddlewarePathNormalization:

    def test_double_v1_chat_completions_routes_correctly(self, middleware_app):
        """/v1/v1/chat/completions 应被归一化并路由到 /v1/chat/completions"""
        resp = middleware_app.post(
            "/v1/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "chat"
        assert resp.json()["auth_checked"] is True

    def test_double_v1_messages_routes_correctly(self, middleware_app):
        """/v1/v1/messages 应被归一化并路由到 /v1/messages"""
        resp = middleware_app.post(
            "/v1/v1/messages",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "anthropic"
        assert resp.json()["auth_checked"] is True

    def test_double_v1_responses_routes_correctly(self, middleware_app):
        """/v1/v1/responses 应被归一化并路由到 /v1/responses"""
        resp = middleware_app.post(
            "/v1/v1/responses",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "responses"
        assert resp.json()["auth_checked"] is True

    def test_triple_v1_chat_completions_routes_correctly(self, middleware_app):
        """/v1/v1/v1/chat/completions 也应被归一化"""
        resp = middleware_app.post(
            "/v1/v1/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "chat"

    def test_single_v1_still_works(self, middleware_app):
        """正常的 /v1/chat/completions 不受影响"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "chat"

    def test_double_v1_without_auth_returns_401(self, middleware_app):
        """重复路径同样需要认证，未认证应返回 401"""
        resp = middleware_app.post(
            "/v1/v1/chat/completions",
            json={"model": "gpt-4o"},
        )
        assert resp.status_code == 401

    # ── 裸路径集成 ──

    def test_bare_chat_completions_routes_correctly(self, middleware_app):
        """/chat/completions 应被补 /v1 并路由到对应 handler"""
        resp = middleware_app.post(
            "/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "chat"
        assert resp.json()["auth_checked"] is True

    def test_bare_messages_routes_correctly(self, middleware_app):
        """/messages 应被补 /v1 并路由到 Anthropic handler"""
        resp = middleware_app.post(
            "/messages",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "anthropic"
        assert resp.json()["auth_checked"] is True

    def test_bare_responses_routes_correctly(self, middleware_app):
        """/responses 应被补 /v1 并路由到 Responses handler"""
        resp = middleware_app.post(
            "/responses",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-pn-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["route"] == "responses"
        assert resp.json()["auth_checked"] is True

    def test_bare_path_without_auth_returns_401(self, middleware_app):
        """裸路径同样需要认证，未认证应返回 401"""
        resp = middleware_app.post(
            "/chat/completions",
            json={"model": "gpt-4o"},
        )
        assert resp.status_code == 401
