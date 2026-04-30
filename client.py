import asyncio
import time

import httpx

from config import REQUEST_TIMEOUT
from models.api_types import APIType
from models.channel import Channel

_clients: dict[str, httpx.AsyncClient] = {}
_cache_ts: dict[str, float] = {}
_lock = asyncio.Lock()


def _cache_key(channel: Channel) -> str:
    return f"{channel.base_url}|{channel.socks5_proxy or ''}"


async def get_or_create_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient:
    if timeout is None:
        timeout = float(REQUEST_TIMEOUT)
    key = _cache_key(channel)
    client = _clients.get(key)
    if client is not None and not client.is_closed:
        _cache_ts[key] = time.time()
        return client
    async with _lock:
        client = _clients.get(key)
        if client is not None and not client.is_closed:
            _cache_ts[key] = time.time()
            return client
        proxy = channel.socks5_proxy
        if proxy:
            client = httpx.AsyncClient(
                proxy=proxy,
                timeout=httpx.Timeout(timeout, connect=10.0),
            )
        else:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10.0),
            )
        _clients[key] = client
        _cache_ts[key] = time.time()
        return client


async def create_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient:
    return await get_or_create_client(channel, timeout)


def create_stream_client(channel: Channel) -> httpx.AsyncClient:
    timeout = float(REQUEST_TIMEOUT)
    proxy = channel.socks5_proxy
    if proxy:
        return httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
        )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
    )


async def close_all_clients():
    for key, client in list(_clients.items()):
        if not client.is_closed:
            await client.aclose()
    _clients.clear()
    _cache_ts.clear()


async def cleanup_stale_clients(max_age: float = 300.0):
    """关闭并移除超过 max_age 秒未使用的客户端连接。"""
    now = time.time()
    stale_keys = [k for k, ts in _cache_ts.items() if now - ts > max_age]
    async with _lock:
        for key in stale_keys:
            client = _clients.pop(key, None)
            _cache_ts.pop(key, None)
            if client and not client.is_closed:
                await client.aclose()


async def remove_channel_client(channel: Channel):
    """从缓存中移除指定渠道的客户端（用于渠道配置变更后刷新连接）。"""
    key = _cache_key(channel)
    async with _lock:
        client = _clients.pop(key, None)
        _cache_ts.pop(key, None)
    if client and not client.is_closed:
        await client.aclose()
    return client


def get_upstream_headers(channel: Channel, extra_headers: dict | None = None) -> dict:
    headers = {}
    if channel.api_type == APIType.ANTHROPIC:
        headers["x-api-key"] = channel.api_key
        headers["anthropic-version"] = "2023-06-01"
        headers["anthropic-beta"] = "prompt-caching-2024-07-31,interleaved-thinking-2025-05-14"
    else:
        headers["Authorization"] = f"Bearer {channel.api_key}"
    if extra_headers:
        headers.update(extra_headers)
    return headers
