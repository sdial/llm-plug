import asyncio
import time

import httpx

from config import REQUEST_TIMEOUT
from models.api_types import APIType
from models.channel import Channel

_clients: dict[str, httpx.AsyncClient] = {}
_cache_ts: dict[str, float] = {}
_lock = asyncio.Lock()

_DEFAULT_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=60.0,
)

_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def _cache_key(channel: Channel) -> str:
    return f"{channel.base_url}|{channel.socks5_proxy or ''}"


async def get_or_create_client(channel: Channel, timeout: float | None = None) -> httpx.AsyncClient:
    if timeout is None:
        timeout = float(REQUEST_TIMEOUT)
    key = _cache_key(channel)
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
                limits=_DEFAULT_LIMITS,
            )
        else:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10.0),
                limits=_DEFAULT_LIMITS,
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
            limits=_DEFAULT_LIMITS,
        )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
        limits=_DEFAULT_LIMITS,
    )


async def close_all_clients():
    async with _lock:
        for key, client in list(_clients.items()):
            if not client.is_closed:
                await client.aclose()
        _clients.clear()
        _cache_ts.clear()


async def invalidate_all_clients():
    """关闭并清除所有缓存的普通客户端（用于配置变更后刷新）。"""
    async with _lock:
        for key, client in list(_clients.items()):
            if not client.is_closed:
                await client.aclose()
        _clients.clear()
        _cache_ts.clear()


async def cleanup_stale_clients(max_age: float = 300.0):
    """关闭并移除超过 max_age 秒未使用的客户端连接。"""
    async with _lock:
        now = time.time()
        stale_keys = [k for k, ts in _cache_ts.items() if now - ts > max_age]
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
        extra_headers = extra_headers or {}
        _apply_anthropic_headers(headers, channel, extra_headers)
    else:
        headers["Authorization"] = f"Bearer {channel.api_key}"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _apply_anthropic_headers(headers: dict, channel: Channel, extra_headers: dict) -> None:
    client_version = extra_headers.pop("anthropic-version", None)
    client_beta = extra_headers.pop("anthropic-beta", None)

    channel_version = channel.anthropic_version or _DEFAULT_ANTHROPIC_VERSION
    version_policy = getattr(channel.anthropic_version_policy, "value", channel.anthropic_version_policy)
    if version_policy == "client" and client_version:
        headers["anthropic-version"] = client_version
    elif version_policy == "channel_if_missing" and client_version:
        headers["anthropic-version"] = client_version
    else:
        headers["anthropic-version"] = channel_version

    beta_policy = getattr(channel.anthropic_beta_policy, "value", channel.anthropic_beta_policy)
    if beta_policy == "client":
        beta_value = client_beta or channel.anthropic_beta
    elif beta_policy == "channel_if_missing":
        beta_value = client_beta or channel.anthropic_beta
    elif beta_policy == "merge":
        beta_value = _merge_anthropic_beta(channel.anthropic_beta, client_beta)
    else:
        beta_value = channel.anthropic_beta

    if beta_value:
        headers["anthropic-beta"] = beta_value


def _merge_anthropic_beta(channel_beta: str | None, client_beta: str | None) -> str | None:
    values: list[str] = []
    for raw in (channel_beta, client_beta):
        if not raw:
            continue
        for item in raw.split(","):
            beta = item.strip()
            if beta and beta not in values:
                values.append(beta)
    return ",".join(values) if values else None
