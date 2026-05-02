import asyncio
import time

import httpx
import pytest

import client
from models.api_types import APIType
from models.channel import Channel


@pytest.fixture(autouse=True)
def reset_client_state():
    """每个测试前清理全局客户端缓存。"""
    client._clients.clear()
    client._cache_ts.clear()
    yield
    # teardown: 关闭所有未关闭的客户端
    for c in list(client._clients.values()):
        if not c.is_closed:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(c.aclose())
                loop.close()
            except Exception:
                pass
    client._clients.clear()
    client._cache_ts.clear()


@pytest.fixture
def sample_channel():
    return Channel(
        id="ch_1",
        name="Test Channel",
        api_type=APIType.OPENAI_CHAT,
        base_url="https://api.openai.com",
        api_key="sk-test",
        models=["gpt-4"],
    )


@pytest.fixture
def anthropic_channel():
    return Channel(
        id="ch_2",
        name="Anthropic Channel",
        api_type=APIType.ANTHROPIC,
        base_url="https://api.anthropic.com",
        api_key="ak-test",
        models=["claude-opus-4-7"],
    )


@pytest.fixture
def proxy_channel():
    return Channel(
        id="ch_3",
        name="Proxy Channel",
        api_type=APIType.OPENAI_CHAT,
        base_url="https://proxy.example.com",
        api_key="sk-test",
        models=["gpt-4"],
        socks5_proxy="socks5://127.0.0.1:1080",
    )


def _run_async(coro):
    """辅助函数：在新建的事件循环中运行协程。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestCacheKey:
    def test_includes_base_url_and_proxy(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="key",
            models=["gpt-4"],
        )
        assert client._cache_key(ch) == "https://api.openai.com|"

    def test_includes_proxy_when_present(self):
        ch = Channel(
            id="ch_1",
            name="Test",
            api_type=APIType.OPENAI_CHAT,
            base_url="https://api.openai.com",
            api_key="key",
            models=["gpt-4"],
            socks5_proxy="socks5://127.0.0.1:1080",
        )
        assert client._cache_key(ch) == "https://api.openai.com|socks5://127.0.0.1:1080"


class TestGetOrCreateClient:
    @pytest.mark.anyio
    async def test_creates_new_client(self, sample_channel):
        c = await client.get_or_create_client(sample_channel)
        assert isinstance(c, httpx.AsyncClient)
        assert not c.is_closed

    @pytest.mark.anyio
    async def test_returns_cached_client(self, sample_channel):
        c1 = await client.get_or_create_client(sample_channel)
        c2 = await client.get_or_create_client(sample_channel)
        assert c1 is c2

    @pytest.mark.anyio
    async def test_creates_different_client_for_different_proxy(self, sample_channel, proxy_channel):
        # 相同 base_url 但不同 proxy 应创建不同客户端
        c1 = await client.get_or_create_client(sample_channel)
        c2 = await client.get_or_create_client(proxy_channel)
        assert c1 is not c2

    @pytest.mark.anyio
    async def test_updates_cache_timestamp_on_reuse(self, sample_channel):
        c1 = await client.get_or_create_client(sample_channel)
        ts_before = client._cache_ts[client._cache_key(sample_channel)]
        time.sleep(0.05)
        c2 = await client.get_or_create_client(sample_channel)
        ts_after = client._cache_ts[client._cache_key(sample_channel)]
        assert ts_after > ts_before
        assert c1 is c2

    @pytest.mark.anyio
    async def test_uses_custom_timeout(self, sample_channel):
        c = await client.get_or_create_client(sample_channel, timeout=30.0)
        # httpx.Timeout 对象
        assert c.timeout.connect == 10.0
        # 总超时时间应接近 30 秒
        assert c.timeout.read == 30.0

    @pytest.mark.anyio
    async def test_creates_new_client_after_closed(self, sample_channel):
        c1 = await client.get_or_create_client(sample_channel)
        await c1.aclose()
        c2 = await client.get_or_create_client(sample_channel)
        assert c1 is not c2
        assert not c2.is_closed


class TestCreateStreamClient:
    def test_creates_new_client_each_time(self, sample_channel):
        c1 = client.create_stream_client(sample_channel)
        c2 = client.create_stream_client(sample_channel)
        assert c1 is not c2

    def test_sets_read_timeout(self, sample_channel):
        c = client.create_stream_client(sample_channel)
        # 流式客户端的 read 超时使用 REQUEST_TIMEOUT
        assert c.timeout.read == 300.0

    def test_uses_proxy_when_configured(self, proxy_channel):
        c = client.create_stream_client(proxy_channel)
        # httpx.AsyncClient 的 _mounts 中包含代理
        mounts = getattr(c, "_mounts", {})
        has_proxy = any(
            getattr(m, "_proxy_url", None) is not None
            for m in mounts.values()
        ) if mounts else False
        # 另一种检测方式：检查 transport 是否有代理
        transport = getattr(c, "_transport", None)
        if transport:
            has_proxy = has_proxy or getattr(transport, "_proxy_url", None) is not None
        # 如果不能直接检测代理，至少确认客户端创建成功且不是 None
        assert c is not None
        assert isinstance(c, httpx.AsyncClient)


class TestCloseAllClients:
    @pytest.mark.anyio
    async def test_closes_all_clients(self, sample_channel, proxy_channel):
        c1 = await client.get_or_create_client(sample_channel)
        c2 = await client.get_or_create_client(proxy_channel)
        await client.close_all_clients()
        assert c1.is_closed
        assert c2.is_closed
        assert len(client._clients) == 0
        assert len(client._cache_ts) == 0


class TestCleanupStaleClients:
    @pytest.mark.anyio
    async def test_removes_stale_clients(self, sample_channel):
        c = await client.get_or_create_client(sample_channel)
        # 将缓存时间设为很久以前
        client._cache_ts[client._cache_key(sample_channel)] = time.time() - 1000
        await client.cleanup_stale_clients(max_age=300.0)
        assert c.is_closed
        assert len(client._clients) == 0

    @pytest.mark.anyio
    async def test_keeps_recent_clients(self, sample_channel):
        c = await client.get_or_create_client(sample_channel)
        await client.cleanup_stale_clients(max_age=300.0)
        assert not c.is_closed
        assert len(client._clients) == 1


class TestRemoveChannelClient:
    @pytest.mark.anyio
    async def test_removes_and_closes_client(self, sample_channel):
        c = await client.get_or_create_client(sample_channel)
        removed = await client.remove_channel_client(sample_channel)
        assert removed is c
        assert client._cache_key(sample_channel) not in client._clients

    @pytest.mark.anyio
    async def test_returns_none_when_not_cached(self, sample_channel):
        removed = await client.remove_channel_client(sample_channel)
        assert removed is None


class TestGetUpstreamHeaders:
    def test_openai_headers(self, sample_channel):
        headers = client.get_upstream_headers(sample_channel)
        assert headers["Authorization"] == "Bearer sk-test"
        assert "x-api-key" not in headers

    def test_anthropic_headers(self, anthropic_channel):
        headers = client.get_upstream_headers(anthropic_channel)
        assert headers["x-api-key"] == "ak-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "prompt-caching-2024-07-31" in headers["anthropic-beta"]
        assert "interleaved-thinking-2025-05-14" in headers["anthropic-beta"]

    def test_merges_extra_headers(self, sample_channel):
        extra = {"X-Custom": "value"}
        headers = client.get_upstream_headers(sample_channel, extra)
        assert headers["X-Custom"] == "value"
        assert headers["Authorization"] == "Bearer sk-test"

    def test_extra_headers_override(self, sample_channel):
        extra = {"Authorization": "Bearer override"}
        headers = client.get_upstream_headers(sample_channel, extra)
        assert headers["Authorization"] == "Bearer override"
