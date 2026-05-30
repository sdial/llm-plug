import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

import config
import storage
from main import app
from routers import admin


@pytest.fixture
def admin_auth_files(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"

    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    keys_path.write_text(
        json.dumps(
            {
                "api_keys": [
                    {
                        "id": "key_1",
                        "name": "llm-client",
                        "key": "sk-llm-client",
                        "enabled": True,
                        "allowed_models": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = asyncio.Lock()
    storage._keys_lock = asyncio.Lock()

    import main
    main._whitelist_cache = main._whitelist.WhitelistCache(str(data_dir / "whitelist.csv"))

    yield


@pytest.mark.anyio
async def test_unconfigured_admin_requires_password_setup(admin_auth_files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/channels")

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "admin_login_required"


@pytest.mark.anyio
async def test_admin_login_sets_http_only_session_cookie(admin_auth_files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        setup_resp = await client.post(
            "/admin/auth/setup",
            json={"password": "correct horse battery staple"},
        )
        denied = await client.get("/admin/channels")
        login_resp = await client.post(
            "/admin/auth/login",
            json={"password": "correct horse battery staple"},
        )
        allowed = await client.get("/admin/channels")

    assert setup_resp.status_code == 200
    assert denied.status_code == 401
    assert login_resp.status_code == 200
    cookie = login_resp.headers["set-cookie"]
    assert "admin_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert allowed.status_code == 200


@pytest.mark.anyio
async def test_llm_api_key_does_not_authorize_admin(admin_auth_files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/admin/auth/setup", json={"password": "admin-passphrase"})
        resp = await client.get(
            "/admin/channels",
            headers={"Authorization": "Bearer sk-llm-client"},
        )

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "admin_login_required"


@pytest.mark.anyio
async def test_admin_router_requires_session_without_main_middleware(admin_auth_files):
    isolated_app = FastAPI()
    isolated_app.include_router(admin.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=isolated_app), base_url="http://test"
    ) as client:
        await client.post("/admin/auth/setup", json={"password": "admin-passphrase"})
        resp = await client.get("/admin/channels")

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "admin_login_required"


@pytest.mark.anyio
async def test_login_page_and_static_assets_are_public(admin_auth_files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = await client.get("/admin/login", follow_redirects=False)
        asset = await client.get("/admin/static/js/admin.js")

    assert page.status_code == 200
    assert "管理员登录" in page.text
    assert asset.status_code == 200


@pytest.mark.anyio
async def test_logout_revokes_existing_session_token(admin_auth_files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/admin/auth/setup",
            json={"password": "correct horse battery staple"},
        )
        login_resp = await client.post(
            "/admin/auth/login",
            json={"password": "correct horse battery staple"},
        )
        session_cookie = login_resp.headers["set-cookie"].split(";", 1)[0]
        csrf_resp = await client.get("/admin/auth/csrf")
        logout_resp = await client.post(
            "/admin/auth/logout",
            headers={
                "Cookie": session_cookie,
                "X-CSRF-Token": csrf_resp.json()["csrf_token"],
            },
        )

    assert logout_resp.status_code == 200
    assert "Max-Age=0" in logout_resp.headers["set-cookie"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/admin/channels",
            headers={"Cookie": session_cookie},
        )

    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "admin_login_required"


@pytest.mark.anyio
async def test_admin_mutation_requires_csrf_token(admin_auth_files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/admin/auth/setup",
            json={"password": "correct horse battery staple"},
        )
        await client.post(
            "/admin/auth/login",
            json={"password": "correct horse battery staple"},
        )

        denied = await client.post(
            "/admin/api-keys",
            json={"name": "client-key"},
        )
        csrf_resp = await client.get("/admin/auth/csrf")
        allowed = await client.post(
            "/admin/api-keys",
            headers={"X-CSRF-Token": csrf_resp.json()["csrf_token"]},
            json={"name": "client-key"},
        )

    assert denied.status_code == 403
    assert denied.json()["error"]["type"] == "csrf_error"
    assert csrf_resp.status_code == 200
    assert allowed.status_code == 200


@pytest.mark.anyio
async def test_admin_login_rate_limits_failed_attempts(admin_auth_files):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/admin/auth/setup",
            json={"password": "correct horse battery staple"},
        )

        responses = [
            await client.post("/admin/auth/login", json={"password": "wrong"})
            for _ in range(11)
        ]
        valid_after_limit = await client.post(
            "/admin/auth/login",
            json={"password": "correct horse battery staple"},
        )

    assert [resp.status_code for resp in responses[:5]] == [401] * 5
    assert responses[5].status_code == 429
    assert valid_after_limit.status_code == 429
