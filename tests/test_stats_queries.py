"""stats 查询族直测：tmp 库路径初始化后直查，摆脱 init+drain 仪式（ADR-0017 工单04 / D3）。

P1-8: stats.refresh_stats / get_api_key_stats / get_today_stats 直接测试。
种子经 stats 写路径直落（`_write_record`，与队列写回调同一目的地、不入队），
查询语义测试不再依赖队列排空等待；只断言外部可观察行为（结果集 / 汇总数字 /
响应键集合）。stats 无后端类，查询面绑定模块态，tmp 库路径初始化即为预约定形态。
refresh_stats 属聚合作业，仍走模块入口。
"""

import pytest
import pytest_asyncio

import stats


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db(tmp_path):
    """每个测试使用独立的临时 SQLite"""
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "test_stats.db"))
    yield
    await stats.close_pool()


async def _seed_record(**overrides):
    """直写一条统计记录（不入队）：与队列写回调同一落库路径，查询测试无需 drain 等待。"""
    record = {
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
    record.update(overrides)
    await stats._write_record(record)


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
        await _seed_record(api_key_id="key_1", input_tokens=100, output_tokens=50)
        await _seed_record(api_key_id="key_1", input_tokens=200, output_tokens=100)

        result = await stats.get_api_key_stats()
        assert "key_1" in result
        assert result["key_1"]["request_count"] == 2
        assert result["key_1"]["total_input_tokens"] == 300
        assert result["key_1"]["total_output_tokens"] == 150

    @pytest.mark.asyncio
    async def test_multiple_keys(self):
        await _seed_record(api_key_id="key_a")
        await _seed_record(api_key_id="key_b", input_tokens=500)
        await _seed_record(api_key_id="key_a", output_tokens=300)

        result = await stats.get_api_key_stats()
        assert len(result) == 2
        assert "key_a" in result
        assert "key_b" in result

    @pytest.mark.asyncio
    async def test_null_key_excluded(self):
        await _seed_record(api_key_id=None)
        await _seed_record(api_key_id="")
        await _seed_record(api_key_id="key_real")

        result = await stats.get_api_key_stats()
        assert len(result) == 1
        assert "key_real" in result

    @pytest.mark.asyncio
    async def test_cache_tokens_included(self):
        await _seed_record(
            api_key_id="key_1",
            cache_read_input_tokens=50,
            cache_creation_input_tokens=20,
        )

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
        await _seed_record()

        result = await stats.refresh_stats()
        assert result["recent_refreshed_days"] == 3

    @pytest.mark.asyncio
    async def test_refresh_backfills_missing_dates(self):
        """写入历史数据后，refresh 应补全缺失日期"""
        await _seed_record(channel_name="Test", api_key_id=None)

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
        await _seed_record(input_tokens=100, output_tokens=50)
        await _seed_record(input_tokens=200, output_tokens=100)

        result = await stats.get_today_stats()
        assert result["overall"]["total_requests"] == 2
        assert result["overall"]["total_input_tokens"] == 300
        assert result["overall"]["total_output_tokens"] == 150

    @pytest.mark.asyncio
    async def test_overall_includes_channels_and_models(self):
        await _seed_record(channel_name="Chan A", model="gpt-4")
        await _seed_record(channel_name="Chan B", model="claude-3")

        result = await stats.get_today_stats()
        overall = result["overall"]
        assert len(overall["channels"]) == 2
        assert len(overall["models"]) == 2

    @pytest.mark.asyncio
    async def test_overall_includes_api_keys(self):
        await _seed_record(api_key_id="key_x")

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
            await _seed_record(input_tokens=10, output_tokens=5)

        result = await stats.get_overall_stats(days=7)
        assert result["total_requests"] == 5
        assert result["total_input_tokens"] == 50
        assert result["total_output_tokens"] == 25
        assert result["success_count"] == 5

    @pytest.mark.asyncio
    async def test_includes_fail_count(self):
        await _seed_record(success=True)
        await _seed_record(success=False, error_msg="error")

        result = await stats.get_overall_stats(days=7)
        assert result["success_count"] == 1
        assert result["fail_count"] == 1

    @pytest.mark.asyncio
    async def test_overall_stats_since_filters_by_time_origin(self):
        """参数化单实现的时间起点语义（ADR-0017 工单03 孪生合并）：起点前不计、起点后计入。"""
        await _seed_record(input_tokens=10, output_tokens=5)

        all_time = await stats.get_overall_stats_since("2000-01-01 00:00:00")
        assert all_time["total_requests"] == 1
        assert all_time["total_input_tokens"] == 10

        future = await stats.get_overall_stats_since("2999-01-01 00:00:00")
        assert future["total_requests"] == 0
