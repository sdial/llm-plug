"""stats 库不可用契约回归（ADR-0017 工单03 / D2）。

`_DB_AVAILABLE` 守卫收敛为单一守卫点后的外部契约回归：库未初始化时
查询族返回空形 / 零值、不抛异常；记账入口丢弃不抛。此前 get_overall_stats_since
在库不可用时经 _open_conn 抛 RuntimeError，是契约面上的洞——守卫收敛后
并入「空形不抛」契约，本文件把它钉住。

只断言外部可观察行为（公开查询面的返回形态），不摸 stats 私有符号。
"""

from datetime import date

import pytest
import pytest_asyncio

import config
import stats

pytestmark = pytest.mark.asyncio

_OVERALL_ZERO = {
    "total_requests": 0,
    "success_count": 0,
    "fail_count": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_cache_read_input_tokens": 0,
    "total_cache_creation_input_tokens": 0,
    "avg_latency_ms": 0,
    "avg_lag_ms": 0,
    "channels": [],
    "models": [],
    "api_keys": [],
}


@pytest_asyncio.fixture(autouse=True)
async def _unavailable_db(tmp_path, monkeypatch):
    """保证库处于未初始化态（_DB_AVAILABLE=False），全程不触任何 sqlite 文件。"""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    await stats.close_pool()
    yield
    await stats.close_pool()


async def test_overall_stats_returns_zero_shape():
    assert await stats.get_overall_stats(days=7) == _OVERALL_ZERO


async def test_overall_stats_since_returns_zero_shape():
    """守卫收敛后并入住契约：此前本入口在库不可用时抛 RuntimeError。"""
    assert await stats.get_overall_stats_since("2026-01-01 00:00:00") == _OVERALL_ZERO


async def test_today_stats_returns_zero_shape():
    assert await stats.get_today_stats() == {"overall": _OVERALL_ZERO, "daily": []}


async def test_daily_query_families_return_empty_lists():
    assert await stats.get_daily_stats() == []
    assert await stats.get_context_shaping_daily_stats() == []
    assert await stats.get_daily_stats_from_requests() == []


async def test_daily_rollup_returns_empty_shape():
    """日合并入口（ADR-0024）同入「空形不抛」契约：daily 空、兜底标记如实。"""
    assert await stats.get_daily_rollup(days=7) == {
        "daily": [],
        "fallback_used": True,
        "mode": "realtime_fallback",
        "raw_daily_count": 0,
    }


async def test_api_key_stats_returns_empty_dict():
    assert await stats.get_api_key_stats() == {}


async def test_list_requests_returns_empty_page_with_normalized_pagination():
    """空形带归一后的分页值：page 下限 1、page_size 上限 100。"""
    result = await stats.list_requests(page=0, page_size=5000)
    assert result == {"items": [], "total": 0, "page": 1, "page_size": 100}


async def test_aggregation_jobs_return_zero_shapes():
    today = date.today()
    assert await stats.aggregate_daily_stats(today, today) == {"updated_rows": 0}
    assert await stats.refresh_missing_daily_stats() == {"refreshed_dates": [], "count": 0, "debug": {"db_available": False}}
    assert await stats.refresh_stats() == {"backfilled_count": 0, "recent_refreshed_days": 0}


async def test_record_entries_discard_without_raising():
    """库不可用时记账入口静默丢弃不抛（告警契约由 test_stats_workers 回归）。"""
    stats.record_request(
        channel_id="c",
        channel_name="n",
        model="m",
        is_stream=False,
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        success=True,
    )
    stats.record_context_shaping_action(
        channel_id="c", model="m", feature="strip_ansi", action="remove_ansi", action_count=1, before_chars=10, after_chars=4
    )
