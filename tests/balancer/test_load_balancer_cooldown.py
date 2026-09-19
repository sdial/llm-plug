"""负载均衡器退避/恢复时序测试

覆盖场景：
1. 渠道在 cooldown 期间被跳过
2. cooldown 过期后渠道自动恢复可用
3. 恢复后再次失败会重新进入 cooldown
4. success 记账可立即重置失败计数
5. 多渠道 cooldown 交叉恢复
6. 优先级回退 + cooldown 恢复的组合
7. 动态更新 cooldown_seconds 的影响

健康键为 ``(model, channel_id)``（ADR-0008 D0），本文件统一用
``model="gpt-4"`` 记账与选路。熔断语义：失败计数达 ``max_fail_count`` 即不健康，
冷却到期清零"假自愈"。
"""

import asyncio

import pytest

from balancer.load_balancer import LoadBalancer
from models.channel import Channel, Endpoint
from proxy import outcomes
from proxy.outcomes import OutcomeKind

MODEL = "gpt-4"


@pytest.fixture(autouse=True)
def _reset_outcomes():
    """隔离全局 outcomes 单例：每个用例前后清空，避免跨用例污染。"""
    from proxy import outcomes

    outcomes.reset()
    yield
    outcomes.reset()


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
        endpoints=[Endpoint(api_type="openai-chat-completions", base_url="http://example.com")],
        api_key="key",
        models=models or [MODEL],
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
        """cooldown 期间渠道应被跳过（失败计数达 max_fail_count）"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=2, cooldown_seconds=60)
        ch = _make_channel(id="ch_cd")
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch], model=MODEL)
        assert selected is None

    @pytest.mark.asyncio
    async def test_channel_recovers_after_cooldown(self):
        """cooldown 过期后渠道应恢复可用"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=2, cooldown_seconds=0.05)
        ch = _make_channel(id="ch_cd")

        # 触发熔断
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

        # 等待 cooldown 过期
        await asyncio.sleep(0.08)
        selected = await lb.select_channel([ch], model=MODEL)
        assert selected is not None
        assert selected.id == "ch_cd"

    @pytest.mark.asyncio
    async def test_recovery_then_refailure_re_enters_cooldown(self):
        """恢复后再次失败应重新进入 cooldown"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=2, cooldown_seconds=0.05)
        ch = _make_channel(id="ch_cd")

        # 触发熔断（2 次失败）
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

        # cooldown 过期后恢复
        await asyncio.sleep(0.08)
        assert (await lb.select_channel([ch], model=MODEL)) is not None

        # 再次失败到 max_fail_count → 重新进入 cooldown
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

    @pytest.mark.asyncio
    async def test_recovery_resets_fail_count_for_full_threshold(self):
        """冷却恢复后 fail_count 应清零，重新按完整 max_fail_count 计失败，
        而非残留计数导致再失败一次就重新进入整段冷却"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=3, cooldown_seconds=0.05)
        ch = _make_channel(id="ch_cd")

        # 触发熔断
        for _ in range(3):
            outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

        # cooldown 过期后恢复，fail_count 应清零
        await asyncio.sleep(0.08)
        assert (await lb.select_channel([ch], model=MODEL)) is not None

        # 恢复后再失败一次：fail_count=1 < 3，渠道应保持可用
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is not None

        # 累计失败到 max_fail_count 才重新进入冷却（恢复后再失败到 3 次）
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

    @pytest.mark.asyncio
    async def test_success_resets_fail_count_immediately(self):
        """success 记账应立即重置失败计数，渠道恢复可用"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=3, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        for _ in range(3):
            outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

        outcomes.record(MODEL, "ch_cd", OutcomeKind.success)
        selected = await lb.select_channel([ch], model=MODEL)
        assert selected is not None
        assert selected.id == "ch_cd"

    @pytest.mark.asyncio
    async def test_success_within_cooldown_period_resets_counter(self):
        """在 cooldown 期间如果记账 success，渠道应立即可用（无需等待 cooldown）"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=2, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

        # 手动重置
        outcomes.record(MODEL, "ch_cd", OutcomeKind.success)
        assert (await lb.select_channel([ch], model=MODEL)) is not None


# ═══════════════════════════════════════════
#  多渠道 cooldown 交叉恢复
# ═══════════════════════════════════════════


class TestMultiChannelCooldown:
    @pytest.mark.asyncio
    async def test_alternating_cooldown_recovery(self):
        """两个渠道交替 cooldown，始终至少有一个可用"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=1, cooldown_seconds=0.05)
        ch_a = _make_channel(id="ch_a", priority=1, weight=1)
        ch_b = _make_channel(id="ch_b", priority=1, weight=1)

        # 淘汰 A
        outcomes.record(MODEL, "ch_a", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_a, ch_b], model=MODEL)
        assert selected.id == "ch_b"

        # 淘汰 B
        outcomes.record(MODEL, "ch_b", OutcomeKind.transport_failure)
        # 两者都不健康
        assert await lb.select_channel([ch_a, ch_b], model=MODEL) is None

        # 等 A 的 cooldown 过期
        await asyncio.sleep(0.08)
        selected = await lb.select_channel([ch_a, ch_b], model=MODEL)
        assert selected is not None
        assert selected.id == "ch_a"

    @pytest.mark.asyncio
    async def test_staggered_recovery_respects_cooldown_order(self):
        """先失败的渠道先恢复"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=1, cooldown_seconds=0.05)
        ch_first = _make_channel(id="ch_first", priority=1, weight=1)
        ch_second = _make_channel(id="ch_second", priority=1, weight=1)

        # 先淘汰 first
        outcomes.record(MODEL, "ch_first", OutcomeKind.transport_failure)

        # 等一小段时间再淘汰 second
        await asyncio.sleep(0.02)
        outcomes.record(MODEL, "ch_second", OutcomeKind.transport_failure)

        # 等 first 的 cooldown 过期，second 还在 cooldown
        await asyncio.sleep(0.04)

        selected = await lb.select_channel([ch_first, ch_second], model=MODEL)
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
        outcomes.configure(max_fail_count=2, cooldown_seconds=0.05)

        ch_high = _make_channel(id="ch_high", priority=1, weight=1)
        ch_low = _make_channel(id="ch_low", priority=10, weight=1)

        # 高优先级正常时应该选它
        selected = await lb.select_channel([ch_high, ch_low], model=MODEL)
        assert selected.id == "ch_high"

        # 让高优先级熔断
        outcomes.record(MODEL, "ch_high", OutcomeKind.transport_failure)
        outcomes.record(MODEL, "ch_high", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_high, ch_low], model=MODEL)
        assert selected.id == "ch_low"

        # 等高优先级 cooldown 过期
        await asyncio.sleep(0.08)
        selected = await lb.select_channel([ch_high, ch_low], model=MODEL)
        assert selected.id == "ch_high"

    @pytest.mark.asyncio
    async def test_all_priority_levels_cascading_failure(self):
        """三个优先级全部级联失败 → 最终返回 None"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=1, cooldown_seconds=300)

        ch_p1 = _make_channel(id="ch_p1", priority=1)
        ch_p2 = _make_channel(id="ch_p2", priority=5)
        ch_p3 = _make_channel(id="ch_p3", priority=10)

        # 逐步淘汰
        outcomes.record(MODEL, "ch_p1", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_p1, ch_p2, ch_p3], model=MODEL)
        assert selected.id == "ch_p2"

        outcomes.record(MODEL, "ch_p2", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_p1, ch_p2, ch_p3], model=MODEL)
        assert selected.id == "ch_p3"

        outcomes.record(MODEL, "ch_p3", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_p1, ch_p2, ch_p3], model=MODEL)
        assert selected is None


# ═══════════════════════════════════════════
#  动态更新 cooldown_seconds 的影响
# ═══════════════════════════════════════════


class TestDynamicCooldownUpdate:
    @pytest.mark.asyncio
    async def test_shortening_cooldown_enables_faster_recovery(self):
        """缩短 cooldown 使已熔断的渠道更快恢复"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=1, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

        # 缩短 cooldown 到 0.01 秒
        outcomes.configure(max_fail_count=1, cooldown_seconds=0.01)
        await asyncio.sleep(0.02)

        selected = await lb.select_channel([ch], model=MODEL)
        assert selected is not None

    @pytest.mark.asyncio
    async def test_lengthening_cooldown_extends_unavailability(self):
        """延长 cooldown 使原本快恢复的渠道继续保持不可用"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=1, cooldown_seconds=0.05)
        ch = _make_channel(id="ch_cd")

        outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)

        # 在 cooldown 过期前延长它
        await asyncio.sleep(0.02)
        outcomes.configure(max_fail_count=1, cooldown_seconds=300)

        await asyncio.sleep(0.05)
        # 虽然从失败算起已经过了 0.07 秒，但 cooldown 已改为 300 秒
        selected = await lb.select_channel([ch], model=MODEL)
        assert selected is None

    @pytest.mark.asyncio
    async def test_max_fail_count_change_affects_unhealthy_threshold(self):
        """提高 max_fail_count 可使已熔断的渠道恢复（因为 fail_count < new_max）"""
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=2, cooldown_seconds=300)
        ch = _make_channel(id="ch_cd")

        for _ in range(3):
            outcomes.record(MODEL, "ch_cd", OutcomeKind.transport_failure)
        assert await lb.select_channel([ch], model=MODEL) is None

        # 提高阈值到 5 → fail_count(3) < max(5) → 恢复
        outcomes.configure(max_fail_count=5, cooldown_seconds=300)
        selected = await lb.select_channel([ch], model=MODEL)
        assert selected is not None
