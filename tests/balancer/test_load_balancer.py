import pytest
from balancer.load_balancer import LoadBalancer, ChannelHealth
from models.channel import Channel
from models.api_types import APIType


def create_channel(id: str, weight: int) -> Channel:
    return Channel(
        id=id,
        name=f"Channel {id}",
        api_type=APIType.ANTHROPIC,
        base_url="http://test",
        api_key="test",
        models=["test"],
        enabled=True,
        weight=weight,
        priority=1,
        socks5_proxy=None,
        created_at="2026-04-28T00:00:00Z"
    )


def test_weighted_round_robin_fairness():
    """测试加权轮询的公平性 - 权重大的应该被选中更多次"""
    ch1 = create_channel("ch1", weight=3)
    ch2 = create_channel("ch2", weight=1)
    channels = [ch1, ch2]
    
    balancer = LoadBalancer()
    balancer._health["ch1"] = ChannelHealth()
    balancer._health["ch2"] = ChannelHealth()
    
    # 选择100次，ch1应该被选中更多（权重3:1比例）
    selections = {"ch1": 0, "ch2": 0}
    for _ in range(100):
        selected = balancer._weighted_round_robin(channels)
        selections[selected.id] += 1
    
    # ch1 (权重3) 应该比 ch2 (权重1) 被选中更多
    # 理论比例是 3:1 = 75%:25%
    ch1_ratio = selections["ch1"] / 100
    assert ch1_ratio > 0.5, \
        f"Expected ch1 selected more than ch2, got ch1={selections['ch1']} ({ch1_ratio:.1%}), ch2={selections['ch2']}"


def test_weighted_round_robin_balanced():
    """测试等权重时轮询均衡"""
    ch1 = create_channel("ch1", weight=1)
    ch2 = create_channel("ch2", weight=1)
    channels = [ch1, ch2]
    
    balancer = LoadBalancer()
    balancer._health["ch1"] = ChannelHealth()
    balancer._health["ch2"] = ChannelHealth()
    
    # 选择100次，两个channel应该各被选中约50次
    selections = {"ch1": 0, "ch2": 0}
    for _ in range(100):
        selected = balancer._weighted_round_robin(channels)
        selections[selected.id] += 1
    
    # 等权重时，分布应该接近 50:50
    ch1_ratio = selections["ch1"] / 100
    assert 0.4 < ch1_ratio < 0.6, \
        f"Expected balanced selection, got ch1={selections['ch1']} ({ch1_ratio:.1%}), ch2={selections['ch2']}"


def test_weighted_round_robin_weight_distribution():
    """测试权重分布是否符合预期比例"""
    ch1 = create_channel("ch1", weight=3)
    ch2 = create_channel("ch2", weight=2)
    channels = [ch1, ch2]
    
    balancer = LoadBalancer()
    balancer._health["ch1"] = ChannelHealth()
    balancer._health["ch2"] = ChannelHealth()
    
    # 选择100次，统计分布
    selections = {"ch1": 0, "ch2": 0}
    for _ in range(100):
        selected = balancer._weighted_round_robin(channels)
        selections[selected.id] += 1
    
    # 权重3:2，ch1应该约占60%左右
    ch1_ratio = selections["ch1"] / 100
    assert 0.5 < ch1_ratio < 0.7, \
        f"Expected ch1 ratio around 0.6, got {ch1_ratio:.2f} (ch1={selections['ch1']}, ch2={selections['ch2']})"
