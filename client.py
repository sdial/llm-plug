import httpx
from typing import Optional

from models.channel import Channel


def create_client(channel: Channel, timeout: float = 120.0) -> httpx.AsyncClient:
    """根据渠道配置创建HTTP客户端（支持SOCKS5代理）"""
    proxy = channel.socks5_proxy
    if proxy:
        return httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
    )


def get_upstream_headers(channel: Channel, extra_headers: Optional[dict] = None) -> dict:
    """构建上游请求头"""
    headers = {}
    if channel.api_type.value == "anthropic":
        headers["x-api-key"] = channel.api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {channel.api_key}"
    if extra_headers:
        headers.update(extra_headers)
    return headers
