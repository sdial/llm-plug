"""ADR-0024 工单08：get_daily_rollup 后端直测（日合并单一住所下沉 stats 层）。

沿用 test_stats_sqlite 的后端直实例化缝（tmp 库 init_db + 直写 _write_record，
无 init+drain 仪式）：切片→按天加权合并正确性、fallback 触发、today 实时覆盖
优先级、request_source 过滤透传、overall 加权均值字段（D1 后端半）。
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

import stats
from routers.admin import get_stats

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def sqlite_stats_db(tmp_path):
    db_path = tmp_path / "stats.db"
    await stats.close_pool()
    await stats.init_db(str(db_path))
    yield db_path
    await stats.stop_stats_workers()
    await stats.close_pool()


def _sample_payload(**overrides):
    payload = {
        "channel_id": "ch_1",
        "channel_name": "Primary",
        "model": "gpt-4o",
        "is_stream": False,
        "input_tokens": 12,
        "output_tokens": 8,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "latency_ms": 120,
        "success": True,
        "api_key_id": "key_a",
        "lag_ms": 25,
        "finish_reason": "stop",
    }
    payload.update(overrides)
    return payload


async def _seed_sample(**overrides):
    await stats._write_record(_sample_payload(**overrides))


_EXPECTED_DAILY_KEYS = [
    "date",
    "total_requests",
    "success_count",
    "fail_count",
    "total_input_tokens",
    "total_output_tokens",
    "total_cache_read_input_tokens",
    "total_cache_creation_input_tokens",
    "avg_latency_ms",
    "avg_lag_ms",
]


async def test_rollup_merges_slices_per_day_with_count_weighted_latency():
    """切片→按天合并：延迟均值按 request_count 加权还原（avg×count 数学沿用）。"""
    for _ in range(3):
        await _seed_sample(channel_id="ch_a", latency_ms=100, lag_ms=50)
    await _seed_sample(channel_id="ch_b", latency_ms=200, lag_ms=90)
    today = stats.agg_now().date()
    await stats.aggregate_daily_stats(today, today)

    result = await stats.get_daily_rollup(days=1)

    assert result["fallback_used"] is False
    assert result["mode"] == "daily_stats"
    assert result["raw_daily_count"] == 2  # 两个渠道切片
    assert len(result["daily"]) == 1
    rec = result["daily"][0]
    assert list(rec.keys()) == _EXPECTED_DAILY_KEYS  # 与旧路由层输出逐字段一致
    assert rec["date"] == today.isoformat()
    assert rec["total_requests"] == 4
    assert rec["success_count"] == 4
    assert rec["fail_count"] == 0
    assert rec["total_input_tokens"] == 48
    assert rec["total_output_tokens"] == 32
    # (100×3 + 200×1) / 4 = 125；(50×3 + 90×1) / 4 = 60
    assert rec["avg_latency_ms"] == 125
    assert rec["avg_lag_ms"] == 60


async def test_rollup_falls_back_to_realtime_when_daily_stats_empty():
    """daily_stats 空 → 兜底明细实时聚合，fallback 语义与调试标记随迁。"""
    for _ in range(2):
        await _seed_sample(channel_id="ch_a", latency_ms=100)
    await _seed_sample(channel_id="ch_a", latency_ms=200)

    result = await stats.get_daily_rollup(days=1)

    assert result["fallback_used"] is True
    assert result["mode"] == "realtime_fallback"
    assert len(result["daily"]) == 1
    rec = result["daily"][0]
    assert rec["total_requests"] == 3
    assert rec["avg_latency_ms"] == round((100 + 100 + 200) / 3)  # 133


async def test_rollup_today_realtime_row_overrides_aggregated_row():
    """D2 today 覆盖优先级：实时路径当天行覆盖 daily_stats 当天行。"""
    for _ in range(2):
        await _seed_sample(channel_id="ch_a", latency_ms=100)
    today = stats.agg_now().date()
    await stats.aggregate_daily_stats(today, today)  # 日聚合表当天行：2 请求 avg=100

    await _seed_sample(channel_id="ch_a", latency_ms=200)
    await _seed_sample(channel_id="ch_a", latency_ms=200)  # 实时当天：4 请求 avg=150

    result = await stats.get_daily_rollup(days=1)

    assert result["fallback_used"] is False
    assert result["mode"] == "daily_stats"
    assert len(result["daily"]) == 1
    rec = result["daily"][0]
    assert rec["total_requests"] == 4  # 实时行覆盖，非与聚合行叠加
    assert rec["avg_latency_ms"] == 150


async def test_rollup_upserts_today_when_daily_stats_lacks_today_row():
    """daily_stats 无当天行（补全机制明确排除当天）而实时有数据：today 行补位。"""
    yesterday_utc = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    await _seed_sample(channel_id="ch_hist", latency_ms=100, timestamp=yesterday_utc)
    yesterday = stats.agg_now().date() - timedelta(days=1)
    await stats.aggregate_daily_stats(yesterday, yesterday)  # 聚合表只有昨天

    await _seed_sample(channel_id="ch_today", latency_ms=300)
    await _seed_sample(channel_id="ch_today", latency_ms=400)

    result = await stats.get_daily_rollup(days=7)

    assert [rec["date"] for rec in result["daily"]] == [yesterday.isoformat(), stats.agg_now().date().isoformat()]
    assert result["daily"][0]["total_requests"] == 1
    assert result["daily"][1]["total_requests"] == 2
    assert result["daily"][1]["avg_latency_ms"] == 350


async def test_rollup_passes_request_source_filter_through():
    """request_source 过滤透传（聚合表路径与实时兜底路径同语义）。"""
    await _seed_sample(channel_id="ch_c1")
    await _seed_sample(channel_id="ch_c1", latency_ms=180)
    await _seed_sample(channel_id="ch_c2", request_source="admin_test")
    today = stats.agg_now().date()
    await stats.aggregate_daily_stats(today, today)

    client_only = await stats.get_daily_rollup(days=1, request_source="client")
    assert client_only["daily"][0]["total_requests"] == 2
    assert client_only["raw_daily_count"] == 1

    multi = await stats.get_daily_rollup(days=1, request_source=("client", "admin_test"))
    assert multi["daily"][0]["total_requests"] == 3
    assert multi["raw_daily_count"] == 2

    unfiltered = await stats.get_daily_rollup(days=1)
    assert unfiltered["daily"][0]["total_requests"] == 3

    # 实时兜底路径同样过滤：group_probe 仅存在于明细表（未聚合）→ 触发 fallback
    await _seed_sample(channel_id="ch_gp", request_source="group_probe")
    realtime_filtered = await stats.get_daily_rollup(days=1, request_source="group_probe")
    assert realtime_filtered["fallback_used"] is True
    assert realtime_filtered["mode"] == "realtime_fallback"
    assert realtime_filtered["daily"][0]["total_requests"] == 1


async def test_admin_stats_overall_carries_weighted_avg_fields():
    """D1 后端半：/admin/stats overall 新增按请求数加权的全区间均值（纯新增字段）。"""
    await _seed_sample(latency_ms=100, lag_ms=25)
    await _seed_sample(latency_ms=200, lag_ms=75)

    result = await get_stats(days=1)

    assert result["overall"]["avg_latency_ms"] == 150
    assert result["overall"]["avg_lag_ms"] == 50
    assert result["_debug"]["fallback_used"] is True  # 未跑日聚合，兜底路径
    assert result["_debug"]["mode"] == "realtime_fallback"
    assert len(result["daily"]) == 1
    assert result["daily"][0]["total_requests"] == 2


async def test_overall_stats_and_today_stats_carry_avg_fields():
    """stats 层 overall 单实现（get_overall_stats / today 同源）均携带加权均值字段。"""
    await _seed_sample(latency_ms=100, lag_ms=25)
    await _seed_sample(latency_ms=200, lag_ms=75)

    overall = await stats.get_overall_stats(days=1)
    assert overall["avg_latency_ms"] == 150
    assert overall["avg_lag_ms"] == 50

    today = await stats.get_today_stats()
    assert today["overall"]["avg_latency_ms"] == 150
    assert today["overall"]["avg_lag_ms"] == 50
