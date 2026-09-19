"""ADR-0011 调度器收敛单测：唯一调度器的循环语义（选路→尝试→429预算→排除）。

fake 候选池 + fake ``attempt_fn`` 覆盖正常路径、接入点穷尽、429 预算等待与耗尽、
quota_window 快速失败、yield_predicate 让位、空池耗尽、AllChannelsExhausted 透传。
"""

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy import outcomes
from proxy.channel_attempt import ChannelAttemptExhausted
from proxy.dispatcher import (
    DispatchContext,
    dispatch,
    dispatch_pinned,
)
from proxy.errors import AllChannelsExhausted
from proxy.outcomes import OutcomeKind
from rate_limiting import RateLimitExceeded


def _ch(id: str, name: str = None) -> Channel:
    return Channel(
        id=id,
        name=name or id,
        api_key="k",
        models=["m"],
        enabled=True,
        weight=1,
        priority=1,
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url=f"http://{id}")],
    )


@pytest.fixture(autouse=True)
def _reset_state():
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    from balancer.load_balancer import load_balancer

    load_balancer._current_weights.clear()
    yield
    outcomes.reset()


def _select_first_available(channels, exclude_ids=None, **kwargs):
    for ch in channels:
        if ch.id not in (exclude_ids or set()):
            return ch
    return None


@pytest.mark.asyncio
async def test_first_candidate_succeeds_returns_immediately(monkeypatch):
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    attempt = AsyncMock(return_value=({"ok": True}, ch_a))

    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    result, served = await dispatch([ch_a, ch_b], attempt, model="m")
    assert result == {"ok": True}
    assert served.id == "ch_a"
    assert attempt.await_count == 1


@pytest.mark.asyncio
async def test_endpoint_exhausted_skips_and_tries_next(monkeypatch):
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    causes = [RuntimeError("boom")]

    async def attempt(channel, wait_budget):
        if channel.id == "ch_a":
            # 模拟 Channel Attempt 逐次记账 + 最后穷尽
            outcomes.record("m", channel.id, OutcomeKind.http_5xx)
            outcomes.record("m", channel.id, OutcomeKind.http_5xx)
            outcomes.record("m", channel.id, OutcomeKind.http_5xx)
            raise ChannelAttemptExhausted(channel, causes.pop(0))
        return ({"ok": True}, channel)

    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    result, served = await dispatch([ch_a, ch_b], attempt, model="m")
    assert result == {"ok": True}
    assert served.id == "ch_b"
    # 3 次失败已记入 outcomes：冷却中（未过期）= 不健康；反向表 = 已降级
    assert outcomes.is_healthy("m", "ch_a") is False
    assert outcomes.is_degraded("m", "ch_a") is True


@pytest.mark.asyncio
async def test_yield_predicate_rejects_degraded_yields_to_next(monkeypatch):
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    outcomes.record("m", "ch_a", OutcomeKind.http_5xx)

    async def attempt(channel, wait_budget):
        return ({"ok": True, "ch": channel.id}, channel)

    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    def not_degraded(ch):
        return not outcomes.is_degraded("m", ch.id)

    result, served = await dispatch([ch_a, ch_b], attempt, model="m", yield_predicate=not_degraded)
    assert served.id == "ch_b"
    assert result["ch"] == "ch_b"


@pytest.mark.asyncio
async def test_empty_pool_raises_with_no_last_error(monkeypatch):
    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(return_value=None),
    )
    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch([_ch("ch_a")], AsyncMock(), model="m")
    assert ei.value.last_error is None


@pytest.mark.asyncio
async def test_all_excluded_raises_with_last_error(monkeypatch):
    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    causes = [RuntimeError("boom")]

    async def attempt(channel, wait_budget):
        if channel.id == "ch_a":
            raise ChannelAttemptExhausted(channel, causes.pop(0))
        raise ChannelAttemptExhausted(channel, RuntimeError("boom2"))

    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch([ch_a, ch_b], attempt, model="m")
    assert isinstance(ei.value.last_error, RuntimeError)
    assert "boom2" in str(ei.value.last_error)


@pytest.mark.asyncio
async def test_quota_window_fails_fast_with_http_status_error(monkeypatch):
    """窗口级 429 走快速失败：记 quota_window 事件 + 抛 HTTPStatusError 不进入重试。"""
    ch_a = _ch("ch_a")
    request = httpx.Request("POST", "http://ch_a")
    response = httpx.Response(429, request=request)

    async def attempt(channel, wait_budget):
        raise httpx.HTTPStatusError("quota window", request=request, response=response)

    with patch("proxy.dispatcher.quota_limits.detect_exception") as detect:
        import time
        from datetime import datetime

        from quota_limits.models import QuotaLimitInfo

        detect.return_value = QuotaLimitInfo(
            reset_at=datetime.fromtimestamp(time.time() + 3600),
            code="AccountQuotaExceeded",
            raw={"error": {"code": "AccountQuotaExceeded"}},
        )

        with pytest.raises(httpx.HTTPStatusError):
            await dispatch([ch_a], attempt, model="m")
        assert outcomes.is_blocked("ch_a") is True


@pytest.mark.asyncio
async def test_context_tried_shared_across_calls():
    """DispatchContext.tried 在多 dispatch 调用间共享，验证跨条目预算/排除语义。"""
    ctx = DispatchContext()
    ctx.tried.add("ch_a")
    assert "ch_a" in ctx.tried
    assert ctx.wait_budget >= 0


@pytest.mark.asyncio
async def test_429_within_budget_consumes_and_retries_same_channel(monkeypatch):
    """单渠道池 + 429 + 预算内：handle_rate_limit 扣预算 -> dispatcher 重选同渠道 -> 第二次成功。"""
    ch_a = _ch("ch_a")
    request = httpx.Request("POST", "http://ch_a")
    response_429 = httpx.Response(429, headers={"retry-after": "0.01"}, request=request)
    calls = {"n": 0}

    async def attempt(channel, wait_budget):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.HTTPStatusError("rate limited", request=request, response=response_429)
        return ({"ok": True}, channel)

    # 捕获 sleep 避免等待；同时 fast-forward 预算
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr("rate_limiting.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    result, served = await dispatch([ch_a], attempt, model="m")
    assert result == {"ok": True}
    assert served.id == "ch_a"
    assert calls["n"] == 2
    assert sleeps == [0.1]  # rate_limiting 最低 0.1s 下限
    # 第一次 429 预算内等待后未记失败 / 未降级
    assert outcomes.is_degraded("m", "ch_a") is False


@pytest.mark.asyncio
async def test_429_budget_exhausted_marks_rate_limit_exhausted(monkeypatch):
    """预算 0 + 429 Retry-After>0：扣预算失败 -> 记 rate_limit_exhausted -> AllChannelsExhausted。"""
    ch_a = _ch("ch_a")
    request = httpx.Request("POST", "http://ch_a")
    response_429 = httpx.Response(429, headers={"retry-after": "5"}, request=request)

    async def attempt(channel, wait_budget):
        raise httpx.HTTPStatusError("rate limited", request=request, response=response_429)

    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    ctx = DispatchContext(wait_budget=0)
    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch([ch_a], attempt, model="m", context=ctx)
    assert isinstance(ei.value.last_error, httpx.HTTPStatusError)
    # rate_limit_exhausted 已记入 outcomes（一次失败即降级）
    assert outcomes.is_degraded("m", "ch_a") is True


@pytest.mark.asyncio
async def test_non_retryable_exception_propagates(monkeypatch):
    ch_a = _ch("ch_a")

    async def attempt(channel, wait_budget):
        raise ValueError("not retriable")

    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    with pytest.raises(ValueError, match="not retriable"):
        await dispatch([ch_a], attempt, model="m")


@pytest.mark.asyncio
async def test_queue_timeout_maps_to_rate_limit_exhausted_via_context(monkeypatch):
    """发送排队超时（RateLimitExceeded(queue_timeout=True)）由 handle_rate_limit 记账后
    dispatcher 抛 AllChannelsExhausted(last_error=RateLimitExceeded)，由调用方映射。"""
    ch_a = _ch("ch_a")

    async def attempt(channel, wait_budget):
        raise RateLimitExceeded("queue timeout", waited=0.5, queue_timeout=True)

    monkeypatch.setattr(
        "balancer.load_balancer.load_balancer.select_channel",
        AsyncMock(side_effect=_select_first_available),
    )

    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch([ch_a], attempt, model="m")
    assert isinstance(ei.value.last_error, RateLimitExceeded)
    assert ei.value.last_error.queue_timeout is True


# ---------------------------------------------------------------------------
# dispatch_pinned（ADR-0021 D1）：锁定调度一等入口直测——不经 load_balancer，
# 与 dispatch 共享同一循环内核（选择→尝试→429预算→排除）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_success_returns_result_and_served_channel():
    """锁定调度正常路径：pinned 渠道过标准准入后尝试一次即返回。"""
    ch = _ch("ch_a")
    attempt = AsyncMock(return_value=({"ok": True}, ch))

    result, served = await dispatch_pinned(ch, attempt, model="m")

    assert result == {"ok": True}
    assert served.id == "ch_a"
    assert attempt.await_count == 1


@pytest.mark.asyncio
async def test_pinned_admission_reject_blocked_raises_exhausted():
    """标准档准入拒绝（窗口硬限制 blocked 优先于一切）→ AllChannelsExhausted，
    last_error 为 None（与 dispatch 空池路径一致），attempt_fn 不被调用。"""
    ch = _ch("ch_a")
    outcomes.record("m", "ch_a", OutcomeKind.quota_window, reset_at=time.time() + 3600)
    assert outcomes.is_blocked("ch_a") is True
    attempt = AsyncMock(return_value=({"ok": True}, ch))

    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch_pinned(ch, attempt, model="m")

    assert ei.value.last_error is None
    attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_admission_reject_unhealthy_raises_exhausted():
    """标准档准入拒绝（(model, channel) 不健康）→ AllChannelsExhausted，attempt_fn 不被调用。"""
    ch = _ch("ch_a")
    for _ in range(3):
        outcomes.record("m", "ch_a", OutcomeKind.http_5xx)
    assert outcomes.is_healthy("m", "ch_a") is False
    attempt = AsyncMock(return_value=({"ok": True}, ch))

    with pytest.raises(AllChannelsExhausted):
        await dispatch_pinned(ch, attempt, model="m")

    attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_admission_respects_context_tried():
    """pinned 渠道已在共享排除集（DispatchContext.tried）→ 标准档准入拒绝 → 穷尽。"""
    ch = _ch("ch_a")
    ctx = DispatchContext()
    ctx.tried.add("ch_a")
    attempt = AsyncMock(return_value=({"ok": True}, ch))

    with pytest.raises(AllChannelsExhausted):
        await dispatch_pinned(ch, attempt, model="m", context=ctx)

    attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_429_within_budget_retries_same_channel(monkeypatch):
    """429 预算内等待后重试 pinned 渠道（预算内重试同一渠道成为接口承诺）。"""
    ch = _ch("ch_a")
    request = httpx.Request("POST", "http://ch_a")
    response_429 = httpx.Response(429, headers={"retry-after": "0.01"}, request=request)
    calls = {"n": 0}

    async def attempt(channel, wait_budget):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.HTTPStatusError("rate limited", request=request, response=response_429)
        return ({"ok": True}, channel)

    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr("rate_limiting.asyncio.sleep", fake_sleep)

    result, served = await dispatch_pinned(ch, attempt, model="m")

    assert result == {"ok": True}
    assert served.id == "ch_a"
    assert calls["n"] == 2
    assert sleeps == [0.1]  # rate_limiting 最低 0.1s 下限


@pytest.mark.asyncio
async def test_pinned_exhaustion_carries_last_error_and_model_label():
    """接入点穷尽 → AllChannelsExhausted 携带 last_error；错误上下文含 model。"""
    ch = _ch("ch_a")
    cause = RuntimeError("boom")

    async def attempt(channel, wait_budget):
        raise ChannelAttemptExhausted(channel, cause)

    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch_pinned(ch, attempt, model="m")
    assert ei.value.last_error is cause
    assert "m" in str(ei.value)


@pytest.mark.asyncio
async def test_pinned_yield_predicate_rejection_counts_into_exclusions():
    """让位谓词拒绝 → 渠道计入排除集 → 下一轮 pinned 准入不过 → 穷尽（粘滞首选落回普通 LB 的机制基础）。"""
    ch = _ch("ch_a")
    outcomes.record("m", "ch_a", OutcomeKind.http_5xx)
    assert outcomes.is_degraded("m", "ch_a") is True
    ctx = DispatchContext()

    async def attempt(channel, wait_budget):
        return ({"ok": True}, channel)

    with pytest.raises(AllChannelsExhausted):
        await dispatch_pinned(
            ch,
            attempt,
            model="m",
            context=ctx,
            yield_predicate=lambda c: not outcomes.is_degraded("m", c.id),
        )

    assert "ch_a" in ctx.tried


@pytest.mark.asyncio
async def test_pinned_admission_false_skips_blocked_and_health_gates():
    """仅启用档（admission=False）：blocked / 不健康门全部跳过（硬绑定条目专用，票 03 消费）。"""
    ch = _ch("ch_a")
    for _ in range(3):
        outcomes.record("m", "ch_a", OutcomeKind.http_5xx)
    outcomes.record("m", "ch_a", OutcomeKind.quota_window, reset_at=time.time() + 3600)
    assert outcomes.is_blocked("ch_a") is True
    attempt = AsyncMock(return_value=({"ok": True}, ch))

    result, served = await dispatch_pinned(ch, attempt, model="m", admission=False)

    assert result == {"ok": True}
    assert served.id == "ch_a"


@pytest.mark.asyncio
async def test_pinned_admission_false_still_requires_enabled():
    """仅启用档仍查 enabled：未启用渠道准入不过 → 穷尽。"""
    ch = _ch("ch_a").model_copy(update={"enabled": False})
    attempt = AsyncMock(return_value=({"ok": True}, ch))

    with pytest.raises(AllChannelsExhausted):
        await dispatch_pinned(ch, attempt, model="m", admission=False)

    attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_admission_false_exhaustion_terminates_with_last_error():
    """仅启用档穷尽仍终止：排除集语义两档一致（硬绑定消费前提，票 03）。

    接入点穷尽后渠道入 tried → 不再重取 pinned 渠道 → AllChannelsExhausted
    携带 last_error（与硬绑定原独立循环 break 路径的 ctx.last_error 语义一致）。
    """
    ch = _ch("ch_a")
    cause = RuntimeError("boom")
    ctx = DispatchContext()

    async def attempt(channel, wait_budget):
        raise ChannelAttemptExhausted(channel, cause)

    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch_pinned(ch, attempt, model="m", context=ctx, admission=False)

    assert ei.value.last_error is cause
    assert "ch_a" in ctx.tried


@pytest.mark.asyncio
async def test_pinned_admission_false_429_budget_exhausted_raises_exhausted(monkeypatch):
    """仅启用档 429 预算耗尽 → 渠道入 tried → 穷尽携带 last_error（不无限重试）。"""
    ch = _ch("ch_a")
    request = httpx.Request("POST", "http://ch_a")
    response_429 = httpx.Response(429, headers={"retry-after": "5"}, request=request)

    async def attempt(channel, wait_budget):
        raise httpx.HTTPStatusError("rate limited", request=request, response=response_429)

    ctx = DispatchContext(wait_budget=0)
    with pytest.raises(AllChannelsExhausted) as ei:
        await dispatch_pinned(ch, attempt, model="m", context=ctx, admission=False)

    assert isinstance(ei.value.last_error, httpx.HTTPStatusError)
    assert "ch_a" in ctx.tried
