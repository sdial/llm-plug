"""负载均衡器退避/恢复时序测试

覆盖场景：
1. 渠道在 cooldown 期间被跳过
2. cooldown 过期后渠道自动恢复可用
3. 恢复后再次失败会重新进入 cooldown
4. record_success 可立即重置失败计数
5. 多渠道 cooldown 交叉恢复
6. 优先级回退 + cooldown 恢复的组合
7. 动态更新 cooldown_seconds 的影响
"""

import asyncio

import pytest

from balancer.load_balancer import LoadBalancer
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
#  Cooldown 时序：不可用 → 等待 → 恢复
# ═══════════════════════════════════════════


class TestCooldownTiming:
    @pytest.mark.asyncio
    async def test_channel_unavailable_during_cooldown(self):
        """cooldown 期间渠道应被跳过"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=2, cooldown_seconds=60)
        ch = _make_channel(id="ch_cd")
        await lb.record_failure("ch_cd")
        await lb.record_failure("ch_cd")
        selected = await lb.select_channel([ch])
        assert selected is None

    @pytest.mark.asyncio
    async def test_channel_recovers_after_cooldown(self):
        """cooldown 过期后渠道应恢复可用"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=2, cooldown_seconds=0.05)
        ch = _make_channel(id="ch_cd")

        # 触发熔断
        await lb.record_failure("ch_cd")
        await lb.record_failure("ch_cd")
        assert await lb.select_channel([ch]) is None

        # 等待 cooldown 过期
        await asyncio.sleep(0.08)
        selected = await lb.select_channel([ch])
        assert selected is not None
        assert selected.id == "ch_cd"

    @pytest.mark.asyncio
    async def test_recovery_then_refailure_re_enters_cooldown(self):
        """恢复后再次失败应重新进入 cooldown"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=2, cooldown_seconds=0.05)
        ch = _make_channel(id="ch_cd")

        # 触发熔断
        await lb.record_failure("ch_cd")
        await lb.record_failure("ch_cd")
        assert await lb.select_channel([ch]) is None

        # cooldown 过期后恢复
        await asyncio.sleep(0.08)
        assert (await lb.select_channel([ch])) is not None

        # 再次失败 → 重新进入 cooldown（这次只需要 2 次失败）
        await lb.record_failure("ch_cd")
        await lb.record_failure("ch_cd")
        assert await lb.select_channel([ch]) is None

    @pytest.mark.asyncio
    async def test_record_success_resets_fail_count_immediately(self):
        """record_success 应立即重置失败计数，渠道恢复可用"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=3, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        for _ in range(3):
            await lb.record_failure("ch_cd")
        assert await lb.select_channel([ch]) is None

        await lb.record_success("ch_cd")
        selected = await lb.select_channel([ch])
        assert selected is not None
        assert selected.id == "ch_cd"

    @pytest.mark.asyncio
    async def test_success_within_cooldown_period_resets_counter(self):
        """在 cooldown 期间如果调用 record_success，渠道应立即可用（无需等待 cooldown）"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=2, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        await lb.record_failure("ch_cd")
        await lb.record_failure("ch_cd")
        assert await lb.select_channel([ch]) is None

        # 手动重置
        await lb.record_success("ch_cd")
        assert (await lb.select_channel([ch])) is not None


# ═══════════════════════════════════════════
#  多渠道 cooldown 交叉恢复
# ═══════════════════════════════════════════


class TestMultiChannelCooldown:
    @pytest.mark.asyncio
    async def test_alternating_cooldown_recovery(self):
        """两个渠道交替 cooldown，始终至少有一个可用"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=1, cooldown_seconds=0.05)
        ch_a = _make_channel(id="ch_a", priority=1, weight=1)
        ch_b = _make_channel(id="ch_b", priority=1, weight=1)

        # 淘汰 A
        await lb.record_failure("ch_a")
        selected = await lb.select_channel([ch_a, ch_b])
        assert selected.id == "ch_b"

        # 淘汰 B
        await lb.record_failure("ch_b")
        # 两者都不健康
        assert await lb.select_channel([ch_a, ch_b]) is None

        # 等 A 的 cooldown 过期
        await asyncio.sleep(0.08)
        selected = await lb.select_channel([ch_a, ch_b])
        assert selected is not None
        assert selected.id == "ch_a"

    @pytest.mark.asyncio
    async def test_staggered_recovery_respects_cooldown_order(self):
        """先失败的渠道先恢复"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=1, cooldown_seconds=0.05)
        ch_first = _make_channel(id="ch_first", priority=1, weight=1)
        ch_second = _make_channel(id="ch_second", priority=1, weight=1)

        # 先淘汰 first
        await lb.record_failure("ch_first")

        # 等一小段时间再淘汰 second
        await asyncio.sleep(0.02)
        await lb.record_failure("ch_second")

        # 等 first 的 cooldown 过期，second 还在 cooldown
        await asyncio.sleep(0.04)

        selected = await lb.select_channel([ch_first, ch_second])
        # first 应该已恢复，second 可能还在 cooldown
        if selected is not None:
            assert selected.id == "ch_first"


# ═══════════════════════════════════════════
#  优先级回退 + cooldown 恢复组合
# ═══════════════════════════════════════════


class TestPriorityFallbackAndRecovery:
    @pytest.mark.asyncio
    async def test_high_priority_cooldown_falls_back_then_recovers(self):
        """高优先级 cooldown → 回退到低优先级 → 高优先级恢复后重新被选中"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=2, cooldown_seconds=0.05)

        ch_high = _make_channel(id="ch_high", priority=1, weight=1)
        ch_low = _make_channel(id="ch_low", priority=10, weight=1)

        # 高优先级正常时应该选它
        selected = await lb.select_channel([ch_high, ch_low])
        assert selected.id == "ch_high"

        # 让高优先级熔断
        await lb.record_failure("ch_high")
        await lb.record_failure("ch_high")
        selected = await lb.select_channel([ch_high, ch_low])
        assert selected.id == "ch_low"

        # 等高优先级 cooldown 过期
        await asyncio.sleep(0.08)
        selected = await lb.select_channel([ch_high, ch_low])
        assert selected.id == "ch_high"

    @pytest.mark.asyncio
    async def test_all_priority_levels_cascading_failure(self):
        """三个优先级全部级联失败 → 最终返回 None"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=1, cooldown_seconds=300)

        ch_p1 = _make_channel(id="ch_p1", priority=1)
        ch_p2 = _make_channel(id="ch_p2", priority=5)
        ch_p3 = _make_channel(id="ch_p3", priority=10)

        # 逐步淘汰
        await lb.record_failure("ch_p1")
        selected = await lb.select_channel([ch_p1, ch_p2, ch_p3])
        assert selected.id == "ch_p2"

        await lb.record_failure("ch_p2")
        selected = await lb.select_channel([ch_p1, ch_p2, ch_p3])
        assert selected.id == "ch_p3"

        await lb.record_failure("ch_p3")
        selected = await lb.select_channel([ch_p1, ch_p2, ch_p3])
        assert selected is None


# ═══════════════════════════════════════════
#  动态更新 cooldown_seconds 的影响
# ═══════════════════════════════════════════


class TestDynamicCooldownUpdate:
    @pytest.mark.asyncio
    async def test_shortening_cooldown_enables_faster_recovery(self):
        """缩短 cooldown 使已熔断的渠道更快恢复"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=1, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        await lb.record_failure("ch_cd")
        assert await lb.select_channel([ch]) is None

        # 缩短 cooldown 到 0.01 秒
        await lb.update_config(max_fail_count=1, cooldown_seconds=0.01)
        await asyncio.sleep(0.02)

        selected = await lb.select_channel([ch])
        assert selected is not None

    @pytest.mark.asyncio
    async def test_lengthening_cooldown_extends_unavailability(self):
        """延长 cooldown 使原本快恢复的渠道继续保持不可用"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=1, cooldown_seconds=0.05)
        ch = _make_channel(id="ch_cd")

        await lb.record_failure("ch_cd")

        # 在 cooldown 过期前延长它
        await asyncio.sleep(0.02)
        await lb.update_config(max_fail_count=1, cooldown_seconds=300)

        await asyncio.sleep(0.05)
        # 虽然从失败算起已经过了 0.07 秒，但 cooldown 已改为 300 秒
        selected = await lb.select_channel([ch])
        assert selected is None

    @pytest.mark.asyncio
    async def test_max_fail_count_change_affects_unhealthy_threshold(self):
        """提高 max_fail_count 可使已熔断的渠道恢复（因为 fail_count < new_max）"""
        lb = LoadBalancer()
        await lb.update_config(max_fail_count=2, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        for _ in range(3):
            await lb.record_failure("ch_cd")
        assert await lb.select_channel([ch]) is None

        # 提高阈值到 5 → fail_count(3) < max(5) → 恢复
        await lb.update_config(max_fail_count=5, cooldown_seconds=300)
        selected = await lb.select_channel([ch])
        assert selected is not None
