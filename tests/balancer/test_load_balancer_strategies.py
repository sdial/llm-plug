import asyncio

import pytest

from balancer.load_balancer import LoadBalancer
from models.channel import Channel


def make_channel(
    id: str,
    *,
    weight: int = 1,
    priority: int = 1,
    enabled: bool = True,
) -> Channel:
    return Channel(
        id=id,
        name=f"Channel {id}",
        api_type="openai-chat-completions",
        base_url="http://example.com",
        api_key="key",
        models=["gpt-4"],
        enabled=enabled,
        weight=weight,
        priority=priority,
    )


@pytest.mark.asyncio
async def test_update_config_sets_strategy_and_sticky_limits():
    lb = LoadBalancer()

    await lb.update_config(
        max_fail_count=3,
        cooldown_seconds=12,
        strategy="sticky",
        sticky_ttl=600,
        sticky_cache_max_entries=321,
    )

    assert lb._max_fail_count == 3
    assert lb._cooldown_seconds == 12.0
    assert lb._strategy == "sticky"
    assert lb._sticky_ttl == 600.0
    assert lb._sticky_cache_max_entries == 321


@pytest.mark.asyncio
async def test_update_config_rejects_unknown_strategy():
    lb = LoadBalancer()

    with pytest.raises(ValueError, match="lb_strategy"):
        await lb.update_config(strategy="random")


@pytest.mark.asyncio
async def test_backup_selects_highest_priority_then_weight_then_id():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_low = make_channel("low", priority=5, weight=100)
    ch_b = make_channel("b", priority=1, weight=5)
    ch_a = make_channel("a", priority=1, weight=5)
    ch_heavy = make_channel("heavy", priority=1, weight=10)

    selected = await lb.select_channel([ch_low, ch_b, ch_a, ch_heavy])

    assert selected.id == "heavy"


@pytest.mark.asyncio
async def test_backup_uses_id_as_stable_tiebreaker():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_b = make_channel("b", priority=1, weight=5)
    ch_a = make_channel("a", priority=1, weight=5)

    selected = await lb.select_channel([ch_b, ch_a])

    assert selected.id == "a"


@pytest.mark.asyncio
async def test_backup_falls_to_same_priority_next_before_lower_priority():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_a = make_channel("a", priority=1, weight=10)
    ch_b = make_channel("b", priority=1, weight=5)
    ch_low = make_channel("low", priority=10, weight=100)

    selected = await lb.select_channel([ch_a, ch_b, ch_low], exclude_ids={"a"})

    assert selected.id == "b"
