"""E2E 冒烟：代理请求落库行的 api_type 应等于实际服务的渠道原生格式。

参照 test_graceful_shutdown.py 的断言方式（request_logs.list_requests()），
经 ASGITransport 走完整代理链路（不触发 lifespan，避免与手动 init_backend 抢占），
上游为 e2e_mock_server（session 级 fixture，同时把 DATA_DIR 指向 tests/_test_data）。
"""

import pytest
from httpx import ASGITransport, AsyncClient

import request_logs

pytestmark = pytest.mark.asyncio


async def _proxy_once(client: AsyncClient, path: str, payload: dict) -> None:
    resp = await client.post(path, json=payload)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text[:300]}"


class TestRequestLogApiTypeE2E:
    async def test_proxied_request_logs_channel_native_api_type(self, e2e_mock_server):
        """共享测试库会残留历史行，只断言本次测试窗口内落库的行。"""
        from datetime import UTC, datetime, timedelta

        import storage
        from channel_catalog import catalog
        from main import app
        from tests.conftest import _setup_e2e_channels

        # 全量跑时前序测试可能删改共享的 channels.json / 留下过期缓存，
        # 这里按 session fixture 的方式重建测试渠道数据后再清缓存。
        _setup_e2e_channels()
        catalog.reset()
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._keys_lock = None

        window_start = datetime.now(UTC) - timedelta(seconds=5)
        result = await request_logs.init_backend()
        assert result["available"] is True
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await _proxy_once(
                    client,
                    "/v1/messages",
                    {
                        "model": "claude-sonnet-4-20250514",
                        "messages": [{"role": "user", "content": "Hello"}],
                        "max_tokens": 100,
                        "stream": False,
                    },
                )
                await _proxy_once(
                    client,
                    "/v1/chat/completions",
                    {
                        "model": "gpt-4o",
                        "messages": [{"role": "user", "content": "Hello"}],
                        "max_tokens": 100,
                    },
                )

            await request_logs.drain_queue()
            listed = await request_logs.list_requests(page_size=100, start=window_start)
            rows = {item["channel_id"]: item["api_type"] for item in listed["items"]}
            # 原生直通：日志记录该渠道原生格式（= executor 实际服务的 source_type）
            assert rows.get("ch_e2e_anthropic") == "anthropic"
            assert rows.get("ch_e2e_openai") == "openai-chat-completions"
        finally:
            # 本测试绕过 lifespan，代理请求缓存的 httpx 客户端绑定在本测试的
            # event loop 上；不主动关闭会让后续 TestClient 的跨 loop 关闭炸掉。
            from client import close_all_clients

            await close_all_clients()
            await request_logs.close_backend()
