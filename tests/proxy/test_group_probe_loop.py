"""Ticket 09 — proxy/group_probe 后台循环与生命周期接线测试。

围绕三个面：
- 循环 shell 不变量：启动即跑轮、空枚举零开销（``run_probe_round`` 不被调用）、
  cancel 干净退出（CancelledError 上抛、无孤儿任务）、双层异常隔离（取目标层唯一
  炸点与睡眠层唯一炸点各兜一层，循环均继续到下一轮）。
- 单轮主体（``_probe_round_once``，循环每轮执行体）：每轮热读 interval /
  concurrency / timeout（改后无需重启）、interval ≥1s 守卫、无 lazy 组时枚举
  即空且不调 ``run_probe_round``。
- 并发约束：``run_probe_round`` 内 Semaphore 以 ``group_probe_concurrency`` 封顶
  活动探测数（07 未覆盖的显式并发上限断言）+ 循环按热读并发透传。

确定性驱动（spec 测试决策）：patch 模块级 ``take_probe_candidates /
run_probe_round / _sleep_until_next`` 注入，不依赖真实 sleep 断言内部时序；以
Event / 计数收敛作为「行为发生」证据，对确由 1s 睡眠驱动的时间收敛用
``asyncio.wait_for`` 限界（断言结果、不断言时序）。
"""

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, patch

import pytest

from models.model_group import ModelGroup
from proxy import group_probe, outcomes
from proxy.group_probe import ProbeCandidate, ProbeGroup
from proxy.outcomes import OutcomeKind

pytestmark = pytest.mark.asyncio


def _pg(group_id="g1", name="g1"):
    return ProbeGroup(id=group_id, name=name, group=ModelGroup(id=group_id, name=name))


def _target(model="m1", channel_id="ch_a", *, groups=None):
    groups = groups or [_pg()]
    return ProbeCandidate(
        model=model,
        channel_id=channel_id,
        first_failed_at=9000.0,
        consecutive_failures=1,
        permanent=False,
        groups=groups,
        requested_model=groups[0].name,
        next_due_at=10000.0,
    )


@pytest.fixture(autouse=True)
def _reset():
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    yield
    outcomes.reset()


async def _wait_until(cond, *, timeout=5.0, interval=0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError("条件在限期内未达成")


async def _yielding_sleep(started, interval):
    """替代 patch 用睡眠：``await asyncio.sleep(0)`` 让循环每轮让出事件循环。

    若用纯无 await 的 noop（AsyncMock 之类），循环本体全是立即完成的 await，
    单任务持续占用 CPU、事件循环被饿死（Event/timer 永不触发）——测试必须让
    循环真实让出。with real ``_sleep_until_next``（内部 ``asyncio.sleep``）同样
    天然让出，无需此替身。
    """
    await asyncio.sleep(0)


# ── 循环 shell 不变量 ──


async def test_loop_runs_first_round_and_cancels_cleanly():
    """启动即跑一轮；空枚举零开销（run_probe_round 不被调用）；cancel 干净退出。"""
    first_round = asyncio.Event()

    async def fake_take(**kw):
        first_round.set()
        return []

    with (
        patch.object(group_probe, "take_probe_candidates", side_effect=fake_take),
        patch.object(group_probe, "run_probe_round", new=AsyncMock()) as probe,
    ):
        task = asyncio.create_task(group_probe.run_group_probe_loop(interval_seconds=3))
        await asyncio.wait_for(first_round.wait(), 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert task.cancelled()  # 取消干净上抛 CancelledError，无其他异常、无孤儿任务
    assert probe.await_count == 0  # 空枚举 → 零开销，单轮驱动不被调用


async def test_loop_survives_take_exception_continues_next_round():
    """内层隔离：take_probe_candidates 炸掉 → log 后继续；下一轮照常跑。"""
    round2_ran = asyncio.Event()
    state = {"raised": False}

    async def fake_take(**kw):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("boom")
        round2_ran.set()
        return []

    with (
        patch.object(group_probe, "take_probe_candidates", side_effect=fake_take),
        patch.object(group_probe, "run_probe_round", new=AsyncMock()),
        patch.object(group_probe, "_sleep_until_next", side_effect=_yielding_sleep),
    ):
        task = asyncio.create_task(group_probe.run_group_probe_loop(interval_seconds=1))
        await asyncio.wait_for(round2_ran.wait(), 3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert task.cancelled()  # 一轮异常后循环未退化，仍正常响应取消


async def test_loop_survives_sleep_exception_via_global_layer():
    """全局兜底层：睡眠层唯一炸点异常 → 循环继续到下一轮（取目标再次发生）。"""
    takes = {"n": 0}
    sleeps = {"n": 0}

    async def fake_take(**kw):
        takes["n"] += 1
        return []

    async def flaky_sleep(started, interval):
        sleeps["n"] += 1
        await asyncio.sleep(0)  # 让出事件循环，避免单任务饿死 loop
        if sleeps["n"] == 1:
            raise RuntimeError("sleep boom")

    with (
        patch.object(group_probe, "take_probe_candidates", side_effect=fake_take),
        patch.object(group_probe, "run_probe_round", new=AsyncMock()),
        patch.object(group_probe, "_sleep_until_next", side_effect=flaky_sleep),
    ):
        task = asyncio.create_task(group_probe.run_group_probe_loop(interval_seconds=1))
        await _wait_until(lambda: takes["n"] >= 2, timeout=5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert takes["n"] >= 2  # 外层兜住睡眠异常后，下一轮照常取目标
    assert task.cancelled()


# ── 单轮主体：热读 / 守卫 / 零开销 ──


async def test_round_body_hot_reads_cadence_each_round():
    """每轮热读 interval：配置改动无需重启，下一轮生效（用户故事 21）。"""
    settings = {
        "group_probe_interval_seconds": 30,
        "group_probe_concurrency": 5,
        "group_probe_timeout": 10,
    }
    seen = []

    async def fake_take(*, interval_seconds):
        seen.append(interval_seconds)
        return []

    with patch("proxy.group_probe.config.get_setting", side_effect=lambda k: settings.get(k)):
        with patch.object(group_probe, "take_probe_candidates", side_effect=fake_take):
            with patch.object(group_probe, "run_probe_round", new=AsyncMock()) as probe:
                await group_probe._probe_round_once()
                settings["group_probe_interval_seconds"] = 45
                await group_probe._probe_round_once()

    assert seen == [30.0, 45.0]
    assert probe.await_count == 0  # 空候选 → run_probe_round 不被调用


async def test_interval_clamped_to_one_second_minimum():
    """interval ≥1s 睡眠守卫：config 极短间隔被钳到 1s，护空枚举不热循环。"""
    settings = {"group_probe_interval_seconds": 0.2, "group_probe_concurrency": 5, "group_probe_timeout": 10}

    with patch("proxy.group_probe.config.get_setting", side_effect=lambda k: settings.get(k)):
        interval, concurrency, timeout = group_probe._read_probe_cadence(None)

    assert interval == 1.0
    assert concurrency == 5
    assert timeout == 10.0


async def test_round_body_zero_overhead_without_lazy_groups():
    """无 lazy_sticky 组：真实枚举即空（load_model_groups → []），单轮驱动不调用。"""
    with patch("channel_catalog.catalog.model_groups", new=AsyncMock(return_value=[])):
        with patch.object(group_probe, "run_probe_round", new=AsyncMock()) as probe:
            interval = await group_probe._probe_round_once(interval_seconds=60)

    assert interval == 60.0
    assert probe.await_count == 0


async def test_round_body_runs_probe_only_with_candidates():
    """有候选才驱动单轮：热读并发/超时原样透传（并发约束由 07 的 Semaphore 落实）。"""
    settings = {
        "group_probe_interval_seconds": 30,
        "group_probe_concurrency": 7,
        "group_probe_timeout": 3,
    }
    target = _target()
    seen = {"interval": None}

    async def fake_take(*, interval_seconds):
        seen["interval"] = interval_seconds
        return [target]

    with patch("proxy.group_probe.config.get_setting", side_effect=lambda k: settings.get(k)):
        with patch.object(group_probe, "take_probe_candidates", side_effect=fake_take):
            with patch.object(group_probe, "run_probe_round", new=AsyncMock()) as probe:
                interval = await group_probe._probe_round_once()

    assert interval == 30.0
    assert seen["interval"] == 30.0
    assert probe.await_count == 1
    assert (probe.await_args.kwargs["timeout"], probe.await_args.kwargs["concurrency"]) == (3.0, 7)


# ── 并发约束 ──


async def test_concurrency_capped_by_group_probe_concurrency():
    """Semaphore 封顶活动探测数：N=10、并发 3 → 峰值 3，全部目标均完成。"""
    targets = [_target(f"m{i}", f"ch{i}") for i in range(10)]
    metrics = {"active": 0, "peak": 0, "finished": 0}

    async def slow_probe(target, *, timeout):
        metrics["active"] += 1
        metrics["peak"] = max(metrics["peak"], metrics["active"])
        await asyncio.sleep(0.02)
        metrics["active"] -= 1
        metrics["finished"] += 1
        return group_probe.ProbeTargetResult(
            model=target.model,
            channel_id=target.channel_id,
            kind="success",
            groups=tuple(target.groups),
            requested_model=target.requested_model,
        )

    with patch.object(group_probe, "_probe_one_target", side_effect=slow_probe):
        result = await group_probe.run_probe_round(targets, timeout=1, concurrency=3)

    assert metrics["peak"] == 3
    assert metrics["finished"] == 10
    assert len(result.succeeded) == 10


# ── 重启后从干净状态重新枚举 ──


async def test_loop_restart_re_enumerates_fresh_state():
    """循环不持有跨轮状态：停掉带候选的循环后，新轮在空的当前态下零开销重枚举。"""
    first_round = asyncio.Event()

    async def fake_take_with_targets(**kw):
        first_round.set()
        return [_target()]

    with (
        patch.object(group_probe, "take_probe_candidates", side_effect=fake_take_with_targets),
        patch.object(group_probe, "run_probe_round", new=AsyncMock()),
    ):
        task = asyncio.create_task(group_probe.run_group_probe_loop(interval_seconds=3))
        await asyncio.wait_for(first_round.wait(), 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert task.cancelled()

    # 停顿后干净重枚举：当前态（空候选）下再次单轮 → 零开销
    with (
        patch.object(group_probe, "take_probe_candidates", new=AsyncMock(return_value=[])),
        patch.object(group_probe, "run_probe_round", new=AsyncMock()) as probe,
    ):
        await group_probe._probe_round_once()

    assert probe.await_count == 0
    # 降级态独立留存于 outcomes（全局，非循环持有）：手动记账仍在视图里
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)
    assert outcomes.is_degraded("m1", "ch_a") is True
