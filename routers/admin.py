import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

import httpx

from client import get_upstream_headers, remove_channel_client
from models.api_key import ApiKey, ApiKeyCreate, ApiKeyUpdate
from models.channel import Channel, ChannelCreate, ChannelUpdate
from models.model_group import LBConfig, ModelGroup, ModelGroupCreate, ModelGroupUpdate
from datetime import date, datetime
from proxy_core import _get_upstream_url

from stats import (
    get_daily_stats, get_daily_stats_from_requests,
    get_overall_stats, list_requests,
    aggregate_daily_stats,
    refresh_missing_daily_stats, get_request_field, refresh_stats,
)
from storage import (
    load_api_keys, load_data, save_api_keys, save_data, invalidate_keys_cache,
    load_model_groups,
    get_lb_config, save_lb_config,
)

LOGS_DIR = Path(__file__).parent.parent / "logs"
STATIC_DIR = Path(__file__).parent.parent / "static"

router = APIRouter(prefix="/admin", tags=["管理"])


async def _get_channels() -> list[Channel]:
    data = await load_data()
    return [Channel(**ch) for ch in data.get("channels", [])]


async def _save_channels(channels: list[Channel]):
    data = await load_data()
    data["channels"] = [ch.model_dump() for ch in channels]
    await save_data(data)


@router.get("/channels")
async def list_channels():
    """获取所有渠道（API Key 脱敏）"""
    channels = await _get_channels()
    result = []
    for ch in channels:
        d = ch.model_dump()
        if d.get("api_key"):
            key = d["api_key"]
            d["api_key"] = key[:4] + "***" if len(key) > 4 else "***"
        result.append(d)
    return result


@router.post("/channels", response_model=Channel)
async def create_channel(body: ChannelCreate):
    """添加渠道"""
    channels = await _get_channels()
    channel = Channel(**body.model_dump())
    channels.append(channel)
    await _save_channels(channels)
    return channel


@router.put("/channels/{channel_id}", response_model=Channel)
async def update_channel(channel_id: str, body: ChannelUpdate):
    """更新渠道"""
    channels = await _get_channels()
    for i, ch in enumerate(channels):
        if ch.id == channel_id:
            update_data = body.model_dump(exclude_unset=True)
            updated = ch.model_copy(update=update_data)
            channels[i] = updated
            await _save_channels(channels)
            old_channel = ch
            break
    else:
        raise HTTPException(status_code=404, detail="渠道不存在")
    await remove_channel_client(old_channel)
    return updated


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    """删除渠道"""
    channels = await _get_channels()
    new_channels = [ch for ch in channels if ch.id != channel_id]
    if len(new_channels) == len(channels):
        raise HTTPException(status_code=404, detail="渠道不存在")
    removed_channel = next((ch for ch in channels if ch.id == channel_id), None)
    await _save_channels(new_channels)
    if removed_channel:
        await remove_channel_client(removed_channel)
    return {"message": "删除成功"}


@router.patch("/channels/{channel_id}/toggle", response_model=Channel)
async def toggle_channel(channel_id: str):
    """启用/禁用渠道"""
    channels = await _get_channels()
    for i, ch in enumerate(channels):
        if ch.id == channel_id:
            updated = ch.model_copy(update={"enabled": not ch.enabled})
            channels[i] = updated
            await _save_channels(channels)
            old_channel = ch
            break
    else:
        raise HTTPException(status_code=404, detail="渠道不存在")
    await remove_channel_client(old_channel)
    return updated


# ============ API Keys CRUD ============


async def _get_api_keys() -> list[ApiKey]:
    data = await load_api_keys()
    return [ApiKey(**k) for k in data.get("api_keys", [])]


async def _save_api_keys(keys: list[ApiKey]):
    await save_api_keys({"api_keys": [k.model_dump() for k in keys]})


@router.get("/api-keys")
async def list_api_keys():
    """获取所有 API Key（Key 脱敏），统计数据从 PG 聚合"""
    import stats as _stats

    keys = await _get_api_keys()
    key_stats = await _stats.get_api_key_stats()
    result = []
    for k in keys:
        d = k.model_dump()
        raw = d.get("key", "")
        d["key"] = raw[:8] + "***" if len(raw) > 8 else "***"
        lookup = k.name or k.id
        s = key_stats.get(lookup, {})
        d["request_count"] = s.get("request_count", 0)
        d["total_input_tokens"] = s.get("total_input_tokens", 0)
        d["total_output_tokens"] = s.get("total_output_tokens", 0)
        result.append(d)
    return result


@router.post("/api-keys", response_model=ApiKey)
async def create_api_key(body: ApiKeyCreate):
    """添加 API Key"""
    keys = await _get_api_keys()
    data = body.model_dump(exclude_none=True)
    key = ApiKey(**data)
    keys.append(key)
    await _save_api_keys(keys)
    await invalidate_keys_cache()
    return key


@router.put("/api-keys/{key_id}", response_model=ApiKey)
async def update_api_key(key_id: str, body: ApiKeyUpdate):
    """更新 API Key"""
    keys = await _get_api_keys()
    for i, k in enumerate(keys):
        if k.id == key_id:
            update_data = body.model_dump(exclude_unset=True)
            updated = k.model_copy(update=update_data)
            keys[i] = updated
            await _save_api_keys(keys)
            await invalidate_keys_cache()
            return updated
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.delete("/api-keys/{key_id}")
async def delete_api_key(key_id: str):
    """删除 API Key"""
    keys = await _get_api_keys()
    new_keys = [k for k in keys if k.id != key_id]
    if len(new_keys) == len(keys):
        raise HTTPException(status_code=404, detail="API Key 不存在")
    await _save_api_keys(new_keys)
    await invalidate_keys_cache()
    return {"message": "删除成功"}


@router.get("/api-keys/{key_id}/key")
async def get_api_key_value(key_id: str):
    """获取 API Key 的完整值（用于复制）"""
    keys = await _get_api_keys()
    for k in keys:
        if k.id == key_id:
            return {"key": k.key}
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.patch("/api-keys/{key_id}/regenerate", response_model=ApiKey)
async def regenerate_api_key(key_id: str):
    """重新生成 API Key"""
    keys = await _get_api_keys()
    for i, k in enumerate(keys):
        if k.id == key_id:
            new_key_value = f"sk-{secrets.token_hex(24)}"
            updated = k.model_copy(update={"key": new_key_value})
            keys[i] = updated
            await _save_api_keys(keys)
            await invalidate_keys_cache()
            return updated
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, model: str | None = Query(default=None)):
    """测试渠道连通性：发送最简prompt，检查返回"""
    channels = await _get_channels()
    channel = next((ch for ch in channels if ch.id == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")

    if not channel.models:
        return {"success": False, "message": "渠道无可用模型", "latency_ms": None}

    if model:
        if model not in channel.models:
            return {"success": False, "message": f"模型 '{model}' 不在此渠道的模型列表中", "latency_ms": None}
        test_model = model
    else:
        test_model = channel.models[0]
    api_type = channel.api_type.value

    url = _get_upstream_url(channel)

    if api_type == "openai-chat-completions":
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    elif api_type == "openai-response":
        payload = {
            "model": test_model,
            "input": "Hi",
            "max_output_tokens": 5,
        }
    elif api_type == "anthropic":
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    else:
        return {"success": False, "message": f"不支持的API类型: {api_type}", "latency_ms": None}

    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    if channel.socks5_proxy:
        test_client = httpx.AsyncClient(
            proxy=channel.socks5_proxy,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    else:
        test_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    start = time.monotonic()
    try:
        resp = await test_client.post(url, json=payload, headers=headers)
        latency_ms = round((time.monotonic() - start) * 1000)
        resp.raise_for_status()
        data = resp.json()

        if api_type == "openai-chat-completions":
            choices = data.get("choices", [])
            ok = bool(choices) and choices[0].get("message", {}).get("content") is not None
            reply = choices[0]["message"]["content"][:100] if ok else str(data)[:200]
        elif api_type == "openai-response":
            output = data.get("output", [])
            ok = bool(output)
            reply = str(output[0])[:100] if ok else str(data)[:200]
        elif api_type == "anthropic":
            content = data.get("content", [])
            ok = bool(content)
            reply = content[0].get("text", "")[:100] if ok else str(data)[:200]
        else:
            ok = True
            reply = str(data)[:200]

        return {
            "success": ok,
            "message": "测试通过" if ok else "返回数据格式异常",
            "model": test_model,
            "latency_ms": latency_ms,
            "reply": reply,
        }
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "success": False,
            "message": f"请求失败: {str(e)}",
            "model": test_model,
            "latency_ms": latency_ms,
            "reply": None,
        }
    finally:
        await test_client.aclose()


# ============ Logs API ============


@router.get("/logs")
async def list_logs():
    """列出所有日志文件"""
    if not LOGS_DIR.exists():
        return []
    files = sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size} for f in files]


@router.get("/logs/{filename}")
async def get_log(filename: str):
    """获取日志文件内容"""
    file_path = (LOGS_DIR / filename).resolve()
    if not file_path.is_relative_to(LOGS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="禁止访问")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, media_type="application/jsonl")


# ============ Stats API ============


@router.get("/stats")
async def get_stats(days: int = Query(default=7, ge=1)):
    """获取统计数据"""
    overall = await get_overall_stats(days=days)
    raw_daily = await get_daily_stats(days=days)
    fallback_used = not bool(raw_daily)
    if fallback_used:
        raw_daily = await get_daily_stats_from_requests(days=days)
    daily_by_date: dict[str, dict] = {}
    for row in raw_daily:
        d = str(row["date"])
        if d not in daily_by_date:
            daily_by_date[d] = {
                "date": d,
                "total_requests": 0,
                "success_count": 0,
                "fail_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_latency_ms": 0,
                "total_lag_ms": 0,
                "latency_count": 0,
            }
        rec = daily_by_date[d]
        rec["total_requests"] += row["request_count"] or 0
        rec["success_count"] += row["success_count"] or 0
        rec["fail_count"] += row["fail_count"] or 0
        rec["total_input_tokens"] += row["input_tokens"] or 0
        rec["total_output_tokens"] += row["output_tokens"] or 0
        if row.get("avg_latency_ms") is not None:
            rec["total_latency_ms"] += row["avg_latency_ms"] * (row["request_count"] or 1)
            rec["latency_count"] += row["request_count"] or 1
        if row.get("avg_lag_ms") is not None:
            rec["total_lag_ms"] += row["avg_lag_ms"] * (row["request_count"] or 1)
    daily = []
    for rec in daily_by_date.values():
        avg_latency = round(rec.pop("total_latency_ms") / rec["latency_count"]) if rec["latency_count"] else 0
        avg_lag = round(rec.pop("total_lag_ms") / rec["latency_count"]) if rec["latency_count"] else 0
        rec.pop("latency_count")
        rec["avg_latency_ms"] = avg_latency
        rec["avg_lag_ms"] = avg_lag
        daily.append(rec)
    daily.sort(key=lambda r: r["date"])

    return {
        "overall": overall,
        "daily": daily,
        "_debug": {
            "server_now": datetime.now().isoformat(),
            "query_days": days,
            "raw_daily_count": len(raw_daily),
            "fallback_used": fallback_used,
        },
    }


@router.post("/stats/refresh/daily")
async def refresh_daily_stats_endpoint():
    """补全缺失的日聚合统计（不含当天）"""
    result = await refresh_missing_daily_stats()
    msg = f"已刷新 {result['count']} 天的日聚合统计"
    if result.get("debug"):
        msg += f" | 服务器日期: {result['debug'].get('today', 'N/A')}"
        msg += f" | requests日期: {', '.join(result['debug'].get('request_dates', []))}"
        msg += f" | 缺失日期: {', '.join(result['debug'].get('missing_dates', []))}"
    return {"message": msg, **result}


@router.post("/stats/refresh")
async def refresh_stats_endpoint():
    """补全缺失历史聚合 + 强制刷新近3天日聚合"""
    result = await refresh_stats()
    return result


@router.post("/stats/aggregate/daily")
async def trigger_daily_aggregation(
    start_date: date, end_date: date,
):
    result = await aggregate_daily_stats(start_date, end_date)
    return {"message": f"已更新 {result['updated_rows']} 条日聚合记录", **result}


@router.get("/requests")
async def list_requests_endpoint(
    model: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    success: bool | None = Query(default=None),
    api_key_id: str | None = Query(default=None),
    is_stream: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
):
    """查询请求记录（支持分页和过滤）"""
    result = await list_requests(
        model=model,
        channel=channel,
        start=start,
        end=end,
        success=success,
        api_key_id=api_key_id,
        is_stream=is_stream,
        page=page,
        page_size=page_size,
    )
    return result


_FIELD_PATH_MAP = {
    "request-headers": "request_headers",
    "request-body": "request_body",
    "response-headers": "response_headers",
    "response-body": "response_body",
}


@router.get("/requests/{request_id}/{field_name}")
async def get_request_field_endpoint(request_id: int, field_name: str):
    """获取单个请求的单个 JSONB 字段（请求/返回的 Header 或 Body）"""
    field = _FIELD_PATH_MAP.get(field_name)
    if field is None:
        raise HTTPException(status_code=400, detail=f"不支持的字段: {field_name}")
    result = await get_request_field(request_id, field)
    if result is None:
        raise HTTPException(status_code=404, detail="请求记录不存在")
    return result


# ============ 模型组 CRUD ============


@router.get("/model-groups")
async def list_model_groups():
    """获取所有模型组"""
    return await load_model_groups()


@router.post("/model-groups", response_model=ModelGroup)
async def create_model_group(body: ModelGroupCreate):
    """创建模型组"""
    groups = await load_model_groups()
    if any(g.name == body.name for g in groups):
        raise HTTPException(status_code=400, detail="模型组名称已存在")
    group = ModelGroup(**body.model_dump())
    groups.append(group)
    from storage import save_model_groups
    await save_model_groups(groups)
    return group


@router.put("/model-groups/{group_id}", response_model=ModelGroup)
async def update_model_group_endpoint(group_id: str, body: ModelGroupUpdate):
    """更新模型组"""
    groups = await load_model_groups()
    for i, g in enumerate(groups):
        if g.id == group_id:
            update_data = body.model_dump(exclude_unset=True)
            if "name" in update_data:
                if any(other.id != group_id and other.name == update_data["name"] for other in groups):
                    raise HTTPException(status_code=400, detail="模型组名称已存在")
            updated = g.model_copy(update=update_data)
            groups[i] = updated
            from storage import save_model_groups
            await save_model_groups(groups)
            return updated
    raise HTTPException(status_code=404, detail="模型组不存在")


@router.delete("/model-groups/{group_id}")
async def delete_model_group_endpoint(group_id: str):
    """删除模型组"""
    groups = await load_model_groups()
    new_groups = [g for g in groups if g.id != group_id]
    if len(new_groups) == len(groups):
        raise HTTPException(status_code=404, detail="模型组不存在")
    from storage import save_model_groups
    await save_model_groups(new_groups)
    return {"message": "删除成功"}


@router.patch("/model-groups/{group_id}/toggle", response_model=ModelGroup)
async def toggle_model_group(group_id: str):
    """启用/禁用模型组"""
    groups = await load_model_groups()
    for i, g in enumerate(groups):
        if g.id == group_id:
            updated = g.model_copy(update={"enabled": not g.enabled})
            groups[i] = updated
            from storage import save_model_groups
            await save_model_groups(groups)
            return updated
    raise HTTPException(status_code=404, detail="模型组不存在")


# ============ 负载均衡配置 ============


@router.get("/lb-config", response_model=LBConfig)
async def get_lb_config_endpoint():
    """获取负载均衡全局配置"""
    return await get_lb_config()


@router.put("/lb-config", response_model=LBConfig)
async def update_lb_config_endpoint(body: LBConfig):
    """更新负载均衡全局配置"""
    await save_lb_config(body)
    return body
