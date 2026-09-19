"""Ticket 05 group lazy sticky vertical slice tests."""

from unittest.mock import AsyncMock, patch

import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from models.model_group import ModelGroup
from proxy import outcomes
from proxy.channel_attempt import NonStreamAttemptResult
from proxy.model_group_dispatch import ModelGroupRequestContext, execute_model_group_request


def _ch(id, name):
    return Channel(
        id=id,
        name=name,
        api_key="k",
        models=["m1"],
        enabled=True,
        weight=1,
        priority=1,
        endpoints=[Endpoint(api_type="openai-chat-completions", base_url=f"http://{id}")],
    )


@pytest.fixture(autouse=True)
def _reset():
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    from balancer.load_balancer import load_balancer

    load_balancer._current_weights.clear()
    yield
    outcomes.reset()


@pytest.mark.asyncio
async def test_sticky_stay_on_primary():
    ch_a = _ch("ch_a", "A")
    ch_b = _ch("ch_b", "B")
    group = ModelGroup(id="grp1", name="g1", items=[{"model": "m1", "channel_id": None}], enabled=True, lazy_sticky=True)
    from balancer.load_balancer import load_balancer

    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    load_balancer._current_weights.clear()
    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch_a, ch_b])),
        patch("proxy.conversion.filter_channels_by_conversion", side_effect=lambda chs, api: chs),
        patch("proxy.model_group_dispatch.attempt_channel", new=AsyncMock()) as mock_att,
    ):

        async def ok(ch, *a, **k):
            return NonStreamAttemptResult({"ok": True}, ch, ch.endpoints[0])

        mock_att.side_effect = ok
        r1, c1 = await execute_model_group_request(
            group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )
        assert c1.id == "ch_a"
        assert outcomes.sticky_preferred(group.id, "m1") == "ch_a"
        r2, c2 = await execute_model_group_request(
            group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )
        assert c2.id == "ch_a"


@pytest.mark.asyncio
async def test_cut_to_backup_and_stay():
    ch_a = _ch("ch_a", "A")
    ch_b = _ch("ch_b", "B")
    group = ModelGroup(id="grp1", name="g1", items=[{"model": "m1", "channel_id": None}], enabled=True, lazy_sticky=True)
    from balancer.load_balancer import load_balancer

    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    load_balancer._current_weights.clear()
    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch_a, ch_b])),
        patch("proxy.conversion.filter_channels_by_conversion", side_effect=lambda chs, api: chs),
        patch("proxy.model_group_dispatch.attempt_channel", new=AsyncMock()) as mock_att,
    ):
        from proxy.channel_attempt import ChannelAttemptExhausted

        async def ok(ch, *a, **k):
            return NonStreamAttemptResult({"ok": True}, ch, ch.endpoints[0])

        mock_att.side_effect = ok
        r1, c1 = await execute_model_group_request(
            group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )
        assert c1.id == "ch_a"

        async def fail_a(ch, *a, **k):
            if ch.id == "ch_a":
                raise ChannelAttemptExhausted(ch, Exception("fail"))
            return NonStreamAttemptResult({"ok": True}, ch, ch.endpoints[0])

        mock_att.side_effect = fail_a
        r2, c2 = await execute_model_group_request(
            group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )
        assert c2.id == "ch_b"
        assert outcomes.sticky_preferred(group.id, "m1") == "ch_b"
        mock_att.side_effect = ok
        r3, c3 = await execute_model_group_request(
            group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )
        assert c3.id == "ch_b"


@pytest.mark.asyncio
async def test_degraded_yield():
    ch_a = _ch("ch_a", "A")
    ch_b = _ch("ch_b", "B")
    group = ModelGroup(id="grp1", name="g1", items=[{"model": "m1", "channel_id": None}], enabled=True, lazy_sticky=True)
    from balancer.load_balancer import load_balancer

    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    load_balancer._current_weights.clear()
    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch_a, ch_b])),
        patch("proxy.conversion.filter_channels_by_conversion", side_effect=lambda chs, api: chs),
        patch("proxy.model_group_dispatch.attempt_channel", new=AsyncMock()) as mock_att,
    ):

        async def ok(ch, *a, **k):
            return NonStreamAttemptResult({"ok": True}, ch, ch.endpoints[0])

        mock_att.side_effect = ok
        await execute_model_group_request(group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None))
        outcomes.remember_preferred(group.id, "m1", "ch_b")
        outcomes.record("m1", "ch_b", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_degraded("m1", "ch_b") is True
        load_balancer._current_weights.clear()
        r, c = await execute_model_group_request(group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None))
        assert c.id == "ch_a"


@pytest.mark.asyncio
async def test_lazy_off_fallback_round_robin():
    ch_a = _ch("ch_a", "A")
    ch_b = _ch("ch_b", "B")
    group = ModelGroup(id="grp1", name="g1", items=[{"model": "m1", "channel_id": None}], enabled=True, lazy_sticky=False)
    from balancer.load_balancer import load_balancer

    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    load_balancer._current_weights.clear()
    outcomes.remember_preferred(group.id, "m1", "ch_a")
    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch_a, ch_b])),
        patch("proxy.conversion.filter_channels_by_conversion", side_effect=lambda chs, api: chs),
        patch("proxy.model_group_dispatch.attempt_channel", new=AsyncMock()) as mock_att,
    ):

        async def ok(ch, *a, **k):
            return NonStreamAttemptResult({"ok": True}, ch, ch.endpoints[0])

        mock_att.side_effect = ok
        r1, c1 = await execute_model_group_request(
            group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )
        r2, c2 = await execute_model_group_request(
            group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )
        assert c1.id == "ch_a"
        assert c2.id == "ch_b"


@pytest.mark.asyncio
async def test_tried_channels_skip():
    ch_a = _ch("ch_a", "A")
    ch_b = _ch("ch_b", "B")
    group = ModelGroup(id="grp1", name="g1", items=[{"model": "m1", "channel_id": None}], enabled=True, lazy_sticky=True)
    from balancer.load_balancer import load_balancer

    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    load_balancer._current_weights.clear()
    outcomes.remember_preferred(group.id, "m1", "ch_a")
    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch_a, ch_b])),
        patch("proxy.conversion.filter_channels_by_conversion", side_effect=lambda chs, api: chs),
        patch("proxy.model_group_dispatch.attempt_channel", new=AsyncMock()) as mock_att,
    ):
        from proxy.channel_attempt import ChannelAttemptExhausted

        async def fail_a(ch, *a, **k):
            if ch.id == "ch_a":
                raise ChannelAttemptExhausted(ch, Exception("fail"))
            return NonStreamAttemptResult({"ok": True}, ch, ch.endpoints[0])

        mock_att.side_effect = fail_a
        r, c = await execute_model_group_request(group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None))
        assert c.id == "ch_b"


@pytest.mark.asyncio
async def test_hard_bound_not_sticky():
    ch_a = _ch("ch_a", "A")
    ch_b = _ch("ch_b", "B")
    group = ModelGroup(
        id="grp1", name="g1", items=[{"model": "m1", "channel_id": "ch_a"}, {"model": "m1", "channel_id": None}], enabled=True, lazy_sticky=True
    )
    from balancer.load_balancer import load_balancer

    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    load_balancer._current_weights.clear()
    outcomes.remember_preferred(group.id, "m1", "ch_b")
    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch_a, ch_b])),
        patch("proxy.conversion.filter_channels_by_conversion", side_effect=lambda chs, api: chs),
        patch("proxy.model_group_dispatch.attempt_channel", new=AsyncMock()) as mock_att,
    ):

        async def ok(ch, *a, **k):
            return NonStreamAttemptResult({"ok": True}, ch, ch.endpoints[0])

        mock_att.side_effect = ok
        r, c = await execute_model_group_request(group, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None))
        assert c.id == "ch_a"
