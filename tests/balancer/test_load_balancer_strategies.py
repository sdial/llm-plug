import hashlib

import pytest

from balancer.load_balancer import LoadBalancer
from models.channel import Channel, Endpoint

MODEL = "gpt-4"


@pytest.fixture(autouse=True)
def _reset_outcomes():
    """隔离全局 outcomes 单例（含会话粘滞缓存）：每个用例前后清空。"""
    from proxy import outcomes

    outcomes.reset()
    yield
    outcomes.reset()


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
        endpoints=[Endpoint(api_type="openai-chat-completions", base_url="http://example.com")],
        api_key="key",
        models=[MODEL],
        enabled=enabled,
        weight=weight,
        priority=priority,
    )


@pytest.mark.asyncio
async def test_update_config_sets_strategy_and_sticky_limits():
    from proxy import outcomes

    lb = LoadBalancer()

    await lb.update_config(
        strategy="sticky",
        sticky_ttl=600,
        sticky_cache_max_entries=321,
    )

    assert lb._strategy == "sticky"
    assert lb._sticky_ttl == 600.0
    assert not hasattr(lb, "_sticky_cache_max_entries")
    assert not hasattr(lb, "_max_fail_count")
    assert not hasattr(lb, "_cooldown_seconds")
    # 粘滞配置已透传 outcomes
    assert outcomes._session_sticky_ttl == 600.0
    assert outcomes._session_sticky_max_entries == 321


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

    selected = await lb.select_channel([ch_low, ch_b, ch_a, ch_heavy], model=MODEL)

    assert selected.id == "heavy"


@pytest.mark.asyncio
async def test_backup_uses_id_as_stable_tiebreaker():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_b = make_channel("b", priority=1, weight=5)
    ch_a = make_channel("a", priority=1, weight=5)

    selected = await lb.select_channel([ch_b, ch_a], model=MODEL)

    assert selected.id == "a"


@pytest.mark.asyncio
async def test_backup_falls_to_same_priority_next_before_lower_priority():
    lb = LoadBalancer()
    await lb.update_config(strategy="backup")
    ch_a = make_channel("a", priority=1, weight=10)
    ch_b = make_channel("b", priority=1, weight=5)
    ch_low = make_channel("low", priority=10, weight=100)

    selected = await lb.select_channel([ch_a, ch_b, ch_low], exclude_ids={"a"}, model=MODEL)

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

    expected = hashlib.sha256(b'{"api_key_id":"key-name","client_ip":"10.0.0.5","user_agent":"agent|None"}').hexdigest()
    assert fingerprint == expected
    assert "raw-secret" not in fingerprint
    assert "raw-api-key" not in fingerprint


@pytest.mark.asyncio
async def test_sticky_stores_only_fingerprint_not_raw_secrets():
    """会话粘滞缓存键是会话指纹（哈希），不含任何原始敏感信息。"""
    from proxy import outcomes

    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]

    await lb.select_channel(
        channels,
        model=MODEL,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={
            "authorization": "Bearer raw-secret",
            "x-api-key": "raw-api-key",
            "x-session-id": "session-secret",
        },
    )

    key = lb._build_session_fingerprint(
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-secret"},
    )
    assert "raw-secret" not in key
    assert "raw-api-key" not in key
    assert "session-secret" not in key
    assert len(key) == 64
    # 会话粘滞记忆写入 outcomes（原 LoadBalancer._sticky_cache 收编）
    assert outcomes.session_sticky_get(key) is not None


@pytest.mark.asyncio
async def test_sticky_cache_entry_is_ignored_when_channel_excluded():
    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    channels = [make_channel("a"), make_channel("b")]
    first = await lb.select_channel(
        channels,
        model=MODEL,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    second = await lb.select_channel(
        channels,
        exclude_ids={first.id},
        model=MODEL,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    assert second.id != first.id


@pytest.mark.asyncio
async def test_sticky_cache_lru_eviction_respects_max_entries():
    from proxy import outcomes

    lb = LoadBalancer()
    await lb.update_config(strategy="sticky", sticky_cache_max_entries=2)
    channels = [make_channel("a"), make_channel("b")]

    # Create 2 cache entries
    await lb.select_channel(
        channels,
        model=MODEL,
        client_ip="10.0.0.0",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-0"},
    )
    await lb.select_channel(
        channels,
        model=MODEL,
        client_ip="10.0.0.1",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    # Re-access session-0 to make it most recently used
    await lb.select_channel(
        channels,
        model=MODEL,
        client_ip="10.0.0.0",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-0"},
    )
    # Add session-2; should evict session-1 (least recently used), not session-0
    await lb.select_channel(
        channels,
        model=MODEL,
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

    assert outcomes.session_sticky_get(key1) is None
    assert outcomes.session_sticky_get(key0) is not None


@pytest.mark.asyncio
async def test_update_config_clears_sticky_cache_when_strategy_or_ttl_changes():
    from proxy import outcomes

    lb = LoadBalancer()
    await lb.update_config(strategy="sticky")
    key = lb._build_session_fingerprint(
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    await lb.select_channel(
        [make_channel("a"), make_channel("b")],
        model=MODEL,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    assert outcomes.session_sticky_get(key) is not None

    await lb.update_config(strategy="round_robin")
    assert outcomes.session_sticky_get(key) is None

    await lb.update_config(strategy="sticky")
    await lb.select_channel(
        [make_channel("a"), make_channel("b")],
        model=MODEL,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )
    assert outcomes.session_sticky_get(key) is not None

    await lb.update_config(strategy="sticky", sticky_ttl=900)
    assert outcomes.session_sticky_get(key) is None

    await lb.update_config(strategy="sticky", sticky_cache_max_entries=2000)
    assert outcomes.session_sticky_get(key) is None


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
            model=MODEL,
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
        model=MODEL,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    second = await lb.select_channel(
        channels,
        exclude_ids={first.id},
        model=MODEL,
        client_ip="10.0.0.5",
        api_key_id="key-name",
        client_headers={"x-session-id": "session-1"},
    )

    assert second.id != first.id
    assert second.id in {"a", "b"}
