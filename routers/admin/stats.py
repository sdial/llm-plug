"""统计域路由：stats / today / refresh / aggregate。"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from stats import (
    agg_now,
    aggregate_daily_stats,
    get_context_shaping_view,
    get_daily_rollup,
    get_overall_stats,
    get_overall_stats_since,
    get_today_stats,
    local_date_to_utc_iso,
    refresh_missing_daily_stats,
    refresh_stats,
)

from .common import AdminAuthRoute, _parsed_request_sources

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


@router.get("/stats")
async def get_stats(
    days: Annotated[int, Query(ge=1, le=3660)] = 7,
    range: Annotated[str | None, Query()] = None,
    request_source: Annotated[str | tuple[str, ...] | None, Query()] = None,
):
    """获取统计数据。request_source 可选，透传给日聚合查询；未传时不过滤（响应结构不变）。"""
    parsed_sources = _parsed_request_sources(request_source)
    range_type = range or ""
    n_days = days
    if range_type in ("this_week", "this_month"):
        now_local = agg_now()
        start_date = now_local.date() - timedelta(days=now_local.weekday()) if range_type == "this_week" else now_local.date().replace(day=1)
        n_days = (now_local.date() - start_date).days + 1
        since_utc = local_date_to_utc_iso(start_date)
        overall = await get_overall_stats_since(since=since_utc)
    else:
        overall = await get_overall_stats(days=days)

    # 日合并/today 覆盖单一住所 stats 层（ADR-0024 D0/D2）：router 只透传
    rollup = await get_daily_rollup(days=n_days, request_source=parsed_sources)

    return {
        "overall": overall,
        "daily": rollup["daily"],
        "_debug": {
            "server_now": datetime.now(UTC).isoformat(),
            "query_days": n_days,
            "range": range_type,
            "raw_daily_count": rollup["raw_daily_count"],
            "fallback_used": rollup["fallback_used"],
            "mode": rollup["mode"],
        },
    }


@router.get("/stats/today")
async def get_stats_today():
    """获取今天（东8区0点至今）的实时统计数据"""
    data = await get_today_stats()
    return {
        "overall": data["overall"],
        "daily": data["daily"],
        "_debug": {
            "server_now": datetime.now(UTC).isoformat(),
            "mode": "today_realtime",
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
    start_date: date,
    end_date: date,
):
    result = await aggregate_daily_stats(start_date, end_date)
    return {"message": f"已更新 {result['updated_rows']} 条日聚合记录", **result}


@router.get("/stats/context-shaping")
async def get_context_shaping_stats(
    days: Annotated[int, Query(ge=1)] = 7,
    channel_id: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    feature: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
):
    return await get_context_shaping_view(
        days=days,
        channel_id=channel_id,
        model=model,
        feature=feature,
        action=action,
    )
