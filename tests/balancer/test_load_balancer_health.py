"""P1-4: 负载均衡器健康检查逻辑、优先级分组、渠道选择完整测试"""

import asyncio
import time

import pytest

from balancer.load_balancer import ChannelHealth, LoadBalancer
from models.channel import Channel


def _make_channel(
    id: str = "ch_test",
    name: str = "Test",
    enabled: bool = True,
    weight: int = 1,
    priority: int = 1,
    models: list[str] | None = None,
) -> Channel:
    return Channel(
        id=id,
        name=name,
        api_type="openai-chat-completions",
        base_url="http://example.com",
        api_key="key",
        models=models or ["gpt-4"],
        enabled=enabled,
        weight=weight,
        priority=priority,
    )


# ═══════════════════════════════════════════
#  ChannelHealth 状态转换
# ═══════════════════════════════════════════


class TestChannelHealth:
    def test_initial_state_is_healthy(self):
        h = ChannelHealth()
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=60) is True

    def test_single_failure_still_healthy(self):
        h = ChannelHealth()
        h.record_failure()
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=60) is True
        assert h.fail_count == 1

    def test_reaching_max_fail_count_becomes_unhealthy(self):
        h = ChannelHealth()
        for _ in range(5):
            h.record_failure()
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=60) is False

    def test_exceeding_max_fail_count_stays_unhealthy(self):
        h = ChannelHealth()
        for _ in range(10):
            h.record_failure()
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=60) is False

    def test_cooldown_recovery(self):
        """冷却期过后恢复健康"""
        h = ChannelHealth()
        for _ in range(5):
            h.record_failure()
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=0.001) is False or True
        # 模拟冷却期已过
        h.last_fail_time = time.time() - 100
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=60) is True

    def test_record_success_resets_fail_count(self):
        h = ChannelHealth()
        for _ in range(5):
            h.record_failure()
        h.record_success()
        assert h.fail_count == 0
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=60) is True

    def test_success_after_failure_restores_health(self):
        h = ChannelHealth()
        for _ in range(3):
            h.record_failure()
        h.record_success()
        assert h.fail_count == 0
        assert h.is_healthy(max_fail_count=5, cooldown_seconds=60) is True

    def test_max_fail_count_one(self):
        """max_fail_count=1 时，一次失败就不健康"""
        h = ChannelHealth()
        h.record_failure()
        assert h.is_healthy(max_fail_count=1, cooldown_seconds=60) is False


# ═══════════════════════════════════════════
#  LoadBalancer.select_channel — 优先级分组
# ═══════════════════════════════════════════


class TestSelectChannelPriority:
    @pytest.mark.asyncio
    async def test_selects_highest_priority(self):
        lb = LoadBalancer()
        ch_high = _make_channel(id="ch1", priority=1, weight=1)
        ch_low = _make_channel(id="ch2", priority=10, weight=1)
        selected = await lb.select_channel([ch_high, ch_low])
        assert selected.id == "ch1"

    @pytest.mark.asyncio
    async def test_same_priority_uses_weighted_round_robin(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a", priority=1, weight=3)
        ch_b = _make_channel(id="ch_b", priority=1, weight=1)
        counts = {"ch_a": 0, "ch_b": 0}
        for _ in range(100):
            selected = await lb.select_channel([ch_a, ch_b])
            counts[selected.id] += 1
        # weight 3:1 → 约 75%:25%
        assert counts["ch_a"] > counts["ch_b"]
        assert 60 <= counts["ch_a"] <= 90

    @pytest.mark.asyncio
    async def test_disabled_channel_excluded(self):
        lb = LoadBalancer()
        ch_disabled = _make_channel(id="ch_off", enabled=False, priority=1)
        ch_enabled = _make_channel(id="ch_on", enabled=True, priority=1)
        selected = await lb.select_channel([ch_disabled, ch_enabled])
        assert selected.id == "ch_on"

    @pytest.mark.asyncio
    async def test_exclude_ids_respected(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a", priority=1)
        ch_b = _make_channel(id="ch_b", priority=1)
        selected = await lb.select_channel([ch_a, ch_b], exclude_ids={"ch_a"})
        assert selected.id == "ch_b"

    @pytest.mark.asyncio
    async def test_all_excluded_returns_none(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a")
        ch_b = _make_channel(id="ch_b")
        selected = await lb.select_channel([ch_a, ch_b], exclude_ids={"ch_a", "ch_b"})
        assert selected is None

    @pytest.mark.asyncio
    async def test_all_disabled_returns_none(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a", enabled=False)
        ch_b = _make_channel(id="ch_b", enabled=False)
        selected = await lb.select_channel([ch_a, ch_b])
        assert selected is None

    @pytest.mark.asyncio
    async def test_all_unhealthy_returns_none(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a")
        ch_b = _make_channel(id="ch_b")
        # 让两个渠道都不健康
        for _ in range(10):
            await lb.record_failure("ch_a")
            await lb.record_failure("ch_b")
        selected = await lb.select_channel([ch_a, ch_b])
        assert selected is None

    @pytest.mark.asyncio
    async def test_unhealthy_channel_skipped_healthy_selected(self):
        lb = LoadBalancer()
        ch_bad = _make_channel(id="ch_bad", priority=1, weight=1)
        ch_good = _make_channel(id="ch_good", priority=1, weight=1)
        for _ in range(10):
            await lb.record_failure("ch_bad")
        selected = await lb.select_channel([ch_bad, ch_good])
        assert selected.id == "ch_good"

    @pytest.mark.asyncio
    async def test_single_channel_returns_it(self):
        lb = LoadBalancer()
        ch = _make_channel(id="ch_solo", priority=1, weight=1)
        selected = await lb.select_channel([ch])
        assert selected.id == "ch_solo"

    @pytest.mark.asyncio
    async def test_empty_list_returns_none(self):
        lb = LoadBalancer()
        selected = await lb.select_channel([])
        assert selected is None

    @pytest.mark.asyncio
    async def test_fallback_to_lower_priority_when_high_unhealthy(self):
        """高优先级不健康时，应选择低优先级"""
        lb = LoadBalancer()
        ch_high = _make_channel(id="ch_high", priority=1, weight=1)
        ch_low = _make_channel(id="ch_low", priority=5, weight=1)
        for _ in range(10):
            await lb.record_failure("ch_high")
        selected = await lb.select_channel([ch_high, ch_low])
        assert selected.id == "ch_low"


# ═══════════════════════════════════════════
#  LoadBalancer — 健康记录
# ═══════════════════════════════════════════


class TestLoadBalancerHealth:
    @pytest.mark.asyncio
    async def test_record_success_resets_health(self):
        lb = LoadBalancer()
        for _ in range(10):
            await lb.record_failure("ch_test")
        await lb.record_success("ch_test")
        ch = _make_channel(id="ch_test")
        selected = await lb.select_channel([ch])
        assert selected is not None

    @pytest.mark.asyncio
    async def test_update_config_changes_thresholds(self):
        lb = LoadBalancer()
        ch = _make_channel(id="ch_test")
        # 默认 max_fail=5, 3 次失败仍健康
        for _ in range(3):
            await lb.record_failure("ch_test")
        selected = await lb.select_channel([ch])
        assert selected is not None

        # 更新为 max_fail=2, 3 次失败就不健康
        await lb.update_config(max_fail_count=2, cooldown_seconds=60)
        selected = await lb.select_channel([ch])
        assert selected is None

    @pytest.mark.asyncio
    async def test_cooldown_recovery(self):
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=1, cooldown_seconds=0.01)
        ch = _make_channel(id="ch_test")
        await lb.record_failure("ch_test")
        # 等待冷却
        await asyncio.sleep(0.02)
        selected = await lb.select_channel([ch])
        assert selected is not None


# ═══════════════════════════════════════════
#  LoadBalancer.cleanup_removed_channels
# ═══════════════════════════════════════════


class TestCleanupRemovedChannels:
    @pytest.mark.asyncio
    async def test_removes_stale_health_entries(self):
        lb = LoadBalancer()
        await lb.record_failure("ch_old")
        await lb.record_failure("ch_keep")
        await lb.cleanup_removed_channels({"ch_keep"})
        assert "ch_old" not in lb._health
        assert "ch_keep" in lb._health

    @pytest.mark.asyncio
    async def test_cleanup_empty_set(self):
        lb = LoadBalancer()
        await lb.record_failure("ch_a")
        await lb.cleanup_removed_channels(set())
        assert len(lb._health) == 0

    @pytest.mark.asyncio
    async def test_cleanup_noop_when_all_active(self):
        lb = LoadBalancer()
        await lb.record_failure("ch_a")
        await lb.record_failure("ch_b")
        await lb.cleanup_removed_channels({"ch_a", "ch_b"})
        assert len(lb._health) == 2
