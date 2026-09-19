import asyncio
import time

import httpx
from loguru import logger

import config
from models.api_types import APIType
from models.channel import Channel, Endpoint

_clients: dict[str, httpx.AsyncClient] = {}
_cache_ts: dict[str, float] = {}
_retired_clients: set[httpx.AsyncClient] = set()
_retirement_tasks: set[asyncio.Task[None]] = set()
_lock = asyncio.Lock()
_MAX_CACHED_CLIENTS = 128
_RETIRED_CLIENT_CLOSE_DELAY_SECONDS = 60.0

_DEFAULT_LIMITS = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=50,
    keepalive_expiry=60.0,
)

_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def _endpoint_cache_key(base_url: str, socks5_proxy: str | None) -> str:
    return f"{base_url}|{socks5_proxy or ''}"


def _cache_key(channel: Channel, endpoint: Endpoint | None = None) -> str:
    """连接池键：明确或默认接入点的 base_url + 渠道级 SOCKS5 代理。

    同一渠道不同接入点 base_url 不同，天然各自建池；socks5 是站点级配置。"""
    selected = endpoint if endpoint is not None else channel.selected_endpoint()
    return _endpoint_cache_key(selected.base_url, channel.socks5_proxy)


async def get_or_create_client(
    channel: Channel,
    timeout: float | None = None,
    *,
    endpoint: Endpoint | None = None,
) -> httpx.AsyncClient:
    if timeout is None:
        timeout = float(config.REQUEST_TIMEOUT)
    key = _cache_key(channel, endpoint)
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
        await _evict_lru_clients_locked()
        return client


async def create_client(
    channel: Channel,
    timeout: float | None = None,
    *,
    endpoint: Endpoint | None = None,
) -> httpx.AsyncClient:
    return await get_or_create_client(channel, timeout, endpoint=endpoint)


def create_stream_client(channel: Channel) -> httpx.AsyncClient:
    timeout = float(config.REQUEST_TIMEOUT)
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
    retirement_tasks = list(_retirement_tasks)
    for task in retirement_tasks:
        task.cancel()
    if retirement_tasks:
        await asyncio.gather(*retirement_tasks, return_exceptions=True)
    async with _lock:
        for _key, client in list(_clients.items()):
            if not client.is_closed:
                await client.aclose()
        for retired in list(_retired_clients):
            if not retired.is_closed:
                await retired.aclose()
        _clients.clear()
        _cache_ts.clear()
        _retired_clients.clear()


def _retirement_done(task: asyncio.Task[None]) -> None:
    _retirement_tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(f"delayed upstream client close failed: {error}")


def _schedule_retirement(clients: list[httpx.AsyncClient]) -> None:
    task = asyncio.create_task(_close_retired_clients_later(clients))
    _retirement_tasks.add(task)
    task.add_done_callback(_retirement_done)


def _retired_client_close_delay_seconds() -> float:
    """旧客户端延迟关闭时间，不短于当前请求超时。"""
    return max(_RETIRED_CLIENT_CLOSE_DELAY_SECONDS, float(config.REQUEST_TIMEOUT))


def _retire_clients_locked(clients: list[httpx.AsyncClient]) -> list[httpx.AsyncClient]:
    retired = [client for client in clients if not client.is_closed]
    _retired_clients.update(retired)
    return retired


async def invalidate_all_clients():
    """让后续请求使用新客户端，并延迟关闭旧客户端以避免中断在途请求。"""
    async with _lock:
        retired = _retire_clients_locked(list(_clients.values()))
        _clients.clear()
        _cache_ts.clear()
    if retired:
        _schedule_retirement(retired)


async def _close_retired_clients_later(clients: list[httpx.AsyncClient]) -> None:
    await asyncio.sleep(_retired_client_close_delay_seconds())
    async with _lock:
        for retired in clients:
            _retired_clients.discard(retired)
            if not retired.is_closed:
                await retired.aclose()


async def cleanup_stale_clients(max_age: float = 300.0):
    """关闭并移除超过 max_age 秒未使用的客户端连接。"""
    async with _lock:
        now = time.time()
        stale_keys = [k for k, ts in _cache_ts.items() if now - ts > max_age]
        stale_clients = []
        for key in stale_keys:
            client = _clients.pop(key, None)
            _cache_ts.pop(key, None)
            if client and not client.is_closed:
                stale_clients.append(client)
        retired = _retire_clients_locked(stale_clients)
    if retired:
        _schedule_retirement(retired)


async def _evict_lru_clients_locked() -> None:
    evicted: list[httpx.AsyncClient] = []
    while len(_clients) > _MAX_CACHED_CLIENTS:
        oldest_key = min(_cache_ts, key=_cache_ts.get)
        oldest_client = _clients.pop(oldest_key, None)
        _cache_ts.pop(oldest_key, None)
        if oldest_client is not None:
            evicted.append(oldest_client)
    retired = _retire_clients_locked(evicted)
    if retired:
        _schedule_retirement(retired)


async def remove_channel_client(channel: Channel):
    """从缓存中移除渠道全部接入点的客户端（用于渠道配置变更后刷新连接）。"""
    keys = [_endpoint_cache_key(ep.base_url, channel.socks5_proxy) for ep in channel.endpoints]
    async with _lock:
        removed = []
        for key in keys:
            client = _clients.pop(key, None)
            _cache_ts.pop(key, None)
            if client is not None:
                removed.append(client)
        retired = _retire_clients_locked(removed)
    if retired:
        _schedule_retirement(retired)
    return retired[0] if len(retired) == 1 else (retired or None)


def get_upstream_headers(
    channel: Channel,
    extra_headers: dict | None = None,
    *,
    endpoint: Endpoint | None = None,
) -> dict:
    """组装上游请求头（ADR-0006：协议属性随选定接入点生效）。

    endpoint 缺省时取渠道当前选定接入点；显式传入供按接入点测试等
    无投影场景使用。密钥优先级：接入点 api_key_override > 渠道 key。"""
    ep = endpoint if endpoint is not None else channel.selected_endpoint()
    effective_api_key = ep.api_key_override or channel.api_key
    headers = {}
    if ep.api_type == APIType.ANTHROPIC:
        headers["x-api-key"] = effective_api_key
        extra_headers = extra_headers or {}
        _apply_anthropic_headers(headers, ep, extra_headers)
    else:
        headers["Authorization"] = f"Bearer {effective_api_key}"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _apply_anthropic_headers(headers: dict, endpoint: Endpoint, extra_headers: dict) -> None:
    client_version = extra_headers.pop("anthropic-version", None)
    client_beta = extra_headers.pop("anthropic-beta", None)

    endpoint_version = endpoint.anthropic_version or _DEFAULT_ANTHROPIC_VERSION
    version_policy = getattr(endpoint.anthropic_version_policy, "value", endpoint.anthropic_version_policy)
    if version_policy == "client":
        if not client_version:
            raise ValueError("anthropic-version is required when anthropic_version_policy is client")
        headers["anthropic-version"] = client_version
    elif version_policy == "channel_if_missing" and client_version:
        headers["anthropic-version"] = client_version
    else:
        headers["anthropic-version"] = endpoint_version

    beta_policy = getattr(endpoint.anthropic_beta_policy, "value", endpoint.anthropic_beta_policy)
    if beta_policy == "client" or beta_policy == "channel_if_missing":
        beta_value = client_beta or endpoint.anthropic_beta
    elif beta_policy == "merge":
        beta_value = _merge_anthropic_beta(endpoint.anthropic_beta, client_beta)
    else:
        beta_value = endpoint.anthropic_beta

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
