import httpx

from config import REQUEST_TIMEOUT
from models.api_types import APIType
from models.channel import Channel

_clients: dict[str, httpx.AsyncClient] = {}


def _cache_key(channel: Channel) -> str:
    return f"{channel.base_url}|{channel.socks5_proxy or ''}"


def get_or_create_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient:
    if timeout is None:
        timeout = float(REQUEST_TIMEOUT)
    key = _cache_key(channel)
    client = _clients.get(key)
    if client is not None and not client.is_closed:
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
    return client


def create_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient:
    return get_or_create_client(channel, timeout)


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
