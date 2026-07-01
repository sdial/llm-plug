"""P1-5: proxy_errors 安全提取函数和错误构建器直接测试"""

import json
from unittest.mock import MagicMock, PropertyMock

import httpx

from routers.proxy_errors import (
    safe_httpx_response_content,
    safe_httpx_response_text,
    upstream_http_error_message,
    anthropic_error,
    anthropic_unauthorized,
    anthropic_invalid_request,
    anthropic_bad_gateway,
    anthropic_gateway_timeout,
    anthropic_response_from_exception,
    unauthorized,
    invalid_request,
    bad_gateway,
    gateway_timeout,
    response_from_proxy_exception,
)


# ─── helpers ───


def _make_mock_response(
    content: bytes = b"error body", status_code: int = 500, encoding: str = "utf-8"
):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    type(resp).content = PropertyMock(return_value=content)
    type(resp).encoding = PropertyMock(return_value=encoding)
    return resp


def _make_response_not_read():
    """模拟 ResponseNotRead 场景"""
    resp = MagicMock(spec=httpx.Response)
    type(resp).content = PropertyMock(side_effect=httpx.ResponseNotRead())
    type(resp).read = MagicMock(return_value=b"deferred content")
    type(resp).encoding = PropertyMock(return_value="utf-8")
    return resp


def _make_stream_closed():
    """模拟流已关闭场景"""
    resp = MagicMock(spec=httpx.Response)
    type(resp).content = PropertyMock(side_effect=httpx.ResponseNotRead())
    type(resp).read = MagicMock(side_effect=httpx.StreamClosed())
    type(resp).encoding = PropertyMock(return_value="utf-8")
    return resp


def _make_stream_consumed():
    """模拟流已消费场景"""
    resp = MagicMock(spec=httpx.Response)
    type(resp).content = PropertyMock(side_effect=httpx.ResponseNotRead())
    type(resp).read = MagicMock(side_effect=httpx.StreamConsumed())
    type(resp).encoding = PropertyMock(return_value="utf-8")
    return resp


# ═══════════════════════════════════════════
#  safe_httpx_response_content
# ═══════════════════════════════════════════


class TestSafeHttpxResponseContent:
    def test_normal_response(self):
        resp = _make_mock_response(b"hello")
        assert safe_httpx_response_content(resp) == b"hello"

    def test_response_not_read_falls_back_to_read(self):
        resp = _make_response_not_read()
        assert safe_httpx_response_content(resp) == b"deferred content"

    def test_stream_closed_returns_none(self):
        resp = _make_stream_closed()
        assert safe_httpx_response_content(resp) is None

    def test_stream_consumed_returns_none(self):
        resp = _make_stream_consumed()
        assert safe_httpx_response_content(resp) is None

    def test_empty_body(self):
        resp = _make_mock_response(b"")
        assert safe_httpx_response_content(resp) == b""


# ═══════════════════════════════════════════
#  safe_httpx_response_text
# ═══════════════════════════════════════════


class TestSafeHttpxResponseText:
    def test_normal_response_decoded(self):
        resp = _make_mock_response(b"hello world")
        assert safe_httpx_response_text(resp) == "hello world"

    def test_stream_closed_returns_empty_string(self):
        resp = _make_stream_closed()
        assert safe_httpx_response_text(resp) == ""

    def test_utf8_encoding(self):
        resp = _make_mock_response("你好".encode("utf-8"))
        assert safe_httpx_response_text(resp) == "你好"

    def test_invalid_utf8_replaced(self):
        resp = _make_mock_response(b"\xff\xfe", encoding="utf-8")
        result = safe_httpx_response_text(resp)
        assert "\ufffd" in result  # replacement character


# ═══════════════════════════════════════════
#  upstream_http_error_message
# ═══════════════════════════════════════════


class TestUpstreamHttpErrorMessage:
    def _make_http_status_error(self, body: str, status_code: int = 502):
        resp = _make_mock_response(body.encode("utf-8"), status_code)
        req = MagicMock(spec=httpx.Request)
        exc = httpx.HTTPStatusError("error", request=req, response=resp)
        return exc

    def test_basic_error_message(self):
        exc = self._make_http_status_error("upstream error", 502)
        result = upstream_http_error_message(exc)
        assert "502" in result
        assert "upstream error" in result

    def test_long_body_truncated(self):
        exc = self._make_http_status_error("x" * 1000, 500)
        result = upstream_http_error_message(exc)
        assert len(result) <= 820  # 800 + prefix + "..."
        assert result.endswith("...")

    def test_empty_body_fallback(self):
        exc = self._make_http_status_error("", 503)
        result = upstream_http_error_message(exc)
        assert "503" in result

    def test_short_body_not_truncated(self):
        exc = self._make_http_status_error("short", 400)
        result = upstream_http_error_message(exc)
        assert "short" in result
        assert "..." not in result


# ═══════════════════════════════════════════
#  Anthropic 格式错误构建器
# ═══════════════════════════════════════════


class TestAnthropicErrorBuilders:
    def test_anthropic_error_structure(self):
        resp = anthropic_error(400, "test_error", "test message")
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["type"] == "error"
        assert body["error"]["type"] == "test_error"
        assert body["error"]["message"] == "test message"

    def test_anthropic_unauthorized(self):
        resp = anthropic_unauthorized()
        assert resp.status_code == 401
        body = json.loads(resp.body)
        assert body["error"]["type"] == "authentication_error"

    def test_anthropic_invalid_request(self):
        resp = anthropic_invalid_request("bad input")
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["error"]["type"] == "invalid_request_error"
        assert "bad input" in body["error"]["message"]

    def test_anthropic_bad_gateway(self):
        resp = anthropic_bad_gateway("upstream down")
        assert resp.status_code == 502
        body = json.loads(resp.body)
        assert body["error"]["type"] == "api_error"

    def test_anthropic_gateway_timeout(self):
        resp = anthropic_gateway_timeout()
        assert resp.status_code == 504

    def test_anthropic_response_from_http_status_error(self):
        mock_resp = _make_mock_response(b"err", 502)
        req = MagicMock(spec=httpx.Request)
        exc = httpx.HTTPStatusError("error", request=req, response=mock_resp)
        result = anthropic_response_from_exception(exc)
        assert result.status_code == 502

    def test_anthropic_response_from_timeout(self):
        exc = httpx.ReadTimeout("timeout")
        result = anthropic_response_from_exception(exc)
        assert result.status_code == 504

    def test_anthropic_response_from_request_error(self):
        exc = httpx.ConnectError("connection refused")
        result = anthropic_response_from_exception(exc)
        assert result.status_code == 502
        body = json.loads(result.body)
        assert "网络错误" in body["error"]["message"]

    def test_anthropic_response_from_generic_exception(self):
        exc = RuntimeError("something broke")
        result = anthropic_response_from_exception(exc)
        assert result.status_code == 502


# ═══════════════════════════════════════════
#  OpenAI 格式错误构建器
# ═══════════════════════════════════════════


class TestOpenAIErrorBuilders:
    def test_unauthorized(self):
        resp = unauthorized()
        assert resp.status_code == 401
        body = json.loads(resp.body)
        assert body["error"]["code"] == "invalid_api_key"

    def test_invalid_request(self):
        resp = invalid_request("missing model")
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "missing model" in body["error"]["message"]

    def test_bad_gateway(self):
        resp = bad_gateway("upstream error")
        assert resp.status_code == 502
        body = json.loads(resp.body)
        assert body["error"]["type"] == "api_error"

    def test_gateway_timeout_default_message(self):
        resp = gateway_timeout()
        assert resp.status_code == 504
        body = json.loads(resp.body)
        assert body["error"]["code"] == "timeout"

    def test_gateway_timeout_custom_message(self):
        resp = gateway_timeout("custom timeout")
        body = json.loads(resp.body)
        assert body["error"]["message"] == "custom timeout"

    def test_response_from_http_status_error(self):
        mock_resp = _make_mock_response(b"err", 502)
        req = MagicMock(spec=httpx.Request)
        exc = httpx.HTTPStatusError("error", request=req, response=mock_resp)
        result = response_from_proxy_exception(exc)
        assert result.status_code == 502

    def test_response_from_timeout(self):
        exc = httpx.ConnectTimeout("connect timeout")
        result = response_from_proxy_exception(exc)
        assert result.status_code == 504

    def test_response_from_request_error(self):
        exc = httpx.ConnectError("refused")
        result = response_from_proxy_exception(exc)
        assert result.status_code == 502

    def test_response_from_generic(self):
        exc = ValueError("generic error")
        result = response_from_proxy_exception(exc)
        assert result.status_code == 502
        body = json.loads(result.body)
        assert "generic error" in body["error"]["message"]
