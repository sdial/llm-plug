"""ADR-0014 D0 — classify_failure 分类接缝契约测试。

纯函数接缝：异常对象 / 裸状态码 → ``OutcomeKind``。语义表 = 四处既有映射
（routing 回退层 / 流执行器 kind 判定段 / 组探活分类 / Responses 透传）的并集；
``RateLimitExceeded`` 刻意不进映射面——它永远在接入点回退层被重抛走限速分流，
分类函数对它按兜底档返回 ``transport_failure``（此处用测试钉住）。

只测分类契约本身（每档异常/状态码 → 期望 kind），不测任何调用方记账行为——
那由调用方路径的既有行为测试兜底。
"""

import json

import httpx
import pytest

from proxy.errors import (
    ConverterError,
    _EmptyStreamError,
    _UpstreamStreamErrorEvent,
    classify_failure,
)
from proxy.outcomes import OutcomeKind
from rate_limiting import RateLimitExceeded


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    """构造带指定状态码的 httpx.HTTPStatusError（raise_for_status 的异常形态）。"""
    request = httpx.Request("POST", "https://upstream.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"upstream {status_code}", request=request, response=response)


# ═══════════════════════════════════════════
#  HTTPStatusError 档：401/403/404 / 5xx / 429 / 词汇表外状态
# ═══════════════════════════════════════════


class TestHTTPStatusErrorTiers:
    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_config_codes_map_to_http_4xx_config(self, code):
        assert classify_failure(_status_error(code)) is OutcomeKind.http_4xx_config

    @pytest.mark.parametrize("code", [500, 502, 503, 599])
    def test_5xx_maps_to_http_5xx(self, code):
        assert classify_failure(_status_error(code)) is OutcomeKind.http_5xx

    def test_429_maps_to_http_429(self):
        assert classify_failure(_status_error(429)) is OutcomeKind.http_429

    @pytest.mark.parametrize("code", [400, 409, 418, 422])
    def test_other_4xx_falls_back_to_transport_failure(self, code):
        # 词汇表外状态归兜底档：探活分类器自造标签 http_{code} 的归一去向
        # （spec 用户故事 7：结果只产出 OutcomeKind 词汇表内的值）
        assert classify_failure(_status_error(code)) is OutcomeKind.transport_failure


# ═══════════════════════════════════════════
#  流执行异常档：空流 / 上游错误事件 → transport_failure
# ═══════════════════════════════════════════


class TestStreamExceptionTiers:
    def test_empty_stream_error(self):
        assert classify_failure(_EmptyStreamError("上游流式响应为空")) is OutcomeKind.transport_failure

    def test_upstream_stream_error_event(self):
        exc = _UpstreamStreamErrorEvent({"error": {"message": "upstream blew up"}})
        assert exc.event == {"error": {"message": "upstream blew up"}}
        assert classify_failure(exc) is OutcomeKind.transport_failure


# ═══════════════════════════════════════════
#  兜底档：转换 / 超时 / 传输 / JSON 解码 / 非 HTTP 异常 → transport_failure
# ═══════════════════════════════════════════


class TestFallbackTier:
    @pytest.mark.parametrize(
        "exc",
        [
            ConverterError("bad payload"),
            httpx.TimeoutException("timed out"),
            httpx.ConnectError("connection refused"),
            json.JSONDecodeError("expecting value", "{", 0),
            RuntimeError("non-http failure"),
        ],
    )
    def test_misc_exceptions_fall_back_to_transport_failure(self, exc):
        assert classify_failure(exc) is OutcomeKind.transport_failure

    def test_no_exc_no_status_falls_back(self):
        assert classify_failure() is OutcomeKind.transport_failure


# ═══════════════════════════════════════════
#  裸 status 档：无异常对象的透传记账路径，命中档位才生效
# ═══════════════════════════════════════════


class TestBareStatusTiers:
    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_config_codes(self, code):
        assert classify_failure(None, code) is OutcomeKind.http_4xx_config

    @pytest.mark.parametrize("code", [500, 502, 599])
    def test_5xx(self, code):
        assert classify_failure(None, code) is OutcomeKind.http_5xx

    def test_429(self):
        assert classify_failure(None, 429) is OutcomeKind.http_429

    @pytest.mark.parametrize("code", [400, 418])
    def test_off_ladder_status_falls_back(self, code):
        assert classify_failure(None, code) is OutcomeKind.transport_failure

    def test_status_only_via_keyword(self):
        assert classify_failure(status=429) is OutcomeKind.http_429

    def test_exc_wins_over_status(self):
        # status 仅在无异常对象时生效：给了异常对象就以异常携带的状态为准
        assert classify_failure(_status_error(500), status=200) is OutcomeKind.http_5xx


# ═══════════════════════════════════════════
#  RateLimitExceeded 不进映射面（钉住）
# ═══════════════════════════════════════════


class TestRateLimitExcludedFromMapping:
    """RateLimitExceeded 永远在接入点回退层被重抛走限速分流，不落 kind 映射。"""

    def test_rate_limit_exceeded_gets_no_special_kind(self):
        exc = RateLimitExceeded("queue wait timeout", retry_after=1.0, queue_timeout=True)
        kind = classify_failure(exc)
        assert kind is OutcomeKind.transport_failure
        assert kind is not OutcomeKind.http_429
        assert kind is not OutcomeKind.rate_limit_exhausted

    def test_rate_limit_exceeded_carrying_429_response_still_not_429_kind(self):
        # 即便携带 429 response（上游瞬时限速包装形态），也只认 HTTPStatusError 类型，
        # 不做鸭子类型嗅探——限速分流由调用方重抛层负责，与分类无关
        request = httpx.Request("POST", "https://upstream.test/v1/chat/completions")
        exc = RateLimitExceeded("rate limited", response=httpx.Response(429, request=request))
        assert classify_failure(exc) is OutcomeKind.transport_failure
