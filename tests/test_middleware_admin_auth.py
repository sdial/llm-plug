"""AdminAuthMiddleware 独立单测（401 / 302 / 豁免 seam）。"""

import pytest
from loguru import logger

from middleware.admin_auth_middleware import AdminAuthMiddleware
from tests.middleware_test_utils import make_echo_app, make_scope, run_middleware


@pytest.fixture
def captured_logs():
    """捕获 loguru 输出（format="{message}"，records 为 str 列表）。"""
    records = []
    handler_id = logger.add(records.append, format="{message}")
    yield records
    logger.remove(handler_id)


class TestAdminAuthMiddleware:
    def test_protected_admin_path_without_session_returns_401(self):
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin/channels"))
        assert sent[0]["status"] == 401
        assert not records  # 下游未被调用
        body = sent[1]["body"]
        assert b"Admin login required" in body

    def test_admin_root_without_session_redirects_to_login(self):
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin"))
        assert sent[0]["status"] == 302
        headers = dict(sent[0]["headers"])
        assert headers[b"location"] == b"/admin/login"

    def test_admin_slash_without_session_redirects(self):
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin/"))
        assert sent[0]["status"] == 302

    def test_exempt_login_page_passes(self):
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin/login"))
        assert sent[0]["status"] == 200

    def test_exempt_auth_prefix_passes(self):
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin/auth/status"))
        assert sent[0]["status"] == 200

    def test_exempt_static_prefix_passes(self):
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin/static/x.js"))
        assert sent[0]["status"] == 200

    def test_valid_session_passes(self, monkeypatch):
        import admin_auth

        async def fake_validate(token):
            return True

        monkeypatch.setattr(admin_auth, "validate_admin_session", fake_validate)
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/admin/channels"))
        assert sent[0]["status"] == 200
        assert records["body"] == b""

    def test_non_admin_path_passes(self):
        records = {}
        app = AdminAuthMiddleware(make_echo_app(records))
        sent, _ = run_middleware(app, make_scope(method="GET", path="/health"))
        assert sent[0]["status"] == 200

    def test_failed_admin_auth_not_logged(self, captured_logs):
        """admin 401/302 不产生请求日志（与旧组合类一致）。"""
        app = AdminAuthMiddleware(make_echo_app({}))
        run_middleware(app, make_scope(method="GET", path="/admin/channels"))
        assert captured_logs == []
