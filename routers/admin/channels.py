"""渠道管理域路由：可用模型列表、渠道 CRUD、启停、连通性测试、上游模型抓取。"""

import asyncio
import copy
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import config
from channel_catalog import catalog
from client import get_upstream_headers
from models.channel import Channel, ChannelCreate, ChannelUpdate, Endpoint
from proxy import outcomes
from proxy.endpoint_execution import EndpointExecutionInput, execute_endpoint
from upstream_catalog import RevisionNotFoundError
from upstream_catalog import catalog as upstream_catalog
from url_builder import build_models_url

from .common import (
    AdminAuthRoute,
    _get_channels,
    _validate_outbound_url,
)

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


class FetchModelsRequest(BaseModel):
    base_url: str
    models_url: str | None = None
    api_key: str | None = None
    api_type: str
    channel_id: str | None = None


def _validate_endpoints_outbound_urls(endpoints: list[Endpoint]) -> None:
    """出站校验覆盖每个接入点：只验首个会漏掉嵌套创建的其余入口。"""
    for ep in endpoints:
        for url in (ep.base_url, ep.url_override, ep.models_url):
            if url and url.strip():
                _validate_outbound_url(url)


async def _validate_profile_reference(profile_id: str, revision: str, endpoints: list[Endpoint]) -> None:
    try:
        document = await upstream_catalog.revision(revision)
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    profile = next((item for item in document.upstream_profiles if item.id == profile_id), None)
    if profile is None:
        raise HTTPException(status_code=409, detail=f"Upstream Profile 不存在: {profile_id}")
    supported = {item.api_type for item in profile.endpoints}
    missing = sorted({endpoint.api_type.value for endpoint in endpoints if endpoint.api_type not in supported})
    if missing:
        raise HTTPException(status_code=409, detail=f"档案 {profile_id} 不支持接入点格式: {', '.join(missing)}")


@router.get("/models")
async def list_available_models():
    """所有渠道中可用的模型列表（去重、排序），供模型组编辑时下拉选择"""
    channels = await _get_channels()
    seen: set[str] = set()
    for ch in channels:
        for m in ch.models or []:
            seen.add(m)
    return {"models": sorted(seen)}


@router.get("/channels")
async def list_channels():
    """获取所有渠道（API Key 脱敏）"""
    channels = await _get_channels()
    result = []
    for ch in channels:
        d = ch.to_storage_dict()
        if d.get("api_key"):
            key = d["api_key"]
            d["api_key"] = key[:4] + "***" if len(key) > 4 else "***"
        result.append(d)
    return result


@router.post("/channels", response_model=Channel)
async def create_channel(body: ChannelCreate):
    """添加渠道"""
    _validate_endpoints_outbound_urls(body.endpoints)
    payload = body.model_dump()
    if "catalog_revision" not in body.model_fields_set:
        payload["catalog_revision"] = await upstream_catalog.active_revision()
    channel = Channel(**payload)
    await _validate_profile_reference(channel.upstream_profile_id, channel.catalog_revision, channel.endpoints)

    return await catalog.add_channel(channel)


@router.put("/channels/{channel_id}", response_model=Channel)
async def update_channel(channel_id: str, body: ChannelUpdate):
    """更新渠道（嵌套契约：body 含 endpoints 即整组替换，不含则保持旧接入点）"""
    update_data = body.model_dump(exclude_unset=True)
    update_data.pop("confirm_profile_change", None)
    if body.endpoints is not None:
        _validate_endpoints_outbound_urls(body.endpoints)
    current = next((item for item in (await catalog.snapshot()).channels if item.id == channel_id), None)
    if current is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    prospective = Channel.model_validate({**current.model_dump(), **update_data})
    if (
        prospective.upstream_profile_id != current.upstream_profile_id or prospective.catalog_revision != current.catalog_revision
    ) and not body.confirm_profile_change:
        raise HTTPException(status_code=409, detail="切换档案或目录版本需要 confirm_profile_change=true")
    await _validate_profile_reference(prospective.upstream_profile_id, prospective.catalog_revision, prospective.endpoints)
    updated = await catalog.update_channel(channel_id, update_data)
    if updated is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return updated


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    """删除渠道"""
    removed = await catalog.delete_channel(channel_id)
    if removed is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return {"message": "删除成功"}


@router.patch("/channels/{channel_id}/toggle", response_model=Channel)
async def toggle_channel(channel_id: str):
    """启用/禁用渠道"""
    updated = await catalog.toggle_channel(channel_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return updated


def _build_probe_payload(api_type_value: str, probe_model: str) -> dict:
    """三分支探测 payload：max_tokens=5；thinking 模型带 thinking 参数。"""
    if api_type_value == "openai-chat-completions":
        return {
            "model": probe_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    if api_type_value == "openai-response":
        return {
            "model": probe_model,
            "input": "Hi",
            "max_output_tokens": 5,
        }
    payload = {
        "model": probe_model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
    }
    # 如果是 thinking 模型，添加 thinking 参数
    if "thinking" in probe_model.lower():
        payload["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    return payload


def _interpret_probe_response(api_type_value: str, data: dict) -> tuple[bool, str]:
    """按入口格式判定探测是否有效并提取回复摘要（成功截 100 字符、异常体截 200）。"""
    if api_type_value == "openai-chat-completions":
        choices = data.get("choices", [])
        ok = bool(choices) and choices[0].get("message", {}).get("content") is not None
        reply = choices[0]["message"]["content"][:100] if ok else str(data)[:200]
        return ok, reply
    if api_type_value == "openai-response":
        output = data.get("output", [])
        ok = bool(output)
        reply = str(output[0])[:100] if ok else str(data)[:200]
        return ok, reply
    content = data.get("content", [])
    ok = bool(content)
    if not ok:
        return False, str(data)[:200]
    # 处理 thinking 模式：找到第一个 text 类型的内容
    text_reply = None
    thinking_reply = None
    for part in content:
        if part.get("type") == "text":
            text_reply = part.get("text", "")
            break
        elif part.get("type") == "thinking":
            thinking_reply = part.get("thinking", "")
    # 优先使用 text 内容，如果没有则使用 thinking 内容
    return ok, (text_reply or thinking_reply or "")[:100]


def _short_circuit_test_results(endpoints: list[Endpoint], message: str, probe_model: str | None) -> dict:
    """前置校验失败的统一形态：每个待测接入点一条失败结果，不发探测请求。"""
    return {
        "success": False,
        "results": [
            {
                "api_type": ep.api_type.value,
                "success": False,
                "message": message,
                "latency_ms": None,
                "model": probe_model,
                "reply": None,
            }
            for ep in endpoints
        ],
    }


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: str,
    model: Annotated[str | None, Query()] = None,
    api_type: Annotated[str | None, Query()] = None,
):
    """测试渠道连通性：默认逐个测试全部启用接入点，可指定单个 api_type。

    每个启用接入点经完整生产发送栈探测（converter 直通 / capability / PII / RPM
    预算排队 / 连接池缓存含 socks5），成功与失败均由发送栈落库为
    ``request_source='admin_test'``；quota 窗口屏蔽时短路不打上游，失败不计
    健康度/冷却（成功时发送栈自然的 success 记账予以保留）。
    """
    channels = await _get_channels()
    channel = next((ch for ch in channels if ch.id == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")

    enabled_endpoints = [ep for ep in channel.endpoints if ep.enabled]
    if not enabled_endpoints:
        raise HTTPException(status_code=400, detail="渠道无启用的接入点")

    if api_type:
        scoped_endpoints = [ep for ep in enabled_endpoints if ep.api_type == api_type]
        if not scoped_endpoints:
            raise HTTPException(status_code=404, detail=f"渠道无启用的 {api_type} 接入点")
    else:
        scoped_endpoints = enabled_endpoints

    if not channel.models:
        return _short_circuit_test_results(scoped_endpoints, "渠道无可用模型", None)

    if model:
        # 校验语义保持现状：模型不在渠道列表时短路，全部结果标记失败
        if model not in channel.models:
            return _short_circuit_test_results(scoped_endpoints, f"模型 '{model}' 不在此渠道的模型列表中", model)
        probe_model = model
    else:
        probe_model = channel.models[0]

    # quota 窗口屏蔽中的渠道：短路返回每个目标接入点的失败结果，不打上游
    if outcomes.is_blocked(channel.id):
        return _short_circuit_test_results(scoped_endpoints, "渠道处于配额窗口屏蔽中，暂不可用", probe_model)

    results = []
    settings = copy.deepcopy(config.get_settings())
    wait_budget = float(settings.get("rate_limit_wait_seconds") or 0)
    for endpoint in scoped_endpoints:
        payload = _build_probe_payload(endpoint.api_type.value, probe_model)
        start = time.monotonic()
        try:
            response_data = await execute_endpoint(
                channel,
                endpoint,
                EndpointExecutionInput(
                    payload=payload,
                    inbound_api_type=endpoint.api_type,
                    requested_model=probe_model,
                    serving_model=probe_model,
                    is_stream=False,
                    request_source="admin_test",
                ),
                settings=settings,
                wait_budget=wait_budget,
            )
            latency_ms = round((time.monotonic() - start) * 1000)
            ok, reply = _interpret_probe_response(endpoint.api_type.value, response_data)
            results.append(
                {
                    "api_type": endpoint.api_type.value,
                    "success": ok,
                    "message": "测试通过" if ok else "返回数据格式异常",
                    "latency_ms": latency_ms,
                    "model": probe_model,
                    "reply": reply,
                }
            )
        except Exception as e:
            latency_ms = round((time.monotonic() - start) * 1000)
            results.append(
                {
                    "api_type": endpoint.api_type.value,
                    "success": False,
                    "message": f"请求失败: {e!s}",
                    "latency_ms": latency_ms,
                    "model": probe_model,
                    "reply": None,
                }
            )

    return {"success": all(r["success"] for r in results), "results": results}


class _ModelFetchError(Exception):
    """单接入点模型列表拉取失败（非 200 / 超时 / 网络异常），消息面向管理员展示。"""


def _fetch_models_headers(api_key: str | None, api_type: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        if api_type == "anthropic":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _fetch_upstream_models(models_url: str, headers: dict[str, str]) -> list[str]:
    """GET 上游模型列表并解析为去重排序的模型名；失败抛 _ModelFetchError。"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(models_url, headers=headers, follow_redirects=False)
            if resp.status_code != 200:
                raise _ModelFetchError(f"上游返回 {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            models = [m.get("id", m.get("name", "")) for m in data.get("data", data.get("models", []))]
            return sorted(set(filter(None, models)))
    except httpx.TimeoutException as e:
        raise _ModelFetchError("请求上游超时") from e
    except _ModelFetchError:
        raise
    except Exception as e:
        raise _ModelFetchError("请求失败") from e


async def _resolve_form_fetch_headers(body: FetchModelsRequest) -> dict[str, str]:
    """解析模型拉取认证：显式输入优先；编辑态可安全复用未变接入点的已保存密钥。"""
    if body.api_key:
        return _fetch_models_headers(body.api_key, body.api_type)
    if not body.channel_id:
        return _fetch_models_headers(None, body.api_type)

    channel = next((item for item in await _get_channels() if item.id == body.channel_id), None)
    if channel is None:
        raise HTTPException(status_code=404, detail="渠道不存在")

    endpoint = next((item for item in channel.endpoints if item.api_type.value == body.api_type), None)
    requested_models_url = body.models_url or None
    if endpoint is None or endpoint.base_url.strip() != body.base_url.strip() or (endpoint.models_url or None) != requested_models_url:
        raise HTTPException(status_code=400, detail="编辑中的接入点地址与已保存的接入点地址不一致；请填写 API Key 后再拉取模型")
    return get_upstream_headers(channel, None, endpoint=endpoint)


@router.post("/channels/fetch-models")
async def fetch_models(body: FetchModelsRequest):
    """从上游 API 获取模型列表（代理请求，避免浏览器跨域）。"""
    headers = await _resolve_form_fetch_headers(body)
    models_url = build_models_url(body.base_url, body.models_url)
    _validate_outbound_url(models_url)

    try:
        return {"models": await _fetch_upstream_models(models_url, headers)}
    except _ModelFetchError as e:
        return {"error": str(e)}


async def _fetch_endpoint_models(channel: Channel, endpoint: Endpoint) -> tuple[list[str], str | None]:
    """拉取单个启用接入点的模型列表；(models, None) 成功、([], 原因) 失败——单点不阻断并发批次。"""
    try:
        models_url = build_models_url(endpoint.base_url, endpoint.models_url)
        _validate_outbound_url(models_url)
        headers = get_upstream_headers(channel, None, endpoint=endpoint)
        return await _fetch_upstream_models(models_url, headers), None
    except HTTPException as e:
        return [], str(e.detail)
    except Exception as e:
        return [], str(e)


@router.post("/channels/{channel_id}/fetch-models")
async def fetch_models_for_channel(channel_id: str):
    """并发拉取渠道全部启用接入点的模型列表，合并去重；单点失败不影响其余结果"""
    channels = await _get_channels()
    channel = next((ch for ch in channels if ch.id == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")

    enabled_endpoints = [ep for ep in channel.endpoints if ep.enabled]
    if not enabled_endpoints:
        raise HTTPException(status_code=400, detail="渠道无启用的接入点")

    fetched = await asyncio.gather(
        *(_fetch_endpoint_models(channel, ep) for ep in enabled_endpoints),
        return_exceptions=True,
    )

    merged_models: list[str] = []
    results = []
    for endpoint, outcome in zip(enabled_endpoints, fetched, strict=True):
        if isinstance(outcome, BaseException):  # 兜底：单点异常降级为该点失败
            results.append({"api_type": endpoint.api_type.value, "success": False, "error": str(outcome)})
            continue
        models, error = outcome
        if error:
            results.append({"api_type": endpoint.api_type.value, "success": False, "error": error})
        else:
            results.append({"api_type": endpoint.api_type.value, "success": True, "count": len(models)})
            merged_models.extend(models)

    return {"models": sorted(set(merged_models)), "results": results}
