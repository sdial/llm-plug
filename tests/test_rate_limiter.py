"""上游限速适配测试：滑动窗口限速器 + 429 重试 / 排队超时故障转移。"""

import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest

import config
from models.api_types import APIType
from models.channel import Channel, Endpoint
from models.model_group import ModelGroup
from proxy import outcomes
from proxy.endpoint_execution import _extract_response_body, _format_error_msg
from proxy.errors import AllChannelsExhausted
from proxy.model_group_dispatch import ModelGroupRequestContext, execute_model_group_request
from proxy.outcomes import OutcomeKind
from proxy.routing import _proxy_single_model_request
from rate_limiter import rate_limiter
from rate_limiting import (
    RateLimitExceeded,
    _is_rate_limit_exception,
    _parse_retry_after,
    _rate_limit_retry_after,
    acquire_send_budget,
    handle_rate_limit,
)
from tests.proxy.endpoint_execution_test_utils import endpoint_execution_side_effect, execute_single_endpoint

ARK_BODY = {
    "error": {
        "code": "AccountQuotaExceeded",
        "message": "You have exceeded the 5-hour usage quota. "
        "It will reset at 2026-08-15 19:23:44 +0800 CST. "
        "Request id: 021786790790230770d3492782dd10cc0a489fec0a72ead809994",
        "param": "",
        "type": "TooManyRequests",
    }
}


@pytest.fixture(autouse=True)
def reset_rate_limiter_and_settings(monkeypatch):
    """清空限速窗口，恢复默认等待预算。"""
    rate_limiter._windows.clear()
    monkeypatch.setattr(config, "_settings", {**config._settings, "rate_limit_wait_seconds": 30})
    yield
    rate_limiter._windows.clear()


# ─── 滑动窗口限速器 ───


class TestSlidingWindowRateLimiter:
    @pytest.mark.anyio
    async def test_allows_requests_within_limit(self):
        for _ in range(5):
            assert await rate_limiter.acquire("ch_1", 5, wait_timeout=0) is True

    @pytest.mark.anyio
    async def test_blocks_over_limit_immediately_with_zero_timeout(self):
        for _ in range(5):
            assert await rate_limiter.acquire("ch_1", 5, wait_timeout=0) is True
        assert await rate_limiter.acquire("ch_1", 5, wait_timeout=0) is False

    @pytest.mark.anyio
    async def test_waits_until_window_slides(self):
        for _ in range(2):
            assert await rate_limiter.acquire("ch_1", 2, window_seconds=0.1) is True
        start = time.monotonic()
        assert await rate_limiter.acquire("ch_1", 2, window_seconds=0.1, wait_timeout=5.0) is True
        assert time.monotonic() - start >= 0.09

    @pytest.mark.anyio
    async def test_timeout_returns_false_when_window_never_slides(self):
        assert await rate_limiter.acquire("ch_1", 1, window_seconds=60.0) is True
        # 窗口 60s，只等 0.05s 一定超时
        assert await rate_limiter.acquire("ch_1", 1, window_seconds=60.0, wait_timeout=0.05) is False

    @pytest.mark.anyio
    async def test_keys_are_independent(self):
        assert await rate_limiter.acquire("ch_a", 1, wait_timeout=0) is True
        assert await rate_limiter.acquire("ch_b", 1, wait_timeout=0) is True
        assert await rate_limiter.acquire("ch_a", 1, wait_timeout=0) is False

    @pytest.mark.anyio
    async def test_zero_or_negative_limit_passes_through(self):
        assert await rate_limiter.acquire("ch_1", 0, wait_timeout=0) is True


# ─── Retry-After 解析 ───


class TestParseRetryAfter:
    def test_seconds(self):
        assert _parse_retry_after("2") == 2.0
        assert _parse_retry_after("0.5") == 0.5

    def test_http_date(self):
        # 未来 1 小时
        future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 3600))
        parsed = _parse_retry_after(future)
        assert parsed is not None and 3500 < parsed <= 3600

    def test_invalid_returns_none(self):
        assert _parse_retry_after("abc") is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after(None) is None

    def test_seconds_whitespace_and_negative_clamped(self):
        assert _parse_retry_after(" 0.5 ") == 0.5
        assert _parse_retry_after("-3") == 0.0
        assert _parse_retry_after("0") == 0.0


# ─── 限速异常识别 ───


class TestRateLimitException:
    def test_recognizes_429_status_error(self):
        request = httpx.Request("POST", "https://upstream.example/v1/chat/completions")
        response = httpx.Response(429, request=request)
        exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
        assert _is_rate_limit_exception(exc) is True
        assert _rate_limit_retry_after(exc) is None

        response = httpx.Response(429, headers={"retry-after": "3"}, request=request)
        exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
        assert _rate_limit_retry_after(exc) == 3.0

    def test_recognizes_custom_exception(self):
        exc = RateLimitExceeded("limited", retry_after=5.0)
        assert _is_rate_limit_exception(exc) is True
        assert _rate_limit_retry_after(exc) == 5.0

    def test_other_errors_not_rate_limit(self):
        request = httpx.Request("POST", "https://upstream.example/v1/chat/completions")
        exc = httpx.HTTPStatusError(
            "server error",
            request=request,
            response=httpx.Response(500, request=request),
        )
        assert _is_rate_limit_exception(exc) is False


# ─── 代理层 429 / 排队行为 ───


def _make_channel(rpm: int | None = None, channel_id: str = "ch_nv") -> Channel:
    return Channel(
        id=channel_id,
        name="NVIDIA",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://integrate.api.nvidia.com")],
        api_key="nv-1",
        models=["nemo"],
        rate_limit_rpm=rpm,
    )


async def _select_channel_fake(channels, exclude_ids=None, **kwargs):
    for ch in channels:
        if ch.id not in (exclude_ids or set()):
            return ch
    return None


class TestProxyRateLimitRetry:
    @pytest.mark.anyio
    async def test_retries_same_channel_after_429_with_retry_after(self, monkeypatch):
        channel = _make_channel()
        state = {"calls": 0}

        async def fake_get_channels(model):
            return [channel]

        async def fake_do_request(channel, request_data, *args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                request = httpx.Request("POST", channel.selected_endpoint().base_url + "/v1/chat/completions")
                response = httpx.Response(429, headers={"retry-after": "0.01"}, request=request)
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return {"id": "ok", "model": request_data["model"]}

        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", endpoint_execution_side_effect(fake_do_request))
        record = MagicMock()
        monkeypatch.setattr("proxy.outcomes.record", record)

        result, selected = await _proxy_single_model_request(
            "nemo",
            {"model": "nemo", "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            False,
            None,
            None,
            None,
            None,
        )

        assert state["calls"] == 2
        assert result == {"id": "ok", "model": "nemo"}
        assert selected.id == "ch_nv"
        # 429 等待重试不记失败
        record.assert_not_called()

    @pytest.mark.anyio
    async def test_budget_exhausted_falls_back_to_failover(self, monkeypatch):
        channel = _make_channel()
        monkeypatch.setattr(config, "_settings", {**config._settings, "rate_limit_wait_seconds": 0})

        async def fake_get_channels(model):
            return [channel]

        async def fake_do_request(channel, request_data, *args, **kwargs):
            request = httpx.Request("POST", channel.selected_endpoint().base_url + "/v1/chat/completions")
            response = httpx.Response(429, headers={"retry-after": "5"}, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)

        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", endpoint_execution_side_effect(fake_do_request))
        record = MagicMock()
        monkeypatch.setattr("proxy.outcomes.record", record)

        with pytest.raises(httpx.HTTPStatusError):
            await _proxy_single_model_request(
                "nemo",
                {"model": "nemo", "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                False,
                None,
                None,
                None,
                None,
            )
        # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：键 (model, channel) 不变
        record.assert_called_once_with("nemo", "ch_nv", OutcomeKind.rate_limit_exhausted)

    @pytest.mark.anyio
    async def test_send_queue_timeout_turns_to_failover(self, monkeypatch):
        # 配置 RPM=1，预占唯一额度 → 请求发送前排队必然超时（预算 0）
        channel = _make_channel(rpm=1)
        monkeypatch.setattr(config, "_settings", {**config._settings, "rate_limit_wait_seconds": 0})
        await rate_limiter.acquire("ch_nv", 1, wait_timeout=0)

        async def fake_get_channels(model):
            return [channel]

        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        record = MagicMock()
        monkeypatch.setattr("proxy.outcomes.record", record)

        with pytest.raises(RateLimitExceeded):
            await _proxy_single_model_request(
                "nemo",
                {"model": "nemo", "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                False,
                None,
                None,
                None,
                None,
            )
        # �Ŷӳ�ʱӦ����Ϊ����ʧ�ܣ�����ѹ�壩��ת����ת��
        # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：键 (model, channel) 不变
        record.assert_called_once_with("nemo", "ch_nv", OutcomeKind.rate_limit_exhausted)

    @pytest.mark.anyio
    async def test_model_group_waits_on_429_then_retries(self, monkeypatch):
        """模型组 Fallback 循环同样等待 429 后重试，不立即记失败。"""
        channel = _make_channel()
        state = {"calls": 0}

        async def fake_get_channels(model):
            return [channel]

        async def fake_do_request(channel, request_data, *args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                request = httpx.Request("POST", channel.selected_endpoint().base_url + "/v1/chat/completions")
                response = httpx.Response(429, headers={"retry-after": "0.01"}, request=request)
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return {"id": "ok", "model": request_data["model"]}

        group = ModelGroup(id="grp_1", name="grp", models=["nemo"], enabled=True)
        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", endpoint_execution_side_effect(fake_do_request))
        record = MagicMock()
        monkeypatch.setattr("proxy.outcomes.record", record)

        result, selected = await execute_model_group_request(
            group,
            ModelGroupRequestContext(
                {"model": "grp", "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                False,
                None,
                None,
                None,
                None,
            ),
        )
        assert state["calls"] == 2
        assert result == {"id": "ok", "model": "nemo"}
        assert selected.id == "ch_nv"
        record.assert_not_called()

    @pytest.mark.anyio
    async def test_limited_channel_stays_healthy_after_success(self, monkeypatch):
        """NVIDIA 场景：RPM 限制下请求成功，渠道保持健康不被记失败。"""
        channel = _make_channel(rpm=1000)
        state = {"calls": 0}

        async def fake_get_channels(model):
            return [channel]

        async def fake_do_request(channel, request_data, *args, **kwargs):
            state["calls"] += 1
            return {"id": "ok"}

        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", endpoint_execution_side_effect(fake_do_request))
        record = MagicMock()
        monkeypatch.setattr("proxy.outcomes.record", record)

        result, _ = await _proxy_single_model_request(
            "nemo",
            {"model": "nemo", "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            False,
            None,
            None,
            None,
            None,
        )
        assert result == {"id": "ok"}
        record.assert_not_called()

    @pytest.mark.anyio
    async def test_model_group_shares_rate_limit_wait_budget(self, monkeypatch):
        """模型组内限速等待预算跨模型共享：预算耗尽后应直接转故障转移。"""
        channel_a = _make_channel(channel_id="ch_a")
        channel_b = _make_channel(channel_id="ch_b")
        state = {"calls_b": 0}

        async def fake_get_channels(model):
            if model == "nemo":
                return [channel_a]
            if model == "dory":
                return [channel_b]
            return []

        async def fake_do_request(channel, request_data, *args, **kwargs):
            request = httpx.Request("POST", channel.selected_endpoint().base_url + "/v1/chat/completions")
            if channel.id == "ch_a":
                # 第一个模型：Retry-After 超过总预算，立即故障转移到下一个模型
                retry_after = "1.1"
            else:
                # 第二个模型：第一次 0.5s 在预算内等待；第二次 0.6s 超过剩余预算
                calls_b = state.setdefault("calls_b", 0)
                state["calls_b"] += 1
                retry_after = "0.5" if calls_b == 0 else "0.6"
            response = httpx.Response(429, headers={"retry-after": retry_after}, request=request)
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)

        group = ModelGroup(id="grp_1", name="grp", models=["nemo", "dory"], enabled=True)
        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", endpoint_execution_side_effect(fake_do_request))
        monkeypatch.setattr(config, "_settings", {**config._settings, "rate_limit_wait_seconds": 1})
        record = MagicMock()
        monkeypatch.setattr("proxy.outcomes.record", record)
        sleep_calls: list[float] = []

        async def tracked_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        # 预算等待休眠发生在 rate_limiting 深模块内，直接打桩该模块的 sleep
        monkeypatch.setattr("rate_limiting.asyncio.sleep", tracked_sleep)

        with pytest.raises(AllChannelsExhausted):
            await execute_model_group_request(
                group,
                ModelGroupRequestContext(
                    {"model": "grp", "messages": [{"role": "user", "content": "hi"}]},
                    APIType.OPENAI_CHAT,
                    False,
                    None,
                    None,
                    None,
                    None,
                ),
            )

        # 第一个模型 Retry-After=1.1s 超过总预算 1.0s，立即故障转移；
        # 第二个模型先等 0.5s，再次 429 时剩余预算 0.5s < 0.6s，转故障转移。
        assert sleep_calls == [0.5]
        assert record.call_args_list == [
            call("nemo", "ch_a", OutcomeKind.rate_limit_exhausted),
            call("dory", "ch_b", OutcomeKind.rate_limit_exhausted),
        ]


# ─── 公开 seam 直接测试：发送前预算获取 ───


class TestAcquireSendBudget:
    @pytest.mark.anyio
    async def test_no_rpm_passes_through(self, monkeypatch):
        acquire = AsyncMock(return_value=True)
        monkeypatch.setattr("rate_limiting.rate_limiter.acquire", acquire)
        await acquire_send_budget(_make_channel(), wait_timeout=0.5)
        acquire.assert_not_awaited()

    @pytest.mark.anyio
    async def test_quota_available_acquires(self, monkeypatch):
        acquire = AsyncMock(return_value=True)
        monkeypatch.setattr("rate_limiting.rate_limiter.acquire", acquire)
        await acquire_send_budget(_make_channel(rpm=5), wait_timeout=1.0)
        acquire.assert_awaited_once_with("ch_nv", 5, window_seconds=60.0, wait_timeout=1.0)

    @pytest.mark.anyio
    async def test_queue_timeout_raises_rate_limit_exceeded(self, monkeypatch):
        acquire = AsyncMock(return_value=False)
        monkeypatch.setattr("rate_limiting.rate_limiter.acquire", acquire)
        with pytest.raises(RateLimitExceeded) as exc_info:
            await acquire_send_budget(_make_channel(rpm=1), wait_timeout=0.0)
        assert exc_info.value.queue_timeout is True
        assert exc_info.value.waited >= 0.0


# ─── 公开 seam 直接测试：限速预算决策 ───


class TestHandleRateLimit:
    @pytest.mark.anyio
    async def test_budget_enough_waits_and_keeps_channel(self, monkeypatch):
        channel = _make_channel()
        tried: set[str] = set()
        record = MagicMock()
        monkeypatch.setattr("rate_limiting.outcomes.record", record)
        sleep_calls: list[float] = []

        async def tracked_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("rate_limiting.asyncio.sleep", tracked_sleep)

        budget, did_failover = await handle_rate_limit(
            RateLimitExceeded("limited", retry_after=0.5),
            channel,
            wait_budget=1.0,
            tried_ids=tried,
        )
        assert budget == 0.5
        assert did_failover is False
        assert sleep_calls == [0.5]
        assert tried == set()
        record.assert_not_called()

    @pytest.mark.anyio
    async def test_budget_exhausted_fails_over(self, monkeypatch):
        channel = _make_channel()
        tried: set[str] = set()
        record = MagicMock()
        monkeypatch.setattr("rate_limiting.outcomes.record", record)
        sleep_calls: list[float] = []

        async def tracked_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("rate_limiting.asyncio.sleep", tracked_sleep)

        budget, did_failover = await handle_rate_limit(
            RateLimitExceeded("limited", retry_after=5.0),
            channel,
            wait_budget=1.0,
            tried_ids=tried,
            model="nemo",
        )
        assert budget == 1.0
        assert did_failover is True
        assert tried == {"ch_nv"}
        assert sleep_calls == []
        # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：键 (model, channel) 不变
        record.assert_called_once_with("nemo", "ch_nv", OutcomeKind.rate_limit_exhausted)

    @pytest.mark.anyio
    async def test_queue_timeout_fails_over_immediately(self, monkeypatch):
        channel = _make_channel()
        tried: set[str] = set()
        record = MagicMock()
        monkeypatch.setattr("rate_limiting.outcomes.record", record)
        sleep_calls: list[float] = []

        async def tracked_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("rate_limiting.asyncio.sleep", tracked_sleep)

        budget, did_failover = await handle_rate_limit(
            RateLimitExceeded("queued out", waited=0.2, queue_timeout=True),
            channel,
            wait_budget=1.0,
            tried_ids=tried,
        )
        # 排队等待时间从预算中扣除，立即转入故障转移
        assert budget == 0.8
        assert did_failover is True
        assert tried == {"ch_nv"}
        assert sleep_calls == []
        # model=None（探活等调用面）沿用旧外壳「无键跳过」语义，键语义不变（ADR-0025 D0）
        record.assert_not_called()

    @pytest.mark.anyio
    async def test_retry_after_zero_applies_floor_and_consumes_budget(self, monkeypatch):
        """Retry-After: 0 应取下限 0.1s 并扣预算，否则同渠道无限重试活锁。"""
        channel = _make_channel()
        tried: set[str] = set()
        record = MagicMock()
        monkeypatch.setattr("rate_limiting.outcomes.record", record)
        sleep_calls: list[float] = []

        async def tracked_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("rate_limiting.asyncio.sleep", tracked_sleep)

        budget, did_failover = await handle_rate_limit(
            RateLimitExceeded("limited", retry_after=0.0),
            channel,
            wait_budget=1.0,
            tried_ids=tried,
        )
        # 等待 0.1s 下限并扣减预算，保持同一渠道重试（非故障转移）
        assert budget == 0.9
        assert did_failover is False
        assert sleep_calls == [0.1]
        assert tried == set()
        record.assert_not_called()

    @pytest.mark.anyio
    async def test_retry_after_zero_header_applies_floor(self, monkeypatch):
        """上游 429 + Retry-After: 0 头同样走 0.1s 下限。"""
        channel = _make_channel()
        tried: set[str] = set()
        record = MagicMock()
        monkeypatch.setattr("rate_limiting.outcomes.record", record)
        sleep_calls: list[float] = []

        async def tracked_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("rate_limiting.asyncio.sleep", tracked_sleep)

        request = httpx.Request("POST", "https://u/v1/chat/completions")
        response = httpx.Response(429, headers={"retry-after": "0"}, request=request)
        exc = httpx.HTTPStatusError("rate limited", request=request, response=response)

        budget, did_failover = await handle_rate_limit(
            exc,
            channel,
            wait_budget=1.0,
            tried_ids=tried,
        )
        assert budget == 0.9
        assert did_failover is False
        assert sleep_calls == [0.1]
        record.assert_not_called()

    @pytest.mark.anyio
    async def test_retry_after_zero_with_no_budget_fails_over(self, monkeypatch):
        """预算为 0 时 Retry-After: 0 必须转故障转移，不能活锁。"""
        channel = _make_channel()
        tried: set[str] = set()
        record = MagicMock()
        monkeypatch.setattr("rate_limiting.outcomes.record", record)
        sleep_calls: list[float] = []

        async def tracked_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("rate_limiting.asyncio.sleep", tracked_sleep)

        budget, did_failover = await handle_rate_limit(
            RateLimitExceeded("limited", retry_after=0.0),
            channel,
            wait_budget=0.0,
            tried_ids=tried,
            model="nemo",
        )
        assert budget == 0.0
        assert did_failover is True
        assert tried == {"ch_nv"}
        assert sleep_calls == []
        record.assert_called_once_with("nemo", "ch_nv", OutcomeKind.rate_limit_exhausted)


class TestErrorBodyHelpers:
    def test_format_error_msg_from_dict(self):
        body = {"error": {"code": "AccountQuotaExceeded", "message": "reset soon"}}
        assert '"AccountQuotaExceeded"' in _format_error_msg(RateLimitExceeded("x"), body)

    def test_format_error_msg_falls_back_to_str(self):
        assert _format_error_msg(RateLimitExceeded("排队超时"), None) == "排队超时"

    def test_format_error_msg_keeps_exception_type_when_httpx_message_is_empty(self):
        assert _format_error_msg(httpx.ReadTimeout(""), None) == "ReadTimeout"

    def test_extract_response_body_json_and_text(self):
        request = httpx.Request("POST", "https://u/v1/messages")
        resp = httpx.Response(429, request=request, json={"a": 1})
        assert _extract_response_body(resp) == {"a": 1}
        resp2 = httpx.Response(500, request=request, text="boom")
        assert _extract_response_body(resp2) == "boom"


class TestRateLimitExceededCarriesBody:
    def test_429_body_attached(self):
        request = httpx.Request("POST", "https://u/v1/messages")
        resp = httpx.Response(429, request=request, json={"error": {"code": "AccountQuotaExceeded"}})
        exc = RateLimitExceeded("限速", retry_after=1.0, error_body={"a": 1}, response=resp)
        assert exc.error_body == {"a": 1}
        assert exc.response is resp


class TestEndpointExecutionRecordsErrorBody:
    @pytest.mark.anyio
    async def test_429_records_raw_error_body_in_log(self, monkeypatch):
        channel = _make_channel()
        captured: dict = {}
        monkeypatch.setattr(
            "proxy.endpoint_execution._record_request",
            lambda **kwargs: captured.update(kwargs),
        )

        class FakeClient:
            def __init__(self, response):
                self._resp = response

            async def post(self, url, json=None, headers=None):
                return self._resp

        request = httpx.Request("POST", "https://u/v1/messages")
        resp = httpx.Response(
            429,
            request=request,
            headers={"retry-after": "1"},
            json=ARK_BODY,
        )

        async def fake_create_client(channel, *, endpoint=None):
            return FakeClient(resp)

        monkeypatch.setattr(
            "proxy.endpoint_execution.create_client",
            fake_create_client,
        )
        monkeypatch.setattr("rate_limiting.rate_limiter.acquire", AsyncMock(return_value=True))

        with pytest.raises(RateLimitExceeded):
            await execute_single_endpoint(
                channel,
                {"model": "nemo", "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                False,
            )

        assert captured.get("success") is False
        assert captured.get("response_body") == ARK_BODY
        assert '"AccountQuotaExceeded"' in captured.get("error_msg", "")


def _ark_body_future_reset() -> dict:
    """窗口级限速 429 body，恢复时刻取未来（避免依赖真实时钟的硬编码样例过期）。"""
    reset_local = datetime.now().astimezone() + timedelta(hours=3)
    return {
        "error": {
            "code": "AccountQuotaExceeded",
            "message": (
                "You have exceeded the 5-hour usage quota. "
                f"It will reset at {reset_local.strftime('%Y-%m-%d %H:%M:%S %z')} CST. "
                "Request id: 021786790790230770d3492782dd10cc0a489fec0a72ead809994"
            ),
            "param": "",
            "type": "TooManyRequests",
        }
    }


class TestWindowQuotaFastFail:
    @pytest.mark.anyio
    async def test_window_quota_429_fails_fast_and_blocks(self, monkeypatch, tmp_path):
        import config as _config

        monkeypatch.setattr(_config, "DATA_DIR", str(tmp_path))
        import quota_limits

        outcomes.reset()
        quota_limits.load()
        channel = _make_channel()
        state = {"calls": 0}

        async def fake_get_channels(model):
            return [channel]

        async def fake_do_request(channel, request_data, *args, **kwargs):
            state["calls"] += 1
            request = httpx.Request("POST", channel.selected_endpoint().base_url + "/v1/messages")
            response = httpx.Response(429, request=request, json=_ark_body_future_reset())
            fake_do_request.response = response
            raise RateLimitExceeded(
                "上游限速 (429): url",
                retry_after=18000,
                error_body=_ark_body_future_reset(),
                response=response,
            )

        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", endpoint_execution_side_effect(fake_do_request))
        # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：委托真实 record（is_blocked
        # 依赖 quota_window 写穿），只按 kind 侦察
        record_kinds = []
        real_record = outcomes.record

        def spy_record(model, channel_id, kind, *args, **kwargs):
            record_kinds.append(kind)
            return real_record(model, channel_id, kind, *args, **kwargs)

        monkeypatch.setattr(outcomes, "record", spy_record)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await _proxy_single_model_request(
                "nemo",
                {"model": "nemo", "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                False,
                None,
                None,
                None,
                None,
            )

        # 只打一次上游：不重试、不故障转移
        assert state["calls"] == 1
        # 重建的 HTTPStatusError 携带原始上游响应
        assert exc_info.value.response is fake_do_request.response
        # 渠道被硬限制（内存判定由 outcomes 派生）
        assert outcomes.is_blocked("ch_nv") is True
        # 窗口级限速只记 quota_window，不记渠道失败
        assert record_kinds == [OutcomeKind.quota_window]

    @pytest.mark.anyio
    async def test_window_quota_429_in_model_group_fails_fast(self, monkeypatch, tmp_path):
        import config as _config

        monkeypatch.setattr(_config, "DATA_DIR", str(tmp_path))
        import quota_limits

        outcomes.reset()
        quota_limits.load()
        channel_a = _make_channel(channel_id="ch_a")
        channel_b = _make_channel(channel_id="ch_b")
        state = {"calls": 0}

        async def fake_get_channels(model):
            return [channel_a] if model == "nemo" else [channel_b]

        async def fake_do_request(channel, request_data, *args, **kwargs):
            state["calls"] += 1
            request = httpx.Request("POST", channel.selected_endpoint().base_url + "/v1/messages")
            response = httpx.Response(429, request=request, json=_ark_body_future_reset())
            fake_do_request.response = response
            raise RateLimitExceeded(
                "上游限速 (429): url",
                retry_after=18000,
                error_body=_ark_body_future_reset(),
                response=response,
            )

        group = ModelGroup(id="grp_1", name="grp", models=["nemo", "dory"], enabled=True)
        monkeypatch.setattr("channel_catalog.catalog.channels_for_model", fake_get_channels)
        monkeypatch.setattr("balancer.load_balancer.load_balancer.select_channel", _select_channel_fake)
        monkeypatch.setattr("proxy.channel_attempt.execute_endpoint", endpoint_execution_side_effect(fake_do_request))
        # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：委托真实 record，只按 kind 侦察
        record_kinds = []
        real_record = outcomes.record

        def spy_record(model, channel_id, kind, *args, **kwargs):
            record_kinds.append(kind)
            return real_record(model, channel_id, kind, *args, **kwargs)

        monkeypatch.setattr(outcomes, "record", spy_record)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await execute_model_group_request(
                group,
                ModelGroupRequestContext(
                    {"model": "grp", "messages": [{"role": "user", "content": "hi"}]},
                    APIType.OPENAI_CHAT,
                    False,
                    None,
                    None,
                    None,
                    None,
                ),
            )
        # 只打一次上游：不重试、不故障转移到 ch_b
        assert state["calls"] == 1
        # 重建的 HTTPStatusError 携带原始上游响应
        assert exc_info.value.response is fake_do_request.response
        assert outcomes.is_blocked("ch_a") is True
        # 窗口级限速只记 quota_window，不记渠道失败
        assert record_kinds == [OutcomeKind.quota_window]
