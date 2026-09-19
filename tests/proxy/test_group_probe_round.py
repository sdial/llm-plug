"""Ticket 07 — proxy/group_probe 单轮探活驱动测试：发送路径复用 / 超时证据 / 记账 / 隔离。

只断言外部可观察行为（spec 测试决策）：dispatch 契约（锁定调度 dispatch_pinned +
零等待预算 + model=None 跳健康门，ADR-0021 D1）、成功消费到底经既有链路
``record(success)`` 解冻、超时显式 ``transport_failure``（退避推进，不被无责
cancelled 掩盖）、失败只压制该对、单目标异常隔离不拖累同轮其他目标、渠道剪枝跳过、
请求源与 request 形态（``request_source='group_probe'`` / ``requested_model``=组名 /
最小 payload）。发送路径注入走单一接缝：``proxy.dispatcher.dispatch_pinned`` +
公共入口 ``proxy.channel_attempt.attempt_channel``（ADR-0034）；真实
``_do_stream_request`` 用 Fake 流式
客户端喂 SSE + ``[DONE]``，消费到底即收尾。
"""

import asyncio

import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from models.model_group import ModelGroup
from proxy import group_probe, outcomes
from proxy.channel_attempt import ChannelAttemptExhausted, StreamAttemptResult
from proxy.errors import AllChannelsExhausted
from proxy.group_probe import ProbeCandidate, ProbeGroup, ProbeRoundResult
from proxy.outcomes import OutcomeKind

pytestmark = pytest.mark.asyncio


class FakeStreamResponse:
    """单 chunk + [DONE] 的 OpenAI Chat 流式响应（与 test_stream_request_source_seam 同形）。"""

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


def _target(model="m1", channel_id="ch_a", *, groups=None, consecutive_failures=1):
    if groups is None:
        groups = [ProbeGroup(id="g1", name="probe-grp", group=ModelGroup(id="g1", name="probe-grp"))]
    return ProbeCandidate(
        model=model,
        channel_id=channel_id,
        first_failed_at=9000.0,
        consecutive_failures=consecutive_failures,
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


async def test_success_consumed_to_done_unfreezes_via_business_chain():
    """成功判据 = 流消费到底：真实 dispatch/select/atomic 链路 + Fake 流 [DONE] 收尾。

    ``record(success)`` 由发送链既有 finally 完成——探活后降级对出视图、退避重置。
    同时验证目标渠道（已降级）经 dispatch model=None 不参与健康门禁也能被选中。
    """
    from unittest.mock import AsyncMock, patch

    ch = _ch("ch_a")
    target = _target()
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)
    assert outcomes.is_degraded("m1", "ch_a") is True

    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch])),
        patch("proxy.stream_executor.create_stream_client", return_value=FakeClient()),
    ):
        result = await group_probe.run_probe_round([target], timeout=5)

    assert result.succeeded_pairs == {("m1", "ch_a")}
    assert result.failed == ()
    assert result.skipped == ()
    assert outcomes.is_degraded("m1", "ch_a") is False
    assert outcomes.is_healthy("m1", "ch_a") is True
    assert not [t for t in outcomes.probe_targets() if t.channel_id == "ch_a"]


async def test_dispatch_contract_single_candidate_pool_zero_wait_budget():
    """dispatch_pinned 调用契约：锁定调度、DispatchContext(wait_budget=0.0)、model=None 跳健康门。

    探活 request 形态与帐：request_source='group_probe'、requested_model=组名、
    model=真实模型名，最小 payload（stream/max_tokens/messages）不含 thinking 参数。
    """
    from unittest.mock import AsyncMock, patch

    ch = _ch("ch_a")
    target = _target()
    captured: dict = {}
    seen: dict = {}

    async def fake_attempt(channel, input, *, wait_budget):
        seen["candidate"] = channel
        seen["request_data"] = input.payload
        seen["requested_model"] = input.requested_model
        seen["model"] = input.serving_model
        seen["request_source"] = input.request_source
        seen["is_stream"] = input.is_stream
        seen["target_api_type"] = input.inbound_api_type
        seen["rate_wait_budget"] = wait_budget

        async def _stream():
            yield "data: [DONE]\n\n"

        return StreamAttemptResult(_stream(), channel, channel.endpoints[0])

    async def fake_pinned(channel, attempt_fn, **kw):
        captured["channel"] = channel
        captured["kw"] = kw
        return await attempt_fn(channel, kw["context"].wait_budget)

    with (
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch])),
        patch("proxy.dispatcher.dispatch_pinned", side_effect=fake_pinned),
        patch("proxy.channel_attempt.attempt_channel", side_effect=fake_attempt),
    ):
        result = await group_probe.run_probe_round([target], timeout=5)

    assert result.succeeded_pairs == {("m1", "ch_a")}
    assert captured["channel"].id == "ch_a"
    ctx = captured["kw"]["context"]
    assert ctx.wait_budget == 0.0
    assert captured["kw"]["model"] is None  # 降级对不参与 select_channel 健康门禁
    assert seen["candidate"] is ch
    assert seen["request_source"] == "group_probe"
    assert seen["requested_model"] == "probe-grp"
    assert seen["model"] == "m1"
    assert seen["is_stream"] is True
    assert seen["target_api_type"] == APIType.OPENAI_CHAT
    assert seen["rate_wait_budget"] == 0.0
    req = seen["request_data"]
    assert req["model"] == "m1"
    assert req["stream"] is True
    assert req["max_tokens"] == 5
    assert req["messages"] == [{"role": "user", "content": "hi"}]
    assert "thinking" not in req and "reasoning" not in req


async def test_timeout_records_transport_failure_and_advances_backoff():
    """超时 → 显式 record(transport_failure)，退避推进（consecutive_failures 递增）。

    即便 dispatch 返回的流挂起（上游不回数据），单目标以 asyncio.timeout 包裹后
    不会被无责 cancelled 掩盖，结果为 transport_failure。
    """
    from unittest.mock import AsyncMock, patch

    ch = _ch("ch_a")
    target = _target(consecutive_failures=1)
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)

    async def hanging_stream():
        await asyncio.sleep(3600)
        yield b"bytes"

    async def fake_pinned(channel, attempt_fn, **kw):
        return hanging_stream(), channel

    with patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch])):
        with patch("proxy.dispatcher.dispatch_pinned", side_effect=fake_pinned):
            result = await group_probe.run_probe_round([target], timeout=0.05)

    assert len(result.failed) == 1
    assert result.failed[0].kind == OutcomeKind.transport_failure.value
    assert (result.failed[0].model, result.failed[0].channel_id) == ("m1", "ch_a")
    rows = [t for t in outcomes.probe_targets() if t.channel_id == "ch_a"]
    assert rows and rows[0].consecutive_failures == 2  # 1（初始）+ 1（显式 transport_failure）
    assert outcomes.is_degraded("m1", "ch_a") is True


async def test_timeout_aclose_hung_stream_if_releaseable():
    """超时后对可释放的挂起生成器执行 aclose（best-effort），不吞外层异常。"""
    from unittest.mock import AsyncMock, patch

    ch = _ch("ch_a")
    target = _target()
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)
    closed = {"n": 0}

    class HangStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)
            raise StopAsyncIteration

        async def aclose(self):
            closed["n"] += 1

    async def fake_pinned(channel, attempt_fn, **kw):
        return HangStream(), channel

    with patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch])):
        with patch("proxy.dispatcher.dispatch_pinned", side_effect=fake_pinned):
            result = await group_probe.run_probe_round([target], timeout=0.05)

    assert closed["n"] == 1
    assert result.failed[0].kind == OutcomeKind.transport_failure.value


async def test_one_target_exception_does_not_abort_round():
    """失败隔离：单目标异常 swallow 后继续，同轮其他目标照常探测。"""
    from unittest.mock import AsyncMock, patch

    ch_a = _ch("ch_a")
    ch_b = _ch("ch_b")
    target_ok = _target("m1", "ch_a")
    target_boom = _target("m2", "ch_b")
    outcomes.record("m2", "ch_b", OutcomeKind.http_5xx, t=9000.0)

    calls = {"n": 0}

    async def fake_pinned(channel, attempt_fn, **kw):
        calls["n"] += 1
        if channel.id == "ch_b":
            raise RuntimeError("boom")
        stream, served = await attempt_fn(channel, kw["context"].wait_budget)
        return stream, served

    async def fake_attempt(channel, input, *, wait_budget):
        async def _stream():
            yield "data: [DONE]\n\n"

        return StreamAttemptResult(_stream(), channel, channel.endpoints[0])

    with patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(side_effect=lambda m: [ch_a, ch_b])):
        with patch("proxy.dispatcher.dispatch_pinned", side_effect=fake_pinned):
            with patch("proxy.channel_attempt.attempt_channel", side_effect=fake_attempt):
                result = await group_probe.run_probe_round([target_ok, target_boom], timeout=1)

    assert calls["n"] == 2
    assert result.succeeded_pairs == {("m1", "ch_a")}
    assert [(r.model, r.channel_id, r.kind) for r in result.failed] == [("m2", "ch_b", OutcomeKind.transport_failure.value)]


async def test_missing_channel_is_pruned_not_failed():
    """渠道已删 / 禁用 / 被格式门控排除：目标进 skipped，不发送、不记账。"""
    from unittest.mock import AsyncMock, patch

    target = _target()
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)
    dispatched = {"n": 0}

    async def fake_pinned(*a, **kw):
        dispatched["n"] += 1

    with patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[])):
        with patch("proxy.dispatcher.dispatch_pinned", side_effect=fake_pinned):
            result = await group_probe.run_probe_round([target], timeout=1)

    assert dispatched["n"] == 0
    assert [s.channel_id for s in result.skipped] == ["ch_a"]
    assert result.succeeded == () and result.failed == ()


async def test_401_404_degrade_via_real_fallback_to_permanent():
    """401/403/404 经既有链路落 http_4xx_config → permanent 停探（含 multi-endpoint 回退）。"""
    from unittest.mock import AsyncMock, patch

    import httpx

    ch = _ch("ch_a")
    target = _target()
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)

    request = httpx.Request("POST", "http://ch_a")
    response = httpx.Response(404, request=request)

    async def failing_execute_endpoint(channel, endpoint, input, *, settings, wait_budget):
        raise httpx.HTTPStatusError("model not found", request=request, response=response)

    # 模拟真实 dispatch_pinned：把 ChannelAttemptExhausted 收敛为 AllChannelsExhausted(带 last_error)
    async def fake_pinned(channel, attempt_fn, **kw):
        try:
            return await attempt_fn(channel, kw["context"].wait_budget)
        except ChannelAttemptExhausted as exc:
            raise AllChannelsExhausted("channel exhausted", last_error=exc.cause) from exc.cause

    with patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch])):
        with patch("proxy.dispatcher.dispatch_pinned", side_effect=fake_pinned):
            with patch("proxy.channel_attempt.execute_endpoint", side_effect=failing_execute_endpoint):
                result = await group_probe.run_probe_round([target], timeout=1)

    assert result.failed[0].kind == OutcomeKind.http_4xx_config.value
    assert outcomes.is_degraded("m1", "ch_a") is True
    # permanent 对保持降级视图、移出探活调度（下一轮枚举不产出）
    assert outcomes.probe_targets() == []


async def test_5xx_failure_keeps_pair_degraded_only():
    """5xx 经既有链路落 http_5xx：该对保持降级并退避推进，同渠道他模型不受波及。"""
    from unittest.mock import AsyncMock, patch

    import httpx

    ch = _ch("ch_a", model="m1")
    ch2 = _ch("ch_b", model="m2")
    target = _target("m1", "ch_a")
    outcomes.record("m1", "ch_a", OutcomeKind.http_5xx, t=9000.0)

    request = httpx.Request("POST", "http://ch_a")
    response = httpx.Response(500, request=request)

    async def failing_execute_endpoint(channel, endpoint, input, *, settings, wait_budget):
        raise httpx.HTTPStatusError("down", request=request, response=response)

    # 模拟真实 dispatch_pinned：把 ChannelAttemptExhausted 收敛为 AllChannelsExhausted(带 last_error)
    async def fake_pinned(channel, attempt_fn, **kw):
        try:
            return await attempt_fn(channel, kw["context"].wait_budget)
        except ChannelAttemptExhausted as exc:
            raise AllChannelsExhausted("channel exhausted", last_error=exc.cause) from exc.cause

    with patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[ch, ch2])):
        with patch("proxy.dispatcher.dispatch_pinned", side_effect=fake_pinned):
            with patch("proxy.channel_attempt.execute_endpoint", side_effect=failing_execute_endpoint):
                result = await group_probe.run_probe_round([target], timeout=1)

    assert result.failed[0].kind == OutcomeKind.http_5xx.value
    assert outcomes.is_degraded("m1", "ch_a") is True
    assert outcomes.is_degraded("m2", "ch_b") is False  # 同渠道他模型不受波及


async def test_empty_candidates_returns_empty_result():
    result = await group_probe.run_probe_round([], timeout=1)
    assert result == ProbeRoundResult(succeeded=(), failed=(), skipped=())
