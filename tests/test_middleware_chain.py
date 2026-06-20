"""CombinedMiddleware 完整链路测试

覆盖中间件核心管道的每个环节：
1. IP 白名单检查（403）
2. 请求体大小限制（413：Content-Length 头 + 实际 body 超限）
3. API Key 认证（401：缺失 / 无效）
4. 模型级访问控制（403：model not in allowed_models）
5. 正常请求通过中间件后 proxy_auth_checked 被设置
6. 非代理路径直通（GET / 非 proxy path 不鉴权）
"""

import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_app(tmp_path, monkeypatch):
    """构建一个最小 FastAPI 应用 + CombinedMiddleware 的测试环境。"""
    import config
    import storage
    import whitelist as _whitelist

    # 隔离数据目录
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    channels_data = {
        "channels": [
            {
                "id": "ch_mw_1",
                "name": "MW Test",
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
                "id": "key_mw_1",
                "name": "mw-test-key",
                "key": "sk-middleware-test",
                "allowed_models": [],
            },
            {
                "id": "key_mw_2",
                "name": "mw-restricted-key",
                "key": "sk-middleware-restricted",
                "allowed_models": ["gpt-4o"],
            },
            {
                "id": "key_mw_3",
                "name": "mw-narrow-key",
                "key": "sk-middleware-narrow",
                "allowed_models": ["claude-sonnet-4-20250514"],
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
    # 设置一个较小的 max_body_size 以便测试 413
    monkeypatch.setattr(config, "MAX_BODY_SIZE", 1024)
    import main as _main

    # 清缓存
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None

    # 重置 api key index
    _main._api_key_index = None

    # 创建空白的白名单文件
    wl_file = data_dir / "whitelist.csv"
    wl_file.write_text("")
    _main._whitelist_cache = _whitelist.WhitelistCache(str(wl_file))

    # 构建一个只含中间件 + 一个回显路由的 app
    inner_app = FastAPI()

    @inner_app.post("/v1/chat/completions")
    async def echo_chat(request: Request):
        return JSONResponse({"status": "ok", "auth_checked": getattr(request.state, "proxy_auth_checked", False)})

    @inner_app.post("/v1/messages")
    async def echo_anthropic(request: Request):
        return JSONResponse({"status": "ok", "auth_checked": getattr(request.state, "proxy_auth_checked", False)})

    @inner_app.get("/health")
    async def health():
        return JSONResponse({"status": "healthy"})

    inner_app.add_middleware(_main.CombinedMiddleware)

    with TestClient(inner_app, raise_server_exceptions=False) as client:
        yield client

    # 清理
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None
    _main._api_key_index = None


# ═══════════════════════════════════════════
#  413 — 请求体过大
# ═══════════════════════════════════════════

class TestBodySizeLimit:

    def test_content_length_header_exceeds_max_returns_413(self, middleware_app):
        """Content-Length 超过 MAX_BODY_SIZE 时应返回 413"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            content=b"x" * 2048,
            headers={
                "Content-Length": "2048",
                "Authorization": "Bearer sk-middleware-test",
            },
        )
        assert resp.status_code == 413
        body = resp.json()
        assert "too large" in body["error"]["message"].lower()

    def test_actual_body_exceeds_max_returns_413(self, middleware_app):
        """实际 body 超过限制（无 Content-Length 或与实际不符）也应返回 413"""
        # 发送超过 1024 字节的实际 body
        big_body = b"x" * 2048
        resp = middleware_app.post(
            "/v1/chat/completions",
            content=big_body,
            headers={"Authorization": "Bearer sk-middleware-test"},
        )
        assert resp.status_code == 413

    def test_body_within_limit_passes(self, middleware_app):
        """正常大小的 body 不应被拒绝"""
        small_body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
        resp = middleware_app.post(
            "/v1/chat/completions",
            content=small_body,
            headers={"Authorization": "Bearer sk-middleware-test"},
        )
        assert resp.status_code == 200

    def test_invalid_content_length_is_ignored(self, middleware_app):
        """Content-Length 值非法（非数字）时应跳过检查，走实际 body 大小"""
        small_body = json.dumps({"model": "gpt-4o"})
        resp = middleware_app.post(
            "/v1/chat/completions",
            content=small_body,
            headers={
                "Content-Length": "not-a-number",
                "Authorization": "Bearer sk-middleware-test",
            },
        )
        assert resp.status_code == 200

    def test_chunked_body_exceeds_max_returns_413(self, tmp_path, monkeypatch):
        """无 Content-Length 且 body 分批到达时，应按累计大小返回 413。"""
        import config
        import main as _main
        import storage
        import whitelist as _whitelist

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        channels_file = data_dir / "channels.json"
        api_keys_file = data_dir / "api_keys.json"
        wl_file = data_dir / "whitelist.csv"
        channels_file.write_text(json.dumps({"channels": []}), encoding="utf-8")
        api_keys_file.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
        wl_file.write_text("", encoding="utf-8")

        monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
        monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
        monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))
        monkeypatch.setattr(config, "MAX_BODY_SIZE", 8)
        _main._whitelist_cache = _whitelist.WhitelistCache(str(wl_file))
        _main._api_key_index = None
        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = _main.CombinedMiddleware(app)
        messages = iter(
            [
                {"type": "http.request", "body": b"12345", "more_body": True},
                {"type": "http.request", "body": b"6789", "more_body": False},
            ]
        )
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }

        asyncio.run(middleware(scope, receive, send))

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 413
        assert b"too large" in sent[1]["body"].lower()


# ═══════════════════════════════════════════
#  401 — API Key 认证
# ═══════════════════════════════════════════

class TestApiKeyAuth:

    def test_missing_auth_header_returns_401(self, middleware_app):
        """无 Authorization 或 x-api-key 头应返回 401"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
        )
        assert resp.status_code == 401
        assert "Missing" in resp.json()["error"]["message"] or "invalid" in resp.json()["error"]["message"].lower()

    def test_invalid_bearer_token_returns_401(self, middleware_app):
        """Bearer token 不在已注册的 API Key 中应返回 401"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-nonexistent-key"},
        )
        assert resp.status_code == 401
        assert "Invalid" in resp.json()["error"]["message"] or "invalid" in resp.json()["error"]["message"].lower()

    def test_valid_bearer_token_passes(self, middleware_app):
        """有效的 Bearer token 应通过认证"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-middleware-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["auth_checked"] is True

    def test_x_api_key_auth_passes(self, middleware_app):
        """x-api-key 头也应能通过认证"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"x-api-key": "sk-middleware-test"},
        )
        assert resp.status_code == 200

    def test_empty_bearer_prefix_returns_401(self, middleware_app):
        """Authorization 头不以 Bearer 开头且无 x-api-key 应返回 401"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401


# ═══════════════════════════════════════════
#  403 — 模型级访问控制
# ═══════════════════════════════════════════

class TestModelAccessControl:

    def test_model_not_in_allowed_list_returns_403(self, middleware_app):
        """请求的模型不在 API Key 的 allowed_models 中应返回 403"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-middleware-narrow"},
        )
        assert resp.status_code == 403
        assert "not allowed" in resp.json()["error"]["message"].lower()

    def test_model_in_allowed_list_passes(self, middleware_app):
        """请求的模型在 allowed_models 中应通过"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet-4-20250514"},
            headers={"Authorization": "Bearer sk-middleware-narrow"},
        )
        assert resp.status_code == 200

    def test_empty_allowed_models_allows_all(self, middleware_app):
        """allowed_models 为空列表时应允许所有模型"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "any-model-works"},
            headers={"Authorization": "Bearer sk-middleware-test"},
        )
        assert resp.status_code == 200

    def test_restricted_key_allows_whitelisted_model(self, middleware_app):
        """受限 key + 允许的模型应通过"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-middleware-restricted"},
        )
        assert resp.status_code == 200

    def test_restricted_key_rejects_non_whitelisted_model(self, middleware_app):
        """受限 key + 不允许的模型应返回 403"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo"},
            headers={"Authorization": "Bearer sk-middleware-restricted"},
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════
#  非代理路径直通
# ═══════════════════════════════════════════

class TestNonProxyPaths:

    def test_get_request_passes_through(self, middleware_app):
        """GET 请求不是代理路径，应直通下游"""
        resp = middleware_app.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_non_proxy_post_path_passes_through(self, middleware_app):
        """POST 到非代理路径也应直通"""
        resp = middleware_app.post("/some/other/endpoint")
        # 404 是因为路由不存在，但中间件没有拦截它（不是 401）
        assert resp.status_code in (404, 405)


# ═══════════════════════════════════════════
#  IP 白名单
# ═══════════════════════════════════════════

class TestIpWhitelist:

    def test_whitelist_deny_returns_403(self, tmp_path, monkeypatch):
        """白名单规则拒绝的 IP 应返回 403"""
        import config
        import storage
        import whitelist as _whitelist
        import main as _main

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        channels_file = data_dir / "channels.json"
        api_keys_file = data_dir / "api_keys.json"
        with open(channels_file, "w") as f:
            json.dump({"channels": []}, f)
        with open(api_keys_file, "w") as f:
            json.dump({"api_keys": []}, f)

        monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
        monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
        monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))
        monkeypatch.setattr(config, "MAX_BODY_SIZE", 10 * 1024 * 1024)

        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None

        # 写一个白名单规则：只允许 10.0.0.0/8
        wl_file = data_dir / "whitelist.csv"
        wl_file.write_text("*,*,10.0.0.0/8,allow 10.x only\n")
        _main._whitelist_cache = _whitelist.WhitelistCache(str(wl_file))
        _main._api_key_index = None

        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        inner = FastAPI()

        @inner.post("/v1/chat/completions")
        async def handler(request: Request):
            return JSONResponse({"status": "ok"})

        inner.add_middleware(_main.CombinedMiddleware)

        with TestClient(inner, raise_server_exceptions=False) as client:
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o"},
            )
            # TestClient 的 client IP 是 "testclient"，不在 10.0.0.0/8 范围内
            assert resp.status_code == 403
            assert "whitelist" in resp.json()["error"].get("type", "").lower() or \
                   resp.status_code == 403

        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None
        _main._api_key_index = None


# ═══════════════════════════════════════════
#  proxy_auth_checked 标志
# ═══════════════════════════════════════════

class TestProxyAuthCheckedFlag:

    def test_proxy_auth_checked_set_on_success(self, middleware_app):
        """成功通过认证后，request.state.proxy_auth_checked 应为 True"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-middleware-test"},
        )
        assert resp.status_code == 200
        assert resp.json()["auth_checked"] is True

    def test_proxy_auth_checked_not_set_on_401(self, middleware_app):
        """认证失败时请求不会到达下游，proxy_auth_checked 无从设置"""
        resp = middleware_app.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer sk-wrong-key"},
        )
        assert resp.status_code == 401
