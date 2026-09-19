from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from models.api_types import APIType
from models.channel import Endpoint

_UPSTREAM_PATHS = {
    APIType.OPENAI_CHAT.value: "/chat/completions",
    APIType.OPENAI_RESPONSE.value: "/responses",
    APIType.ANTHROPIC.value: "/messages",
}


def build_upstream_url(endpoint: Endpoint) -> str:
    """按选定接入点构造代理请求 URL（ADR-0006）。

    接入点的 url_override 优先，空值则回退其 base_url 自动拼接。"""
    endpoint_url = _clean_url(endpoint.url_override)
    if endpoint_url:
        return endpoint_url

    path = _UPSTREAM_PATHS.get(endpoint.api_type.value)
    if not path:
        return _clean_url(endpoint.base_url)
    return append_api_path(endpoint.base_url, path)


def build_models_url(base_url: str, models_url: str | None = None) -> str:
    """构造模型列表 URL。高级 models_url 优先，空值则回退 base_url。"""
    explicit = _clean_url(models_url)
    if explicit:
        return explicit
    return append_api_path(base_url, "/models")


def append_query(url: str, query_string: str | None) -> str:
    """合并已有 query 和透传 query，避免手写 ? 破坏 URL。"""
    if not query_string:
        return url

    parsed = urlsplit(url)
    items = parse_qsl(parsed.query, keep_blank_values=True)
    items.extend(parse_qsl(query_string, keep_blank_values=True))
    return urlunsplit(parsed._replace(query=urlencode(items, doseq=True)))


def append_api_path(base_url: str, path: str) -> str:
    base = _clean_url(base_url).rstrip("/")
    if not base:
        return path

    parsed = urlsplit(base)
    existing_path = parsed.path.rstrip("/")
    target_path = path.rstrip("/")

    if _has_endpoint_suffix(existing_path, target_path):
        return urlunsplit(parsed)

    new_path = f"{existing_path}{target_path}" if existing_path.endswith("/v1") else f"{existing_path}/v1{target_path}"

    return urlunsplit(parsed._replace(path=new_path))


def _has_endpoint_suffix(existing_path: str, target_path: str) -> bool:
    if existing_path.endswith(target_path):
        return True
    return target_path.endswith("s") and existing_path.endswith(target_path[:-1])


def _clean_url(value: str | None) -> str:
    return (value or "").strip()
