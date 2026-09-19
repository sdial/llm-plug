"""窗口级限速（额度耗尽型 429）检测与硬限制测试。"""

from datetime import UTC, datetime

import httpx
import pytest

import config
from balancer.load_balancer import LoadBalancer
from models.api_types import APIType
from models.channel import Channel, Endpoint
from quota_limits import detect_body, detect_exception
from quota_limits.store import QuotaLimitStore
from rate_limiting import RateLimitExceeded

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


def _ark_response() -> httpx.Response:
    request = httpx.Request("POST", "https://upstream.example/v1/messages")
    return httpx.Response(429, request=request, json=ARK_BODY)


def _utc_future(seconds: float = 3600):
    import time

    return datetime.fromtimestamp(time.time() + seconds, tz=UTC)


class TestQuotaLimitStore:
    def test_mark_and_check_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
        store = QuotaLimitStore()
        reset_at = _utc_future()
        store.mark_blocked("ch_1", reset_at, "AccountQuotaExceeded")
        assert store.has_active_window("ch_1") is True
        store.unblock("ch_1")
        assert store.has_active_window("ch_1") is False

    def test_expired_not_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
        store = QuotaLimitStore()
        store.mark_blocked("ch_1", datetime(2020, 1, 1, tzinfo=UTC), "x")
        assert store.has_active_window("ch_1") is False

    def test_round_trip_persistence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
        store = QuotaLimitStore()
        reset_at = _utc_future()
        store.mark_blocked("ch_1", reset_at, "AccountQuotaExceeded")
        reloaded = QuotaLimitStore()
        reloaded.load()
        assert reloaded.has_active_window("ch_1") is True

    def test_cleanup_removes_inactive_and_expired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
        store = QuotaLimitStore()
        store.mark_blocked("ch_keep", _utc_future(), "AccountQuotaExceeded")
        store.mark_blocked("ch_gone", _utc_future(), "AccountQuotaExceeded")
        store.mark_blocked("ch_old", datetime(2020, 1, 1, tzinfo=UTC), "x")
        store.cleanup(active_channel_ids={"ch_keep"})
        assert store.has_active_window("ch_keep") is True
        assert store.has_active_window("ch_gone") is False
        assert store.has_active_window("ch_old") is False

    def test_naive_reset_at_normalized_to_utc(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
        store = QuotaLimitStore()
        store.mark_blocked("ch_1", datetime(2030, 1, 1), "x")
        assert store.has_active_window("ch_1") is True


class TestArkAdapter:
    def test_matches_account_quota_exceeded(self):
        info = detect_body(ARK_BODY)
        assert info is not None
        assert info.code == "AccountQuotaExceeded"
        assert info.reset_at == datetime(2026, 8, 15, 11, 23, 44, tzinfo=UTC)

    def test_no_reset_time_still_detected(self):
        body = {
            "error": {
                "code": "AccountQuotaExceeded",
                "message": "You have exceeded the weekly usage quota.",
            }
        }
        info = detect_body(body)
        assert info is not None
        assert info.reset_at is None

    def test_ignores_transient_429(self):
        body = {"error": {"message": "rate limited", "type": "rate_limit_error"}}
        assert detect_body(body) is None

    def test_ignores_non_dict(self):
        assert detect_body("not a dict") is None
        assert detect_body(None) is None


class TestDetectFromException:
    def test_from_rate_limit_exceeded(self):
        # error_body 字段在 Task 4 才加入构造器，此处先用 setattr 注入
        exc = RateLimitExceeded("限速", retry_after=1.0)
        exc.error_body = ARK_BODY
        info = detect_exception(exc)
        assert info is not None and info.code == "AccountQuotaExceeded"

    def test_from_http_status_error(self):
        exc = httpx.HTTPStatusError("limited", request=_ark_response().request, response=_ark_response())
        info = detect_exception(exc)
        assert info is not None and info.code == "AccountQuotaExceeded"

    def test_transient_http_status_error_returns_none(self):
        request = httpx.Request("POST", "https://upstream.example/v1/messages")
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
        )
        exc = httpx.HTTPStatusError("limited", request=request, response=response)
        assert detect_exception(exc) is None

    def test_queue_timeout_returns_none(self):
        exc = RateLimitExceeded("排队超时", waited=0.2, queue_timeout=True)
        assert detect_exception(exc) is None


def _lb_channel(channel_id: str) -> Channel:
    return Channel(
        id=channel_id,
        name=channel_id,
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://upstream.example")],
        api_key="k",
        models=["m"],
        enabled=True,
    )


class TestLoadBalancerBlockedChecker:
    """选路时 ``is_blocked`` 优先于一切：quota 窗口渠道被先排除。

    旧的 ``set_blocked_checker`` 桥已删除，阻塞判定由 outcomes 的
    ``is_blocked`` 视图驱动（ADR-0008 D0）。
    """

    @pytest.fixture(autouse=True)
    def _reset_outcomes(self):
        from proxy import outcomes

        outcomes.reset()
        yield
        outcomes.reset()

    @pytest.mark.anyio
    async def test_skips_blocked_channel(self):
        from proxy import outcomes

        lb = LoadBalancer()
        outcomes.record(
            "m",
            "ch_blocked",
            outcomes.OutcomeKind.quota_window,
            reset_at=_utc_future().timestamp(),
        )
        channels = [_lb_channel("ch_blocked"), _lb_channel("ch_ok")]
        selected = await lb.select_channel(channels, model="m")
        assert selected is not None and selected.id == "ch_ok"

    @pytest.mark.anyio
    async def test_all_blocked_returns_none(self):
        from proxy import outcomes

        lb = LoadBalancer()
        outcomes.record(
            "m",
            "ch_1",
            outcomes.OutcomeKind.quota_window,
            reset_at=_utc_future().timestamp(),
        )
        assert await lb.select_channel([_lb_channel("ch_1")], model="m") is None

    @pytest.mark.anyio
    async def test_no_checker_selects_normally(self):
        lb = LoadBalancer()
        channels = [_lb_channel("ch_1"), _lb_channel("ch_2")]
        selected = await lb.select_channel(channels, model="m")
        assert selected is not None and selected.id in ("ch_1", "ch_2")


class TestSetupWiring:
    def test_setup_wires_quota_write_through(self, monkeypatch, tmp_path):
        """setup 后 quota_window 事件触发 JSON 写穿，重启后可重载。"""
        import config as _config

        monkeypatch.setattr(_config, "DATA_DIR", str(tmp_path))
        import quota_limits
        from proxy import outcomes

        quota_limits.load()
        quota_limits.setup()
        try:
            reset_ts = _utc_future().timestamp()
            outcomes.record("m", "ch_write", outcomes.OutcomeKind.quota_window, reset_at=reset_ts)
            # 内存视图可见
            assert outcomes.is_blocked("ch_write") is True
            # 写穿落盘
            import os

            path = os.path.join(str(tmp_path), "channel_quota_limits.json")
            assert os.path.exists(path)
            import json

            raw = json.load(open(path, encoding="utf-8"))
            assert "ch_write" in raw
            # 重启重载：新进程视图从文件恢复
            outcomes.reset()
            outcomes.load_quota_limits(path)
            assert outcomes.is_blocked("ch_write") is True
        finally:
            from proxy.outcomes import set_quota_adapter

            set_quota_adapter(None)
