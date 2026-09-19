"""ADR-0021 D0 — ``outcomes.admits()`` 准入单一住所直测。

只测外部行为：准入四维度（enabled / exclude_ids / blocked / healthy）
逐门直测 + ``model=None`` 跳健康门的接口承诺 + 与负载均衡选择器
（``_get_top_priority_group``）过滤结果的逐条件等价。
不测内部事件环形态；quota / 健康视图的完整语义见 test_outcomes.py。
"""

import time

import pytest

from balancer.load_balancer import LoadBalancer
from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy import outcomes


@pytest.fixture(autouse=True)
def _reset_outcomes():
    """每个用例前后清空模块状态，并钉住默认阈值，避免被 config 全局状态污染。"""
    outcomes.reset()
    outcomes.set_quota_adapter(None)
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    yield
    outcomes.reset()
    outcomes.set_quota_adapter(None)
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)


def create_channel(id: str, *, enabled: bool = True, priority: int = 1) -> Channel:
    return Channel(
        id=id,
        name=f"Channel {id}",
        endpoints=[Endpoint(api_type=APIType.ANTHROPIC, base_url="http://test")],
        api_key="test",
        models=["m"],
        enabled=enabled,
        weight=1,
        priority=priority,
        socks5_proxy=None,
        created_at="2026-08-30T00:00:00Z",
    )


# ═══════════════════════════════════════════
#  四维度直测：enabled / exclude_ids / blocked / healthy
# ═══════════════════════════════════════════


class TestAdmitsDimensions:
    def test_enabled_gate(self):
        ch = create_channel("ch", enabled=False)
        assert outcomes.admits(ch, "m") is False
        ch = create_channel("ch", enabled=True)
        assert outcomes.admits(ch, "m") is True

    def test_exclude_ids_gate(self):
        ch = create_channel("ch")
        assert outcomes.admits(ch, "m", exclude_ids={"ch"}) is False
        assert outcomes.admits(ch, "m", exclude_ids={"other"}) is True
        # 缺省 exclude_ids 不排除任何渠道
        assert outcomes.admits(ch, "m") is True

    def test_blocked_gate(self):
        ch = create_channel("ch")
        outcomes.record("m", "ch", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
        assert outcomes.admits(ch, "m") is False
        # blocked 优先于一切：即使渠道健康也不准入
        assert outcomes.is_healthy("m", "ch") is True
        # 窗口过期后恢复准入
        outcomes.record("m", "ch", outcomes.OutcomeKind.quota_window, reset_at=time.time() - 1)
        assert outcomes.admits(ch, "m") is True

    def test_healthy_gate(self):
        ch = create_channel("ch")
        for _ in range(3):
            outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        assert outcomes.admits(ch, "m") is False
        # 其他模型不受影响（健康视图按 (model, channel) 隔离）
        assert outcomes.admits(ch, "other") is True
        outcomes.record("m", "ch", outcomes.OutcomeKind.success)
        assert outcomes.admits(ch, "m") is True


class TestModelNoneSkipsHealthGate:
    def test_model_none_skips_health_gate(self):
        """model=None 跳健康门——探活复用语义升为接口承诺（ADR-0021 D0）。"""
        ch = create_channel("ch")
        for _ in range(3):
            outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("m", "ch") is False
        assert outcomes.admits(ch, None) is True

    def test_model_none_still_enforces_other_gates(self):
        """跳的只是健康门：enabled / exclude / blocked 门仍然生效。"""
        assert outcomes.admits(create_channel("ch", enabled=False), None) is False
        assert outcomes.admits(create_channel("ch"), None, exclude_ids={"ch"}) is False
        outcomes.record("m", "ch", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
        assert outcomes.admits(create_channel("ch"), None) is False


# ═══════════════════════════════════════════
#  与负载均衡选择器过滤的等价性
# ═══════════════════════════════════════════


class TestSelectorEquivalence:
    @pytest.mark.parametrize(
        "scenario",
        ["baseline", "disabled", "excluded", "blocked", "unhealthy", "model_none"],
    )
    def test_top_priority_group_filter_equals_admits(self, scenario):
        """``_get_top_priority_group`` 的过滤结果与逐渠道 ``admits()`` 一致。"""
        healthy_ch = create_channel("ch_ok", priority=1)
        other_ch = create_channel("ch_other", priority=1)
        disabled_ch = create_channel("ch_disabled", enabled=False, priority=1)
        channels = [healthy_ch, other_ch, disabled_ch]

        exclude_ids: set[str] = set()
        model = "m"
        if scenario == "disabled":
            healthy_ch.enabled = False
        elif scenario == "excluded":
            exclude_ids = {"ch_ok"}
        elif scenario == "blocked":
            outcomes.record("m", "ch_ok", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
        elif scenario == "unhealthy":
            for _ in range(3):
                outcomes.record("m", "ch_ok", outcomes.OutcomeKind.http_5xx)
        elif scenario == "model_none":
            for _ in range(3):
                outcomes.record("m", "ch_ok", outcomes.OutcomeKind.http_5xx)
            model = None

        balancer = LoadBalancer()
        group = balancer._get_top_priority_group(channels, exclude_ids, model)
        expected = [ch for ch in channels if outcomes.admits(ch, model, exclude_ids=exclude_ids)]
        assert [ch.id for ch in group] == [ch.id for ch in expected]
