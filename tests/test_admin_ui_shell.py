import httpx
import pytest

from main import app


@pytest.mark.anyio
async def test_admin_index_is_a_shell():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    html = resp.text
    assert "/static/js/htmx.min.js" in html
    assert 'hx-get="/admin/ui/channels"' in html
    assert 'id="admin-content"' in html
    assert 'id="channelsTab"' not in html


@pytest.mark.anyio
async def test_admin_ui_fragments_exist():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/ui/channels")

    assert resp.status_code == 200
    assert "渠道列表" in resp.text
    assert "channelList" in resp.text
