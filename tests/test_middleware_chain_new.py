"""5 个新中间件的链序组合测试：锁定行为锚点。

链序（执行顺序）：Whitelist → AdminAuth → ProxyAuth → BodyBuffer → RequestLog → app。
合成装配 = 后包先执行，故构造顺序 RequestLog 最内、Whitelist 最外。
"""

import json

import pytest
from loguru import logger

import config
import storage
import whitelist as _whitelist
from middleware import proxy_auth_middleware as pam
from middleware.admin_auth_middleware import AdminAuthMiddleware
from middleware.body_buffer_middleware import BodyBufferMiddleware
from middleware.proxy_auth_middleware import ProxyAuthMiddleware
from middleware.request_log_middleware import RequestLogMiddleware
from middleware.whitelist_middleware import WhitelistMiddleware
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


def _install_whitelist(tmp_path, text):
    """写临时白名单 CSV 并替换模块级缓存。"""
    wl_file = tmp_path / "whitelist.csv"
    wl_file.write_text(text, encoding="utf-8")
    import middleware.whitelist_middleware as wmod

    wmod._whitelist_cache = _whitelist.WhitelistCache(str(wl_file))


def _build_chain(records, tmp_path, whitelist_text=""):
    """按链序装配 5 个中间件（后包先执行：RequestLog 最内，Whitelist 最外）。"""
    _install_whitelist(tmp_path, whitelist_text)
    app = make_echo_app(records)
    app = RequestLogMiddleware(app)
    app = BodyBufferMiddleware(app)
    app = ProxyAuthMiddleware(app)
    app = AdminAuthMiddleware(app)
    app = WhitelistMiddleware(app)
    return app


class TestChainAnchors:
    def test_success_logs_exactly_one_req_and_res(self, api_keys_env, captured_logs, tmp_path):
        records = {}
        app = _build_chain(records, tmp_path)
        body = json.dumps({"model": "gpt-4o", "stream": True}).encode()
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-open"}),
            body=body,
        )
        assert sent[0]["status"] == 200
        assert records["body"] == body  # 下游完整收到 body（buffered_receive 恰好重放一次）
        req_lines = [m for m in captured_logs if "[REQ]" in m]
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(req_lines) == 1
        assert len(res_lines) == 1
        assert "-> 200 OK" in res_lines[0]

    def test_auth_failure_401_logs_exactly_one_res(self, api_keys_env, captured_logs, tmp_path):
        records = {}
        app = _build_chain(records, tmp_path)
        sent, _ = run_middleware(app, make_scope(), body=json.dumps({"model": "gpt-4o"}).encode())
        assert sent[0]["status"] == 401
        assert not records  # 下游未被调用
        req_lines = [m for m in captured_logs if "[REQ]" in m]
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(req_lines) == 1  # 401 由 ProxyAuth 记一条
        assert len(res_lines) == 1
        assert "-> 401 ERR" in res_lines[0]

    def test_oversized_body_413_before_auth(self, api_keys_env, captured_logs, tmp_path):
        records = {}
        app = _build_chain(records, tmp_path)
        # 非法 token + 超限 body：413 先于鉴权判定（否则会是 401），且只记一条 413
        sent, _ = run_middleware(
            app,
            make_scope(headers={"Authorization": "Bearer sk-wrong", "Content-Length": "2048"}),
            body=b"x" * 2048,
        )
        assert sent[0]["status"] == 413
        assert not records
        req_lines = [m for m in captured_logs if "[REQ]" in m]
        res_lines = [m for m in captured_logs if "[RES]" in m]
        assert len(req_lines) == 1
        assert len(res_lines) == 1
        assert "-> 413 ERR" in res_lines[0]
        # 413 在解析 model 之前触发：日志 model 为空、stream=False，且无鉴权记录
        assert "model= stream=False" in req_lines[0]

    def test_non_proxy_path_zero_logs(self, api_keys_env, captured_logs, tmp_path):
        records = {}
        app = _build_chain(records, tmp_path)
        sent, _ = run_middleware(app, make_scope(method="GET", path="/health"))
        assert sent[0]["status"] == 200
        assert captured_logs == []

    def test_whitelist_403_no_logs(self, api_keys_env, captured_logs, tmp_path):
        records = {}
        app = _build_chain(records, tmp_path, whitelist_text="*,*,10.0.0.0/8,allow 10.x only\n")
        sent, _ = run_middleware(app, make_scope(client=("1.2.3.4", 5555)))
        assert sent[0]["status"] == 403
        assert not records
        assert captured_logs == []

    def test_admin_401_no_logs(self, api_keys_env, captured_logs, tmp_path):
        records = {}
        app = _build_chain(records, tmp_path)
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin/channels"))
        assert sent[0]["status"] == 401
        assert not records
        assert captured_logs == []
