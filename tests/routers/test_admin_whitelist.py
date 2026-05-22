import ipaddress

import pytest
import pytest_asyncio
import httpx

import request_logs
import stats
import whitelist as wl
from main import app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(tmp_path, monkeypatch):
    await stats.init_db(str(tmp_path / "stats.db"))
    await request_logs.init_backend(
        {
            "request_log_db_type": "sqlite",
            "request_log_sqlite_path": str(tmp_path / "request_logs.db"),
        }
    )
    yield
    await stats.close_pool()
    await request_logs.close_backend()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestWhitelistMiddleware:
    async def test_no_rules_allows_admin(self, client, monkeypatch):
        """白名单为空时放行所有请求"""
        import main
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: [])
        resp = await client.get("/admin/channels")
        assert resp.status_code != 403

    async def test_matching_ip_allows_request(self, client, monkeypatch):
        """IP 匹配白名单规则时放行"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods=frozenset(),
                network=ipaddress.ip_network("127.0.0.1/32"),
                description="test",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        resp = await client.get("/admin/channels")
        assert resp.status_code != 403

    async def test_non_matching_ip_blocks_admin(self, client, monkeypatch):
        """IP 不在白名单时返回 403"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods=frozenset(),
                network=ipaddress.ip_network("10.0.0.0/8"),
                description="内网",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        resp = await client.get("/admin/channels")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["type"] == "ip_whitelist_error"
        assert "IP 白名单" in body["error"]["message"]

    async def test_method_not_allowed_returns_403(self, client, monkeypatch):
        """方法不在白名单时返回 403"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods=frozenset({"GET"}),
                network=ipaddress.ip_network("127.0.0.1/32"),
                description="test",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        resp = await client.delete("/admin/channels/nonexistent")
        assert resp.status_code == 403
        assert "DELETE" in resp.json()["error"]["message"]

    async def test_non_admin_path_not_blocked(self, client, monkeypatch):
        """白名单规则只针对 /admin/*，其他路径不受影响"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/admin/*",
                methods=frozenset(),
                network=ipaddress.ip_network("10.0.0.0/8"),
                description="内网",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        # 根路径重定向，不应被 403
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code != 403

    async def test_whitelist_blocks_proxy_path(self, client, monkeypatch):
        """白名单可阻断代理路径（/v1/chat/completions 等）"""
        import main
        rules = [
            wl.WhitelistRule(
                path_pattern="/v1/*",
                methods=frozenset(),
                network=ipaddress.ip_network("10.0.0.0/8"),
                description="内网代理",
            )
        ]
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: rules)
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["type"] == "ip_whitelist_error"


class TestWhitelistAPI:
    @pytest_asyncio.fixture(autouse=True)
    async def patch_whitelist_path(self, tmp_path, monkeypatch):
        """每个测试使用独立临时目录，白名单检查全部放行"""
        import routers.admin as admin_router
        import main
        monkeypatch.setattr(admin_router, "WHITELIST_PATH", tmp_path / "whitelist.csv")
        monkeypatch.setattr(main._whitelist_cache, "get_rules", lambda: [])

    async def test_get_whitelist_no_file(self, client):
        resp = await client.get("/admin/whitelist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == ""
        assert data["rule_count"] == 0

    async def test_put_whitelist_saves_and_returns_count(self, client):
        content = (
            "path_pattern,methods,ip_cidr,description\n"
            "/admin/*,*,10.1.1.0/24,内网\n"
            "/admin/*,*,127.0.0.1,本机\n"
        )
        resp = await client.put("/admin/whitelist", json={"content": content})
        assert resp.status_code == 200
        data = resp.json()
        assert data["rule_count"] == 2

    async def test_get_whitelist_returns_saved_content(self, client):
        content = "# comment\n/admin/*,*,127.0.0.1,本机\n"
        await client.put("/admin/whitelist", json={"content": content})
        resp = await client.get("/admin/whitelist")
        assert resp.json()["content"] == content
        assert resp.json()["rule_count"] == 1

    async def test_put_whitelist_invalid_cidr_returns_400(self, client):
        resp = await client.put(
            "/admin/whitelist", json={"content": "/admin/*,*,not-an-ip,test\n"}
        )
        assert resp.status_code == 400
        assert "not-an-ip" in resp.json()["detail"]

    async def test_put_whitelist_bad_column_count_returns_400(self, client):
        resp = await client.put(
            "/admin/whitelist", json={"content": "/admin/*,*\n"}
        )
        assert resp.status_code == 400
        assert "4 列" in resp.json()["detail"]

    async def test_put_whitelist_empty_clears_rules(self, client):
        await client.put("/admin/whitelist", json={"content": "/admin/*,*,127.0.0.1,test\n"})
        resp = await client.put("/admin/whitelist", json={"content": ""})
        assert resp.status_code == 200
        assert resp.json()["rule_count"] == 0
