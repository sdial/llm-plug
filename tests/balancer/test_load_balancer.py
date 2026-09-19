import asyncio
import inspect

import pytest

from balancer.load_balancer import LoadBalancer
from models.api_types import APIType
from models.channel import Channel, Endpoint


@pytest.fixture(autouse=True)
def _reset_outcomes():
    """隔离全局 outcomes 单例：每个用例前后清空，避免跨用例污染。"""
    from proxy import outcomes

    outcomes.reset()
    yield
    outcomes.reset()


def create_channel(id: str, weight: int) -> Channel:
    return Channel(
        id=id,
        name=f"Channel {id}",
        endpoints=[Endpoint(api_type=APIType.ANTHROPIC, base_url="http://test")],
        api_key="test",
        models=["test"],
        enabled=True,
        weight=weight,
        priority=1,
        socks5_proxy=None,
        created_at="2026-04-28T00:00:00Z",
    )


def test_weighted_round_robin_fairness():
    """测试加权轮询的公平性 - 权重大的应该被选中更多次"""
    ch1 = create_channel("ch1", weight=3)
    ch2 = create_channel("ch2", weight=1)
    channels = [ch1, ch2]

    balancer = LoadBalancer()

    # 选择100次，ch1应该被选中更多（权重3:1比例）
    selections = {"ch1": 0, "ch2": 0}
    for _ in range(100):
        selected = balancer._weighted_round_robin(channels, model="test")
        selections[selected.id] += 1

    # ch1 (权重3) 应该比 ch2 (权重1) 被选中更多
    # 理论比例是 3:1 = 75%:25%
    ch1_ratio = selections["ch1"] / 100
    assert ch1_ratio > 0.5, f"Expected ch1 selected more than ch2, got ch1={selections['ch1']} ({ch1_ratio:.1%}), ch2={selections['ch2']}"


def test_weighted_round_robin_balanced():
    """测试等权重时轮询均衡"""
    ch1 = create_channel("ch1", weight=1)
    ch2 = create_channel("ch2", weight=1)
    channels = [ch1, ch2]

    balancer = LoadBalancer()

    # 选择100次，两个channel应该各被选中约50次
    selections = {"ch1": 0, "ch2": 0}
    for _ in range(100):
        selected = balancer._weighted_round_robin(channels, model="test")
        selections[selected.id] += 1

    # 等权重时，分布应该接近 50:50
    ch1_ratio = selections["ch1"] / 100
    assert 0.4 < ch1_ratio < 0.6, f"Expected balanced selection, got ch1={selections['ch1']} ({ch1_ratio:.1%}), ch2={selections['ch2']}"


def test_weighted_round_robin_weight_distribution():
    """测试权重分布是否符合预期比例"""
    ch1 = create_channel("ch1", weight=3)
    ch2 = create_channel("ch2", weight=2)
    channels = [ch1, ch2]

    balancer = LoadBalancer()

    # 选择100次，统计分布
    selections = {"ch1": 0, "ch2": 0}
    for _ in range(100):
        selected = balancer._weighted_round_robin(channels, model="test")
        selections[selected.id] += 1

    # 权重3:2，ch1应该约占60%左右
    ch1_ratio = selections["ch1"] / 100
    assert 0.5 < ch1_ratio < 0.7, f"Expected ch1 ratio around 0.6, got {ch1_ratio:.2f} (ch1={selections['ch1']}, ch2={selections['ch2']})"


def test_weighted_round_robin_weights_isolated_by_model():
    """(model, channel) 键：A 模型的 SWRR 轮询进度不污染 B 模型。

    三个渠道权重 1:1:1，A 模型连选一轮后 B 模型应从零进度开始（仍选渠道一）。
    """
    balancer = LoadBalancer()
    ch_a1 = create_channel("ch1", weight=1)
    ch_b1 = create_channel("ch2", weight=1)
    ch_c1 = create_channel("ch3", weight=1)
    channels = [ch_a1, ch_b1, ch_c1]

    # A 模型先走一轮：三个渠道各被选中一次
    first = {balancer._weighted_round_robin(channels, model="model-a").id for _ in range(3)}
    assert first == {"ch1", "ch2", "ch3"}

    # B 模型选第一个时，不应受 A 模型残留权重影响 → 仍是 ch1
    selected = balancer._weighted_round_robin(channels, model="model-b")
    assert selected.id == "ch1"


@pytest.mark.anyio
async def test_health_mutations_account_via_outcomes():
    """健康记账已直连 outcomes（ADR-0025 D1，LB 兼容外壳退场）：record 是同步缝，
    记账后健康 / 降级视图立即可见；LB 自身仅保留异步的选择与配置接缝。"""
    from proxy import outcomes
    from proxy.outcomes import OutcomeKind

    balancer = LoadBalancer()

    assert not inspect.iscoroutinefunction(outcomes.record)
    assert inspect.iscoroutinefunction(balancer.remove_channel)
    assert inspect.iscoroutinefunction(balancer.update_config)

    outcomes.record("test", "ch1", OutcomeKind.transport_failure)
    assert outcomes.is_degraded("test", "ch1") is True
    outcomes.record("test", "ch1", OutcomeKind.success)
    assert outcomes.is_degraded("test", "ch1") is False
    await balancer.remove_channel("ch1")
    assert outcomes.is_healthy("test", "ch1") is True


@pytest.mark.anyio
async def test_update_config_waits_for_balancer_lock():
    balancer = LoadBalancer()

    await balancer._lock.acquire()
    try:
        update_task = asyncio.create_task(balancer.update_config(strategy="sticky", sticky_ttl=900))
        await asyncio.sleep(0)

        assert not update_task.done()
        assert balancer._strategy == "round_robin"
        assert balancer._sticky_ttl == 1800.0
    finally:
        balancer._lock.release()

    await update_task
    assert balancer._strategy == "sticky"
    assert balancer._sticky_ttl == 900.0
