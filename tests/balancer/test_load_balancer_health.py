"""P1-4: 负载均衡器健康检查逻辑、优先级分组、渠道选择完整测试。

健康状态已收编到 :mod:`proxy.outcomes`（ADR-0008 D0）——本文件测的是
LoadBalancer 与 outcomes 视图的集成行为：``select_channel`` 按
``(model, channel)`` 键过滤健康、``blocked`` 优先于一切、以及优先级分组。
"""

import asyncio
import time

import pytest

from balancer.load_balancer import LoadBalancer
from models.channel import Channel, Endpoint
from proxy import outcomes
from proxy.outcomes import OutcomeKind


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
        models=models or ["gpt-4"],
        enabled=enabled,
        weight=weight,
        priority=priority,
    )


# ═══════════════════════════════════════════
#  outcomes 健康视图集成（替代原 ChannelHealth 状态转换）
# ═══════════════════════════════════════════


class TestOutcomesHealthIntegration:
    def test_initial_state_is_healthy(self):
        from proxy import outcomes

        assert outcomes.is_healthy("gpt-4", "ch_test") is True

    def test_single_failure_still_healthy(self):
        from proxy import outcomes

        outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("gpt-4", "ch_test") is True
        assert outcomes.is_degraded("gpt-4", "ch_test") is True

    def test_reaching_max_fail_count_becomes_unhealthy(self):
        from proxy import outcomes

        outcomes.configure(max_fail_count=5, cooldown_seconds=60)
        for _ in range(5):
            outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("gpt-4", "ch_test") is False

    def test_exceeding_max_fail_count_stays_unhealthy(self):
        from proxy import outcomes

        outcomes.configure(max_fail_count=5, cooldown_seconds=60)
        for _ in range(10):
            outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("gpt-4", "ch_test") is False

    def test_cooldown_recovery(self):
        """冷却期过后恢复健康（假自愈），真实故障由 is_degraded 兜住"""
        from proxy import outcomes

        outcomes.configure(max_fail_count=5, cooldown_seconds=0.01)
        for _ in range(5):
            outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("gpt-4", "ch_test") is False
        time.sleep(0.02)
        assert outcomes.is_healthy("gpt-4", "ch_test") is True
        # 冷却假自愈不清除降级态：只有真实 success 才清除
        assert outcomes.is_degraded("gpt-4", "ch_test") is True

    def test_cooldown_recovery_resets_fail_count(self):
        """冷却期结束恢复时失败计数应清零，按完整 max_fail_count 重新计数"""
        from proxy import outcomes

        outcomes.configure(max_fail_count=5, cooldown_seconds=0.01)
        for _ in range(5):
            outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("gpt-4", "ch_test") is False
        time.sleep(0.02)
        assert outcomes.is_healthy("gpt-4", "ch_test") is True
        # 恢复后失败一次仍是健康状态，不会立刻重新进入整段冷却
        outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("gpt-4", "ch_test") is True

    def test_success_resets_fail_count(self):
        from proxy import outcomes

        outcomes.configure(max_fail_count=5, cooldown_seconds=60)
        for _ in range(5):
            outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.success)
        assert outcomes.is_healthy("gpt-4", "ch_test") is True
        assert outcomes.is_degraded("gpt-4", "ch_test") is False

    def test_success_after_failure_restores_health(self):
        from proxy import outcomes

        outcomes.configure(max_fail_count=5, cooldown_seconds=60)
        for _ in range(3):
            outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.success)
        assert outcomes.is_healthy("gpt-4", "ch_test") is True

    def test_max_fail_count_one(self):
        """max_fail_count=1 时，一次失败就不健康"""
        from proxy import outcomes

        outcomes.configure(max_fail_count=1, cooldown_seconds=60)
        outcomes.record("gpt-4", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("gpt-4", "ch_test") is False

    def test_failure_isolated_per_model_channel_pair(self):
        """(model, channel) 键：A 模型 3 次失败不污染 B 模型的可选性（A 挂不死 B）"""
        from proxy import outcomes

        outcomes.configure(max_fail_count=3, cooldown_seconds=120)
        for _ in range(3):
            outcomes.record("model-a", "ch_test", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("model-a", "ch_test") is False
        assert outcomes.is_healthy("model-b", "ch_test") is True


# ═══════════════════════════════════════════
#  LoadBalancer.select_channel — 优先级分组
# ═══════════════════════════════════════════


class TestSelectChannelPriority:
    @pytest.mark.asyncio
    async def test_selects_highest_priority(self):
        lb = LoadBalancer()
        ch_high = _make_channel(id="ch1", priority=1, weight=1)
        ch_low = _make_channel(id="ch2", priority=10, weight=1)
        selected = await lb.select_channel([ch_high, ch_low], model="gpt-4")
        assert selected.id == "ch1"

    @pytest.mark.asyncio
    async def test_same_priority_uses_weighted_round_robin(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a", priority=1, weight=3)
        ch_b = _make_channel(id="ch_b", priority=1, weight=1)
        counts = {"ch_a": 0, "ch_b": 0}
        for _ in range(100):
            selected = await lb.select_channel([ch_a, ch_b], model="gpt-4")
            counts[selected.id] += 1
        # weight 3:1 → 约 75%:25%
        assert counts["ch_a"] > counts["ch_b"]
        assert 60 <= counts["ch_a"] <= 90

    @pytest.mark.asyncio
    async def test_disabled_channel_excluded(self):
        lb = LoadBalancer()
        ch_disabled = _make_channel(id="ch_off", enabled=False, priority=1)
        ch_enabled = _make_channel(id="ch_on", enabled=True, priority=1)
        selected = await lb.select_channel([ch_disabled, ch_enabled], model="gpt-4")
        assert selected.id == "ch_on"

    @pytest.mark.asyncio
    async def test_exclude_ids_respected(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a", priority=1)
        ch_b = _make_channel(id="ch_b", priority=1)
        selected = await lb.select_channel([ch_a, ch_b], exclude_ids={"ch_a"}, model="gpt-4")
        assert selected.id == "ch_b"

    @pytest.mark.asyncio
    async def test_all_excluded_returns_none(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a")
        ch_b = _make_channel(id="ch_b")
        selected = await lb.select_channel([ch_a, ch_b], exclude_ids={"ch_a", "ch_b"}, model="gpt-4")
        assert selected is None

    @pytest.mark.asyncio
    async def test_all_disabled_returns_none(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a", enabled=False)
        ch_b = _make_channel(id="ch_b", enabled=False)
        selected = await lb.select_channel([ch_a, ch_b], model="gpt-4")
        assert selected is None

    @pytest.mark.asyncio
    async def test_all_unhealthy_returns_none(self):
        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a")
        ch_b = _make_channel(id="ch_b")
        # 让两个渠道都不健康（同一 (model, channel) 键下）
        for _ in range(10):
            outcomes.record("gpt-4", "ch_a", OutcomeKind.transport_failure)
            outcomes.record("gpt-4", "ch_b", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_a, ch_b], model="gpt-4")
        assert selected is None

    @pytest.mark.asyncio
    async def test_unhealthy_channel_skipped_healthy_selected(self):
        lb = LoadBalancer()
        ch_bad = _make_channel(id="ch_bad", priority=1, weight=1)
        ch_good = _make_channel(id="ch_good", priority=1, weight=1)
        for _ in range(10):
            outcomes.record("gpt-4", "ch_bad", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_bad, ch_good], model="gpt-4")
        assert selected.id == "ch_good"

    @pytest.mark.asyncio
    async def test_single_channel_returns_it(self):
        lb = LoadBalancer()
        ch = _make_channel(id="ch_solo", priority=1, weight=1)
        selected = await lb.select_channel([ch], model="gpt-4")
        assert selected.id == "ch_solo"

    @pytest.mark.asyncio
    async def test_empty_list_returns_none(self):
        lb = LoadBalancer()
        selected = await lb.select_channel([], model="gpt-4")
        assert selected is None

    @pytest.mark.asyncio
    async def test_fallback_to_lower_priority_when_high_unhealthy(self):
        """高优先级不健康时，应选择低优先级"""
        lb = LoadBalancer()
        ch_high = _make_channel(id="ch_high", priority=1, weight=1)
        ch_low = _make_channel(id="ch_low", priority=5, weight=1)
        for _ in range(10):
            outcomes.record("gpt-4", "ch_high", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch_high, ch_low], model="gpt-4")
        assert selected.id == "ch_low"

    @pytest.mark.asyncio
    async def test_blocked_channel_excluded_before_health(self):
        """blocked 优先于一切：quota 窗口渠道即使健康也被先排除"""
        from proxy import outcomes

        lb = LoadBalancer()
        ch_blocked = _make_channel(id="ch_blocked", priority=1, weight=1)
        ch_ok = _make_channel(id="ch_ok", priority=1, weight=1)
        # 高优先级健康渠道进入 quota 窗口 → 被排除，选低权重健康渠道
        outcomes.record(
            "gpt-4",
            "ch_blocked",
            outcomes.OutcomeKind.quota_window,
            reset_at=time.time() + 3600,
        )
        selected = await lb.select_channel([ch_blocked, ch_ok], model="gpt-4")
        assert selected.id == "ch_ok"

    @pytest.mark.asyncio
    async def test_all_blocked_returns_none(self):
        from proxy import outcomes

        lb = LoadBalancer()
        ch_a = _make_channel(id="ch_a")
        ch_b = _make_channel(id="ch_b")
        for ch in (ch_a, ch_b):
            outcomes.record("gpt-4", ch.id, outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
        selected = await lb.select_channel([ch_a, ch_b], model="gpt-4")
        assert selected is None


# ═══════════════════════════════════════════
#  LoadBalancer — 健康记录
# ═══════════════════════════════════════════


class TestLoadBalancerHealth:
    @pytest.mark.asyncio
    async def test_success_resets_health(self):
        lb = LoadBalancer()
        for _ in range(10):
            outcomes.record("gpt-4", "ch_test", OutcomeKind.transport_failure)
        outcomes.record("gpt-4", "ch_test", OutcomeKind.success)
        ch = _make_channel(id="ch_test")
        selected = await lb.select_channel([ch], model="gpt-4")
        assert selected is not None

    @pytest.mark.asyncio
    async def test_update_config_changes_thresholds(self):
        lb = LoadBalancer()
        ch = _make_channel(id="ch_test")
        # 默认 max_fail=3, 2 次失败仍健康（阈值经 outcomes 直调）
        outcomes.configure(max_fail_count=3, cooldown_seconds=60)
        for _ in range(2):
            outcomes.record("gpt-4", "ch_test", OutcomeKind.transport_failure)
        selected = await lb.select_channel([ch], model="gpt-4")
        assert selected is not None

        # 更新为 max_fail=2, 2 次失败就不健康（阈值直达 outcomes）
        outcomes.configure(max_fail_count=2, cooldown_seconds=60)
        selected = await lb.select_channel([ch], model="gpt-4")
        assert selected is None

    @pytest.mark.asyncio
    async def test_cooldown_recovery(self):
        lb = LoadBalancer()
        outcomes.configure(max_fail_count=1, cooldown_seconds=0.01)
        ch = _make_channel(id="ch_test")
        outcomes.record("gpt-4", "ch_test", OutcomeKind.transport_failure)
        # 等待冷却
        await asyncio.sleep(0.02)
        selected = await lb.select_channel([ch], model="gpt-4")
        assert selected is not None


# ═══════════════════════════════════════════
#  LoadBalancer.remove_channel
# ═══════════════════════════════════════════


class TestRemoveChannel:
    @pytest.mark.asyncio
    async def test_removes_stale_health_entries(self):
        from proxy import outcomes

        lb = LoadBalancer()
        outcomes.record("gpt-4", "ch_old", OutcomeKind.transport_failure)
        outcomes.record("gpt-4", "ch_keep", OutcomeKind.transport_failure)
        await lb.remove_channel("ch_old")
        assert outcomes.is_degraded("gpt-4", "ch_old") is False
        assert outcomes.is_degraded("gpt-4", "ch_keep") is True
