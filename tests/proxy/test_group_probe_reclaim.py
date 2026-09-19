"""Ticket 08 — 探活成功粘滞记忆抢回与同轮收敛（proxy/group_probe 写回）。

Unit 缝：直接手搭 ``ProbeRoundResult`` / ``ProbeTargetResult`` 驱动
``reclaim_sticky_preferred`` / ``_converge_writes``——单成功抢回、同组同模型
多渠道收敛（items 序 / 渠道字典序确定性）、跨组共享对写回各自组键、失败/跳过
零接触 ``_preferred``、零 ``record`` 副效应。Integration 缝：真实
``run_probe_round`` + mock 流成功后断言粘滞记忆被抢回、``record(success)``
仅一次（写回只加 ``remember_preferred``）、降级对出视图；随后按
``tests/proxy/test_group_lazy_sticky.py`` 的 patch 配方调
``execute_model_group_request`` 断言后续请求自首选（被抢回）渠道起、不落备用。
"""

from unittest.mock import AsyncMock, patch

import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from models.model_group import ModelGroup
from proxy import group_probe, outcomes
from proxy.group_probe import ProbeGroup
from proxy.outcomes import OutcomeKind

pytestmark = pytest.mark.asyncio


def _ch(channel_id, model="m1", api_type=APIType.OPENAI_CHAT):
    return Channel(
        id=channel_id,
        name=channel_id,
        api_key="k",
        models=[model],
        enabled=True,
        weight=1,
        priority=1,
        endpoints=[Endpoint(api_type=api_type, base_url=f"http://{channel_id}")],
    )


def _grp(group_id, items=None, *, lazy_sticky=True):
    return ModelGroup(id=group_id, name=group_id, items=items or [], enabled=True, lazy_sticky=lazy_sticky)


def _pg(group):
    return ProbeGroup(id=group.id, name=group.name, group=group)


def _ok(model, channel_id, *groups):
    return group_probe.ProbeTargetResult(
        model=model,
        channel_id=channel_id,
        kind="success",
        groups=tuple(groups),
        requested_model=groups[0].name if groups else "probe-grp",
    )


def _fail(model, channel_id, *groups, kind=OutcomeKind.http_5xx.value):
    return group_probe.ProbeTargetResult(
        model=model,
        channel_id=channel_id,
        kind=kind,
        groups=tuple(groups),
        requested_model=groups[0].name if groups else "probe-grp",
    )


@pytest.fixture(autouse=True)
def _reset():
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    from balancer.load_balancer import load_balancer

    load_balancer._current_weights.clear()
    yield
    outcomes.reset()


# ── Unit：reclaim_sticky_preferred / _converge_writes 直驱 ──


async def test_single_success_reclaims_sticky():
    """单成功：对所属 lazy 组 remember_preferred → sticky_preferred 抢回主渠道。"""
    g1 = _grp("g1", [{"model": "m1"}])
    result = group_probe.ProbeRoundResult(
        succeeded=(_ok("m1", "ch_a", _pg(g1)),),
        failed=(),
        skipped=(),
    )

    group_probe.reclaim_sticky_preferred(result)

    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_a"


async def test_same_model_two_channels_converges_deterministically():
    """同组同模型两个渠道成功 → 收敛到 items 序最靠前渠道（回到主）。

    ``(group, model)`` 的 items 位置是模型级首个条目、同模型各渠道恒相等，
    故胜者由渠道字典序平局决胜——但无论成功目标的扫描顺序如何，最终记忆一致。
    """
    g1 = _grp("g1", [{"model": "m1"}, {"model": "m1"}])
    outcomes.remember_preferred(g1.id, "m1", "ch_b")  # 记忆现停在备用

    plan = group_probe._converge_writes(
        group_probe.ProbeRoundResult(succeeded=(_ok("m1", "ch_b", _pg(g1)), _ok("m1", "ch_a", _pg(g1))), failed=(), skipped=())
    )
    assert plan == [("g1", "m1", "ch_a")]
    group_probe.reclaim_sticky_preferred(
        group_probe.ProbeRoundResult(succeeded=(_ok("m1", "ch_b", _pg(g1)), _ok("m1", "ch_a", _pg(g1))), failed=(), skipped=())
    )
    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_a"

    # items 序反转 / 成功目标扫描序反转均不影响收敛结果（确定性）
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    g2 = _grp("g2", [{"model": "m2"}, {"model": "m2"}])
    reversed_plan = group_probe._converge_writes(
        group_probe.ProbeRoundResult(succeeded=(_ok("m2", "ch_a", _pg(g2)), _ok("m2", "ch_b", _pg(g2))), failed=(), skipped=())
    )
    assert reversed_plan == [("g2", "m2", "ch_a")]


async def test_items_position_anchors_convergence_when_positions_differ():
    """不同模型在同组处于不同 items 位置：各自 ``(group, model)`` 键收敛到本模型的
    锚定渠道，不互相覆盖（"组序 × items 序"在模型级成立）。"""
    g1 = _grp("g1", [{"model": "m1"}, {"model": "m2"}])

    plan = group_probe._converge_writes(
        group_probe.ProbeRoundResult(
            succeeded=(_ok("m1", "ch_p", _pg(g1)), _ok("m2", "ch_q", _pg(g1))),
            failed=(),
            skipped=(),
        )
    )
    assert plan == [("g1", "m1", "ch_p"), ("g1", "m2", "ch_q")]

    group_probe.reclaim_sticky_preferred(
        group_probe.ProbeRoundResult(
            succeeded=(_ok("m1", "ch_p", _pg(g1)), _ok("m2", "ch_q", _pg(g1))),
            failed=(),
            skipped=(),
        )
    )
    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_p"
    assert outcomes.sticky_preferred(g1.id, "m2") == "ch_q"


async def test_model_absent_from_items_falls_back_but_still_writes():
    """组 items 不含模型（测试手搭等异常态）：位置回退末尾哨兵，仍写回记忆。"""
    g1 = _grp("g1", [{"model": "other"}])
    result = group_probe.ProbeRoundResult(succeeded=(_ok("m1", "ch_a", _pg(g1)),), failed=(), skipped=())

    group_probe.reclaim_sticky_preferred(result)

    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_a"


async def test_shared_pair_writes_back_each_owning_group_key():
    """跨组共享对：只探一次（单目标携带多组），写回各自组键。"""
    g1 = _grp("g1", [{"model": "m1"}])
    g2 = _grp("g2", [{"model": "m1"}])
    result = group_probe.ProbeRoundResult(
        succeeded=(_ok("m1", "ch_a", _pg(g1), _pg(g2)),),
        failed=(),
        skipped=(),
    )

    group_probe.reclaim_sticky_preferred(result)

    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_a"
    assert outcomes.sticky_preferred(g2.id, "m1") == "ch_a"


async def test_failed_and_skipped_targets_never_touch_preferred():
    """失败 / 跳过目标绝不写 ``_preferred``（D7: 抢回只在成功分支）。"""
    g1 = _grp("g1", [{"model": "m1"}])
    result = group_probe.ProbeRoundResult(
        succeeded=(),
        failed=(_fail("m1", "ch_a", _pg(g1)),),
        skipped=(_fail("m1", "ch_b", _pg(g1), kind="skipped"),),
    )

    with patch.object(outcomes, "remember_preferred", side_effect=outcomes.remember_preferred) as remember:
        with patch.object(outcomes, "record", side_effect=outcomes.record) as record:
            group_probe.reclaim_sticky_preferred(result)

    assert remember.call_count == 0
    assert record.call_count == 0
    assert outcomes.sticky_preferred(g1.id, "m1") is None


async def test_reclaim_adds_no_record_side_effects():
    """写回只调 ``remember_preferred``，不产生任何 ``outcomes.record``（要求 5）。"""
    g1 = _grp("g1", [{"model": "m1"}])
    result = group_probe.ProbeRoundResult(succeeded=(_ok("m1", "ch_a", _pg(g1)),), failed=(), skipped=())

    with patch.object(outcomes, "remember_preferred", side_effect=outcomes.remember_preferred) as remember:
        with patch.object(outcomes, "record", side_effect=outcomes.record) as record:
            group_probe.reclaim_sticky_preferred(result)

    assert remember.call_count == 1
    assert record.call_count == 0


async def test_duplicate_succeeded_triple_converges_to_single_write():
    """同组同模型同渠道重复成功（理论上枚举已去重）：写回计划去重为单条。"""
    g1 = _grp("g1", [{"model": "m1"}])
    plan = group_probe._converge_writes(
        group_probe.ProbeRoundResult(
            succeeded=(_ok("m1", "ch_a", _pg(g1)), _ok("m1", "ch_a", _pg(g1))),
            failed=(),
            skipped=(),
        )
    )
    assert plan == [("g1", "m1", "ch_a")]


# ── Integration：真实 run_probe_round + 组路由从首选渠道起 ──


class FakeStreamResponse:
    """单 chunk + [DONE] 的 OpenAI Chat 流式响应（与 test_group_probe_round 同形）。"""

    status_code = 200
    is_error = False
    headers = {"content-type": "text/event-stream"}

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        yield 'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}'
        yield ""
        yield "data: [DONE]"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeClient:
    def stream(self, method, url, *, json=None, headers=None):
        return FakeStreamResponse()

    async def aclose(self):
        return None


def _candidate(model, channel_id, *groups):
    return group_probe.ProbeCandidate(
        model=model,
        channel_id=channel_id,
        first_failed_at=9000.0,
        consecutive_failures=1,
        permanent=False,
        groups=list(groups),
        requested_model=groups[0].name if groups else "probe-grp",
        next_due_at=10000.0,
    )


async def test_run_probe_round_success_reclaims_and_records_success_once():
    """真实 run_probe_round 成功 → 记录恰一次 success、记忆抢回、降级出视图。

    同时验证 07 记账不被破坏：``record(success)`` 仅由发送链发出一次，
    写回只补 ``remember_preferred``，无额外 ``record``。
    """
    ch = _ch("ch_a")
    g1 = _grp("g1", [{"model": "m1"}])
    target = _candidate("m1", "ch_a", _pg(g1))
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)
    assert outcomes.is_degraded("m1", "ch_a") is True

    with (
        patch.object(outcomes, "record", wraps=outcomes.record) as record_mock,
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch])),
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient()),
    ):
        result = await group_probe.run_probe_round([target], timeout=5)

    assert result.succeeded_pairs == {("m1", "ch_a")}
    assert result.failed == () and result.skipped == ()
    # 粘滞记忆被抢回（写回副作用），降级对出视图
    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_a"
    assert outcomes.is_degraded("m1", "ch_a") is False
    assert not [t for t in outcomes.probe_targets() if t.channel_id == "ch_a"]

    success_calls = [c for c in record_mock.call_args_list if c.args[2] == OutcomeKind.success]
    assert len(success_calls) == 1
    assert (success_calls[0].args[0], success_calls[0].args[1]) == ("m1", "ch_a")
    assert record_mock.call_count == 1  # 成功路径整轮只记一次，写回不新增 record


async def test_recovered_pair_routes_to_reclaimed_primary_without_backup():
    """回切后组路由：后续请求自首选（被抢回）渠道起，不落备用。

    patch 配方：channel_catalog.catalog.channels_for_model / proxy.conversion.filter_channels_by_conversion
    / Channel Attempt 注入（tests/proxy/test_group_lazy_sticky.py）。
    """
    from proxy.model_group_dispatch import ModelGroupRequestContext, execute_model_group_request

    ch_p = _ch("ch_p")
    ch_b = _ch("ch_b")
    g1 = _grp("g1", [{"model": "m1"}])

    # 模拟过往粘滞：记忆停在备用 ch_b；主 ch_p 降级
    outcomes.remember_preferred(g1.id, "m1", "ch_b")
    outcomes.record("m1", "ch_p", OutcomeKind.http_5xx, t=9000.0)

    # 探活成功 → 主渠道恢复，记忆被抢回
    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(side_effect=lambda m: [ch_p, ch_b])),
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient()),
    ):
        result = await group_probe.run_probe_round([_candidate("m1", "ch_p", _pg(g1))], timeout=5)

    assert result.succeeded_pairs == {("m1", "ch_p")}
    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_p"
    assert outcomes.is_degraded("m1", "ch_p") is False

    # 组路由：首选渠道被抢回为 ch_p，请求应起于 ch_p 且不触碰备用 ch_b
    attempted: list[str] = []

    async def attempt(channel, *args, **kwargs):
        from proxy.channel_attempt import NonStreamAttemptResult

        attempted.append(channel.id)
        return NonStreamAttemptResult({"ok": True}, channel, channel.endpoints[0])

    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch_p, ch_b])),
        patch("proxy.conversion.filter_channels_by_conversion", side_effect=lambda chs, api: chs),
        patch("proxy.model_group_dispatch.attempt_channel", side_effect=attempt),
    ):
        _resp, served = await execute_model_group_request(
            g1, ModelGroupRequestContext({"model": "g1"}, APIType.OPENAI_CHAT, False, None, None, None, None)
        )

    assert served.id == "ch_p"
    assert attempted == ["ch_p"]
    assert outcomes.sticky_preferred(g1.id, "m1") == "ch_p"
