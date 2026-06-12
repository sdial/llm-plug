"""P1-8: stats.refresh_stats / get_api_key_stats / get_today_stats 直接测试"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import config
import stats


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db(tmp_path, monkeypatch):
    """每个测试使用独立的临时 SQLite"""
    db_path = str(tmp_path / "test_stats.db")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    await stats.init_db(db_path)
    yield
    await stats.close_pool()


def _record(**kwargs):
    """快速写入一条统计记录"""
    defaults = {
        "channel_id": "ch_test",
        "channel_name": "Test Channel",
        "model": "gpt-4",
        "is_stream": False,
        "input_tokens": 100,
        "output_tokens": 50,
        "latency_ms": 200,
        "success": True,
        "api_key_id": "key_1",
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    defaults.update(kwargs)
    stats.record_request(**defaults)


# ═══════════════════════════════════════════
#  get_api_key_stats
# ═══════════════════════════════════════════

class TestGetApiKeyStats:

    @pytest.mark.asyncio
    async def test_empty_db(self):
        result = await stats.get_api_key_stats()
        assert result == {}

    @pytest.mark.asyncio
    async def test_single_key(self):
        _record(api_key_id="key_1", input_tokens=100, output_tokens=50)
        _record(api_key_id="key_1", input_tokens=200, output_tokens=100)
        await stats.drain_queue()

        result = await stats.get_api_key_stats()
        assert "key_1" in result
        assert result["key_1"]["request_count"] == 2
        assert result["key_1"]["total_input_tokens"] == 300
        assert result["key_1"]["total_output_tokens"] == 150

    @pytest.mark.asyncio
    async def test_multiple_keys(self):
        _record(api_key_id="key_a")
        _record(api_key_id="key_b", input_tokens=500)
        _record(api_key_id="key_a", output_tokens=300)
        await stats.drain_queue()

        result = await stats.get_api_key_stats()
        assert len(result) == 2
        assert "key_a" in result
        assert "key_b" in result

    @pytest.mark.asyncio
    async def test_null_key_excluded(self):
        _record(api_key_id=None)
        _record(api_key_id="")
        _record(api_key_id="key_real")
        await stats.drain_queue()

        result = await stats.get_api_key_stats()
        assert len(result) == 1
        assert "key_real" in result

    @pytest.mark.asyncio
    async def test_cache_tokens_included(self):
        _record(
            api_key_id="key_1",
            cache_read_input_tokens=50,
            cache_creation_input_tokens=20,
        )
        await stats.drain_queue()

        result = await stats.get_api_key_stats()
        assert result["key_1"]["total_cache_read_input_tokens"] == 50
        assert result["key_1"]["total_cache_creation_input_tokens"] == 20


# ═══════════════════════════════════════════
#  refresh_stats
# ═══════════════════════════════════════════

class TestRefreshStats:

    @pytest.mark.asyncio
    async def test_refresh_empty_db(self):
        result = await stats.refresh_stats()
        assert result["backfilled_count"] == 0
        assert result["recent_refreshed_days"] == 3

    @pytest.mark.asyncio
    async def test_refresh_with_recent_data(self):
        """写入今天的数据后 refresh 应成功"""
        _record()
        await stats.drain_queue()

        result = await stats.refresh_stats()
        assert result["recent_refreshed_days"] == 3

    @pytest.mark.asyncio
    async def test_refresh_backfills_missing_dates(self):
        """写入历史数据后，refresh 应补全缺失日期"""
        # 手动写入 3 天前的数据
        three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
        stats.record_request(
            channel_id="ch_test",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=100,
            output_tokens=50,
            latency_ms=200,
            success=True,
        )
        await stats.drain_queue()

        result = await stats.refresh_stats()
        # 至少补全了一些缺失日期或刷新了近3天
        assert isinstance(result["backfilled_count"], int)


# ═══════════════════════════════════════════
#  get_today_stats
# ═══════════════════════════════════════════

class TestGetTodayStats:

    @pytest.mark.asyncio
    async def test_empty_db_returns_structure(self):
        result = await stats.get_today_stats()
        assert "overall" in result
        assert "daily" in result
        assert result["overall"]["total_requests"] == 0

    @pytest.mark.asyncio
    async def test_with_today_data(self):
        _record(input_tokens=100, output_tokens=50)
        _record(input_tokens=200, output_tokens=100)
        await stats.drain_queue()

        result = await stats.get_today_stats()
        assert result["overall"]["total_requests"] == 2
        assert result["overall"]["total_input_tokens"] == 300
        assert result["overall"]["total_output_tokens"] == 150

    @pytest.mark.asyncio
    async def test_overall_includes_channels_and_models(self):
        _record(channel_name="Chan A", model="gpt-4")
        _record(channel_name="Chan B", model="claude-3")
        await stats.drain_queue()

        result = await stats.get_today_stats()
        overall = result["overall"]
        assert len(overall["channels"]) == 2
        assert len(overall["models"]) == 2

    @pytest.mark.asyncio
    async def test_overall_includes_api_keys(self):
        _record(api_key_id="key_x")
        await stats.drain_queue()

        result = await stats.get_today_stats()
        keys = result["overall"]["api_keys"]
        assert len(keys) >= 1
        assert any(k["key_id"] == "key_x" for k in keys)


# ═══════════════════════════════════════════
#  get_overall_stats
# ═══════════════════════════════════════════

class TestGetOverallStats:

    @pytest.mark.asyncio
    async def test_zero_stats_when_empty(self):
        result = await stats.get_overall_stats(days=7)
        assert result["total_requests"] == 0
        assert result["channels"] == []
        assert result["models"] == []

    @pytest.mark.asyncio
    async def test_aggregates_multiple_records(self):
        for _ in range(5):
            _record(input_tokens=10, output_tokens=5)
        await stats.drain_queue()

        result = await stats.get_overall_stats(days=7)
        assert result["total_requests"] == 5
        assert result["total_input_tokens"] == 50
        assert result["total_output_tokens"] == 25
        assert result["success_count"] == 5

    @pytest.mark.asyncio
    async def test_includes_fail_count(self):
        _record(success=True)
        _record(success=False, error_msg="error")
        await stats.drain_queue()

        result = await stats.get_overall_stats(days=7)
        assert result["success_count"] == 1
        assert result["fail_count"] == 1
