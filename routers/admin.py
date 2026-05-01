import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

import httpx

from client import get_upstream_headers, remove_channel_client
from models.api_key import ApiKey, ApiKeyCreate, ApiKeyUpdate
from models.channel import Channel, ChannelCreate, ChannelUpdate
from datetime import date, datetime, timedelta

from stats import (
    get_daily_stats, get_daily_stats_from_requests,
    get_overall_stats, get_hourly_stats, get_hourly_stats_from_requests,
    aggregate_hourly_stats, aggregate_daily_stats, list_requests,
    refresh_missing_daily_stats, get_request_field,
)
from storage import load_api_keys, load_data, save_api_keys, save_data, get_lock, invalidate_keys_cache

LOGS_DIR = Path(__file__).parent.parent / "logs"
STATIC_DIR = Path(__file__).parent.parent / "static"

router = APIRouter(prefix="/admin", tags=["管理"])


def _get_channels() -> list[Channel]:
    data = load_data()
    return [Channel(**ch) for ch in data.get("channels", [])]


def _save_channels(channels: list[Channel]):
    save_data({"channels": [ch.model_dump() for ch in channels]})


@router.get("/channels")
def list_channels():
    """获取所有渠道（API Key 脱敏）"""
    channels = _get_channels()
    result = []
    for ch in channels:
        d = ch.model_dump()
        if d.get("api_key"):
            key = d["api_key"]
            d["api_key"] = key[:4] + "***" if len(key) > 4 else "***"
        result.append(d)
    return result


@router.post("/channels", response_model=Channel)
def create_channel(body: ChannelCreate):
    """添加渠道"""
    with get_lock():
        channels = _get_channels()
        channel = Channel(**body.model_dump())
        channels.append(channel)
        _save_channels(channels)
    return channel


@router.put("/channels/{channel_id}", response_model=Channel)
async def update_channel(channel_id: str, body: ChannelUpdate):
    """更新渠道"""
    with get_lock():
        channels = _get_channels()
        for i, ch in enumerate(channels):
            if ch.id == channel_id:
                update_data = body.model_dump(exclude_unset=True)
                updated = ch.model_copy(update=update_data)
                channels[i] = updated
                _save_channels(channels)
                old_channel = ch
                break
        else:
            raise HTTPException(status_code=404, detail="渠道不存在")
    # 渠道配置变更（base_url/socks5_proxy）时刷新客户端缓存（锁外执行）
    await remove_channel_client(old_channel)
    return updated


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    """删除渠道"""
    with get_lock():
        channels = _get_channels()
        new_channels = [ch for ch in channels if ch.id != channel_id]
        if len(new_channels) == len(channels):
            raise HTTPException(status_code=404, detail="渠道不存在")
        # 记录被删除的渠道用于锁外清理
        removed_channel = next((ch for ch in channels if ch.id == channel_id), None)
        _save_channels(new_channels)
    # 移除被删除渠道的客户端缓存（锁外执行）
    if removed_channel:
        await remove_channel_client(removed_channel)
    return {"message": "删除成功"}


@router.patch("/channels/{channel_id}/toggle", response_model=Channel)
async def toggle_channel(channel_id: str):
    """启用/禁用渠道"""
    with get_lock():
        channels = _get_channels()
        for i, ch in enumerate(channels):
            if ch.id == channel_id:
                updated = ch.model_copy(update={"enabled": not ch.enabled})
                channels[i] = updated
                _save_channels(channels)
                old_channel = ch
                break
        else:
            raise HTTPException(status_code=404, detail="渠道不存在")
    # 渠道配置变更时刷新客户端缓存（锁外执行）
    await remove_channel_client(old_channel)
    return updated


# ============ API Keys CRUD ============


def _get_api_keys() -> list[ApiKey]:
    data = load_api_keys()
    return [ApiKey(**k) for k in data.get("api_keys", [])]


def _save_api_keys(keys: list[ApiKey]):
    save_api_keys({"api_keys": [k.model_dump() for k in keys]})


@router.get("/api-keys")
def list_api_keys():
    """获取所有 API Key（Key 脱敏）"""
    keys = _get_api_keys()
    result = []
    for k in keys:
        d = k.model_dump()
        raw = d.get("key", "")
        d["key"] = raw[:8] + "***" if len(raw) > 8 else "***"
        result.append(d)
    return result


@router.post("/api-keys", response_model=ApiKey)
def create_api_key(body: ApiKeyCreate):
    """添加 API Key"""
    with get_lock():
        keys = _get_api_keys()
        data = body.model_dump(exclude_none=True)
        key = ApiKey(**data)
        keys.append(key)
        _save_api_keys(keys)
        invalidate_keys_cache()
    return key


@router.put("/api-keys/{key_id}", response_model=ApiKey)
def update_api_key(key_id: str, body: ApiKeyUpdate):
    """更新 API Key"""
    with get_lock():
        keys = _get_api_keys()
        for i, k in enumerate(keys):
            if k.id == key_id:
                update_data = body.model_dump(exclude_unset=True)
                updated = k.model_copy(update=update_data)
                keys[i] = updated
                _save_api_keys(keys)
                invalidate_keys_cache()
                return updated
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.delete("/api-keys/{key_id}")
def delete_api_key(key_id: str):
    """删除 API Key"""
    with get_lock():
        keys = _get_api_keys()
        new_keys = [k for k in keys if k.id != key_id]
        if len(new_keys) == len(keys):
            raise HTTPException(status_code=404, detail="API Key 不存在")
        _save_api_keys(new_keys)
        invalidate_keys_cache()
    return {"message": "删除成功"}


@router.get("/api-keys/{key_id}/key")
def get_api_key_value(key_id: str):
    """获取 API Key 的完整值（用于复制）"""
    keys = _get_api_keys()
    for k in keys:
        if k.id == key_id:
            return {"key": k.key}
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.patch("/api-keys/{key_id}/regenerate", response_model=ApiKey)
def regenerate_api_key(key_id: str):
    """重新生成 API Key"""
    with get_lock():
        keys = _get_api_keys()
        for i, k in enumerate(keys):
            if k.id == key_id:
                new_key_value = f"sk-{secrets.token_hex(24)}"
                updated = k.model_copy(update={"key": new_key_value})
                keys[i] = updated
                _save_api_keys(keys)
                invalidate_keys_cache()
                return updated
    raise HTTPException(status_code=404, detail="API Key 不存在")


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, model: str | None = Query(default=None)):
    """测试渠道连通性：发送最简prompt，检查返回"""
    channels = _get_channels()
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
    base = channel.base_url.rstrip("/")

    if api_type == "openai-chat-completions":
        url = f"{base}/v1/chat/completions"
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    elif api_type == "openai-response":
        url = f"{base}/v1/responses"
        payload = {
            "model": test_model,
            "input": "Hi",
            "max_output_tokens": 5,
        }
    elif api_type == "anthropic":
        url = f"{base}/v1/messages"
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    else:
        return {"success": False, "message": f"不支持的API类型: {api_type}", "latency_ms": None}

    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    # 测试使用独立客户端（非缓存），可安全关闭
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
def list_logs():
    """列出所有日志文件"""
    if not LOGS_DIR.exists():
        return []
    files = sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size} for f in files]


@router.get("/logs/{filename}")
def get_log(filename: str):
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

    # 小时级统计（最近24小时）
    now = datetime.now()
    start_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    raw_hourly = await get_hourly_stats(start_time=start_hour)
    if not raw_hourly:
        raw_hourly = await get_hourly_stats_from_requests(start_time=start_hour)
    hourly_by_time: dict[str, dict] = {}
    for row in raw_hourly:
        h = row["hour"]
        if isinstance(h, datetime):
            h_str = h.strftime("%Y-%m-%d %H:00")
        else:
            h_str = str(h)[:13] + ":00" if len(str(h)) >= 13 else str(h)
        if h_str not in hourly_by_time:
            hourly_by_time[h_str] = {
                "hour": h_str,
                "total_requests": 0,
                "success_count": 0,
                "fail_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_latency_ms": 0,
                "total_lag_ms": 0,
                "latency_count": 0,
            }
        rec = hourly_by_time[h_str]
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
    hourly = []
    for rec in hourly_by_time.values():
        avg_latency = round(rec.pop("total_latency_ms") / rec["latency_count"]) if rec["latency_count"] else 0
        avg_lag = round(rec.pop("total_lag_ms") / rec["latency_count"]) if rec["latency_count"] else 0
        rec.pop("latency_count")
        rec["avg_latency_ms"] = avg_latency
        rec["avg_lag_ms"] = avg_lag
        hourly.append(rec)
    hourly.sort(key=lambda r: r["hour"])

    return {
        "overall": overall,
        "daily": daily,
        "hourly": hourly,
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


@router.post("/stats/aggregate/hourly")
async def trigger_hourly_aggregation(
    start_time: datetime,
    end_time: datetime,
):
    result = await aggregate_hourly_stats(start_time, end_time)
    return {"message": f"已更新 {result['updated_rows']} 条小时聚合记录", **result}


@router.post("/stats/aggregate/daily")
async def trigger_daily_aggregation(
    start_date: date,
    end_date: date,
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


# URL 路径字段名 → stats.get_request_field 的 field 参数名
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
