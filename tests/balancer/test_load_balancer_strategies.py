import asyncio
import hashlib

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


def test_build_session_fingerprint_prefers_x_session_id_and_hashes_value():
    lb = LoadBalancer()

    fingerprint = lb._build_session_fingerprint(
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={
            "X-Session-ID": "session-secret",
            "x-claude-code-session-id": "claude-session",
            "Authorization": "Bearer raw-secret",
            "x-api-key": "raw-api-key",
            "User-Agent": "agent",
        },
    )

    assert fingerprint == hashlib.sha256(b'{"session":"session-secret"}').hexdigest()
    assert "session-secret" not in fingerprint
    assert "raw-secret" not in fingerprint
    assert "raw-api-key" not in fingerprint


def test_build_session_fingerprint_uses_structured_non_sensitive_fallback():
    lb = LoadBalancer()

    fingerprint = lb._build_session_fingerprint(
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={
            "Authorization": "Bearer raw-secret",
            "x-api-key": "raw-api-key",
            "User-Agent": "agent|None",
        },
    )

    expected = hashlib.sha256(
        b'{"api_key_id":"key-name","client_ip":"10.0.0.5","user_agent":"agent|None"}'
    ).hexdigest()
    assert fingerprint == expected
    assert "raw-secret" not in fingerprint
    assert "raw-api-key" not in fingerprint


@pytest.mark.asyncio
async def test_sticky_cache_stores_only_fingerprint_not_raw_secrets():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]

    await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={
            "authorization": "Bearer raw-secret",
            "x-api-key": "raw-api-key",
            "x-session-id": "session-secret",
        },
    )

    assert len(lb._sticky_cache) == 1
    cached_key = next(iter(lb._sticky_cache.keys()))
    assert "raw-secret" not in cached_key
    assert "raw-api-key" not in cached_key
    assert "session-secret" not in cached_key
    assert len(cached_key) == 64


@pytest.mark.asyncio
async def test_sticky_cache_entry_is_ignored_when_channel_excluded():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]
    first = await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    second = await lb.select_channel(
        channels,
        exclude_ids={first.id},
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    assert second.id != first.id


@pytest.mark.asyncio
async def test_sticky_cache_lru_eviction_respects_max_entries():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky", sticky_cache_max_entries=2)
    channels = [make_channel("a"), make_channel("b")]

    # Create 2 cache entries
    await lb.select_channel(
        channels,
        client_ip="10.0.0.0",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-0"},
    )
    await lb.select_channel(
        channels,
        client_ip="10.0.0.1",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    # Re-access session-0 to make it most recently used
    await lb.select_channel(
        channels,
        client_ip="10.0.0.0",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-0"},
    )
    # Add session-2; should evict session-1 (least recently used), not session-0
    await lb.select_channel(
        channels,
        client_ip="10.0.0.2",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-2"},
    )

    key0 = lb._build_session_fingerprint(
        client_ip="10.0.0.0",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-0"},
    )
    key1 = lb._build_session_fingerprint(
        client_ip="10.0.0.1",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    assert len(lb._sticky_cache) == 2
    assert key1 not in lb._sticky_cache
    assert key0 in lb._sticky_cache


@pytest.mark.asyncio
async def test_update_config_clears_sticky_cache_when_strategy_or_ttl_changes():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    await lb.select_channel(
        [make_channel("a"), make_channel("b")],
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    assert lb._sticky_cache

    await lb.update_config(strategy="round_robin")
    assert not lb._sticky_cache

    await lb.update_config(strategy="sticky")
    await lb.select_channel(
        [make_channel("a"), make_channel("b")],
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    assert lb._sticky_cache

    await lb.update_config(strategy="sticky", sticky_ttl=900)
    assert not lb._sticky_cache

    await lb.update_config(strategy="sticky", sticky_cache_max_entries=2000)
    assert not lb._sticky_cache


@pytest.mark.asyncio
async def test_sticky_never_crosses_priority_when_high_priority_available():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    high_a = make_channel("high-a", priority=1)
    high_b = make_channel("high-b", priority=1)
    low = make_channel("low", priority=10, weight=1000)

    for i in range(50):
        selected = await lb.select_channel(
            [low, high_a, high_b],
            client_ip=f"10.0.0.{i}",
            api_key_id="key-name",
            client_headers={"user-agent": f"agent-{i}"},
        )
        assert selected.id in {"high-a", "high-b"}


@pytest.mark.asyncio
async def test_sticky_exclude_id_reselects_within_same_priority():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]
    first = await lb.select_channel(
        channels,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    second = await lb.select_channel(
        channels,
        exclude_ids={first.id},
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    assert second.id != first.id
    assert second.id in {"a", "b"}
