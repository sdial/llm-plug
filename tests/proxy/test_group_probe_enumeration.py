"""Ticket 06 — proxy/group_probe.take_probe_candidates 纯读枚举测试。

只断言外部可观察行为：产出的探活目标清单（降级对 / 跨组去重 / 所属 lazy 组 /
行级退避信息 / ``requested_model`` 先到先得）。剪枝由枚举侧天然完成——组删 /
组停用 / 关 ``lazy_sticky`` / 模型条目移除 / 渠道删 / 渠道禁用 / 格式门控排除后
目标消失；窗口硬限制与 ``permanent`` 对不产出；退避未到期对在读侧跳过。
零记账：调用后 ``outcomes`` 视图不变（无 ``record`` 副作用）。

构造方式沿用 ``tests/proxy/test_group_lazy_sticky.py``：直接构建 ``Channel`` /
``Endpoint`` / ``ModelGroup``，patch Channel Catalog + channel
registry + conversion 注入确定性输入（spec「确定性驱动」测试决策）；退避用
``now`` / ``interval_seconds`` 参数驱动，不依赖真实 sleep。
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from models.model_group import ModelGroup
from proxy import group_probe, outcomes
from proxy.conversion import filter_channels_by_conversion


def _ch(channel_id, models=("m1",), api_type=APIType.OPENAI_CHAT, *, enabled=True, allow_format_conversion=None):
    return Channel(
        id=channel_id,
        name=channel_id,
        api_key="k",
        models=list(models),
        enabled=enabled,
        weight=1,
        priority=1,
        allow_format_conversion=allow_format_conversion,
        endpoints=[Endpoint(api_type=api_type, base_url=f"http://{channel_id}")],
    )


def _grp(group_id, name=None, items=None, *, enabled=True, lazy_sticky=True):
    return ModelGroup(id=group_id, name=name or group_id, items=items or [], enabled=enabled, lazy_sticky=lazy_sticky)


def _items(*specs):
    return [{"model": model, "channel_id": channel_id} for model, channel_id in specs]


@pytest.fixture(autouse=True)
def _reset():
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    yield
    outcomes.reset()


async def _take(groups, channels_by_model, *, now=10000.0, interval=60.0, filter_hook=None):
    def _convert(channels, api):
        if filter_hook is not None:
            channels = filter_hook(channels, api)
        return filter_channels_by_conversion(channels, api)

    with (
        patch("channel_catalog.catalog.model_groups", new=AsyncMock(return_value=groups)),
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(side_effect=lambda m: channels_by_model.get(m, []))),
        patch("proxy.group_probe.filter_channels_by_conversion", side_effect=_convert),
    ):
        return await group_probe.take_probe_candidates(now=now, interval_seconds=interval)


@pytest.mark.asyncio
async def test_hard_bound_entry_produces_no_target():
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    group = _grp("g1", items=[{"model": "m1", "channel_id": "ch_a"}, {"model": "m2", "channel_id": None}])
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)
    outcomes.record("m2", "ch_b", outcomes.OutcomeKind.http_5xx, t=9000.0)

    targets = await _take([group], {"m1": [ch_a], "m2": [ch_b]})

    assert [(t.model, t.channel_id) for t in targets] == [("m2", "ch_b")]


@pytest.mark.asyncio
async def test_targets_pruned_by_group_scope():
    ch_a = _ch("ch_a")
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)

    lazy = _grp("g1", items=_items(("m1", None)))
    assert len(await _take([lazy], {"m1": [ch_a]})) == 1

    # 组删除
    assert await _take([], {"m1": [ch_a]}) == []
    # 组停用
    disabled = _grp("g1", items=_items(("m1", None)), enabled=False)
    assert await _take([disabled], {"m1": [ch_a]}) == []
    # 关 lazy_sticky
    no_sticky = _grp("g1", items=_items(("m1", None)), lazy_sticky=False)
    assert await _take([no_sticky], {"m1": [ch_a]}) == []
    # 模型条目移除
    entry_removed = _grp("g1", items=_items(("m2", None)))
    assert await _take([entry_removed], {"m1": [ch_a]}) == []


@pytest.mark.asyncio
async def test_target_pruned_when_channel_removed_or_disabled():
    ch_a = _ch("ch_a")
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)
    group = _grp("g1", items=_items(("m1", None)))

    assert len(await _take([group], {"m1": [ch_a]})) == 1
    # 渠道删除 / 禁用：registry 为该模型返回空集（get_channels_for_model 已排除禁用渠道）
    assert await _take([group], {"m1": []}) == []


@pytest.mark.asyncio
async def test_conversion_gated_channel_excluded():
    ch_ant = _ch("ch_ant", api_type=APIType.ANTHROPIC, allow_format_conversion=False)
    outcomes.record("m1", "ch_ant", outcomes.OutcomeKind.http_5xx, t=9000.0)
    group = _grp("g1", items=_items(("m1", None)))

    assert await _take([group], {"m1": [ch_ant]}) == []

    # 允许跨格式转换时 anthropic 渠道可服务 OpenAI Chat 探活请求，通过门控
    ch_ant_conv = _ch("ch_ant_conv", api_type=APIType.ANTHROPIC)
    outcomes.record("m1", "ch_ant_conv", outcomes.OutcomeKind.http_5xx, t=9000.0)
    targets = await _take([group], {"m1": [ch_ant_conv]})
    assert [(t.model, t.channel_id) for t in targets] == [("m1", "ch_ant_conv")]


@pytest.mark.asyncio
async def test_probe_requests_target_openai_chat():
    ch_a = _ch("ch_a")
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)
    group = _grp("g1", items=_items(("m1", None)))
    seen_api_types = []

    def record_api(channels, api):
        seen_api_types.append(api)
        return channels

    targets = await _take([group], {"m1": [ch_a]}, filter_hook=record_api)

    assert seen_api_types == [APIType.OPENAI_CHAT]
    assert len(targets) == 1


@pytest.mark.asyncio
async def test_only_degraded_pairs_produce_targets():
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    group = _grp("g1", items=_items(("m1", None)))
    outcomes.record("m1", "ch_b", outcomes.OutcomeKind.http_5xx, t=9000.0)

    targets = await _take([group], {"m1": [ch_a, ch_b]})

    assert [(t.model, t.channel_id) for t in targets] == [("m1", "ch_b")]
    assert outcomes.is_degraded("m1", "ch_a") is False


@pytest.mark.asyncio
async def test_blocked_channel_pair_excluded():
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    group = _grp("g1", items=_items(("m1", None)))
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)
    outcomes.record("m1", "ch_b", outcomes.OutcomeKind.http_5xx, t=9000.0)
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)

    targets = await _take([group], {"m1": [ch_a, ch_b]})

    assert [(t.model, t.channel_id) for t in targets] == [("m1", "ch_b")]


@pytest.mark.asyncio
async def test_permanent_pair_excluded():
    ch_a = _ch("ch_a")
    group = _grp("g1", items=_items(("m1", None)))
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_4xx_config, t=9000.0)

    # permanent 保留降级视图（组层持续不可选），仅移出探活调度
    assert outcomes.is_degraded("m1", "ch_a") is True
    assert await _take([group], {"m1": [ch_a]}) == []


@pytest.mark.asyncio
async def test_shared_pair_deduplicated_across_groups_first_wins():
    ch_a = _ch("ch_a")
    g1 = _grp("g1", name="Group-One", items=_items(("m1", None)))
    g2 = _grp("g2", name="Group-Two", items=_items(("m1", None)))
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)

    (target,) = await _take([g1, g2], {"m1": [ch_a]})

    assert target.model == "m1"
    assert target.channel_id == "ch_a"
    assert [scope.id for scope in target.groups] == ["g1", "g2"]
    assert [scope.name for scope in target.groups] == ["Group-One", "Group-Two"]
    assert target.groups[1].group is g2  # 携带完整组对象供写回
    assert target.requested_model == "Group-One"

    # 轮次顺序反转 → requested_model / groups 跟随组枚举序
    (r,) = await _take([g2, g1], {"m1": [ch_a]})
    assert [scope.id for scope in r.groups] == ["g2", "g1"]
    assert r.requested_model == "Group-Two"


@pytest.mark.asyncio
async def test_duplicate_entries_in_same_group_not_repeated():
    ch_a = _ch("ch_a")
    group = _grp("g1", items=_items(("m1", None), ("m1", None)))
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)

    (target,) = await _take([group], {"m1": [ch_a]})

    assert [scope.id for scope in target.groups] == ["g1"]
    assert target.requested_model == "g1"


@pytest.mark.asyncio
async def test_first_failure_backoff_skips_until_due():
    ch_a = _ch("ch_a")
    group = _grp("g1", items=_items(("m1", None)))
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)
    # consecutive_failures=1 → k=0 → next_due_at = first_failed_at + interval

    assert await _take([group], {"m1": [ch_a]}, now=9050.0, interval=60.0) == []

    (target,) = await _take([group], {"m1": [ch_a]}, now=9060.0, interval=60.0)
    assert target.consecutive_failures == 1
    assert target.first_failed_at == 9000.0
    assert target.next_due_at == 9060.0


@pytest.mark.asyncio
async def test_consecutive_failures_escalate_backoff_geometrically():
    ch_a = _ch("ch_a")
    group = _grp("g1", items=_items(("m1", None)))
    for i in range(3):
        outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0 + i)
    # consecutive_failures=3 → k=2 → next_due_at = 9000 + 60×2² = 9240

    assert await _take([group], {"m1": [ch_a]}, now=9200.0, interval=60.0) == []

    (target,) = await _take([group], {"m1": [ch_a]}, now=9300.0, interval=60.0)
    assert target.next_due_at == 9240.0


@pytest.mark.asyncio
async def test_backoff_capped_at_600_seconds():
    ch_a = _ch("ch_a")
    group = _grp("g1", items=_items(("m1", None)))
    for i in range(20):
        outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0 + i)

    (target,) = await _take([group], {"m1": [ch_a]}, now=10000.0, interval=60.0)

    assert target.consecutive_failures == 20
    assert target.next_due_at == 9600.0  # 9000 + min(60×2^19, 600)


@pytest.mark.asyncio
async def test_zero_bookkeeping_views_unchanged():
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    g1 = _grp("g1", items=_items(("m1", None)))
    outcomes.record("m1", "ch_a", outcomes.OutcomeKind.http_5xx, t=9000.0)
    outcomes.record("m1", "ch_b", outcomes.OutcomeKind.http_4xx_config, t=9000.0)
    outcomes.record("m2", "ch_c", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
    outcomes.remember_preferred(g1.id, "m1", "ch_a")

    before = (
        outcomes.is_degraded("m1", "ch_a"),
        outcomes.is_degraded("m1", "ch_b"),
        outcomes.is_blocked("ch_c"),
        outcomes.sticky_preferred(g1.id, "m1"),
        len(outcomes.probe_targets()),
    )

    await _take([g1], {"m1": [ch_a, ch_b]})

    after = (
        outcomes.is_degraded("m1", "ch_a"),
        outcomes.is_degraded("m1", "ch_b"),
        outcomes.is_blocked("ch_c"),
        outcomes.sticky_preferred(g1.id, "m1"),
        len(outcomes.probe_targets()),
    )
    assert after == before
