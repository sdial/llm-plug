"""Ticket 05（ADR-0014 D2）：落库组装 helper 收敛后的行级 seam 回归。

四个落库调用点（非流式 PII 拦截 / 失败 / 成功 + 流式 finally）经同一组装函数
（proxy.request_record.record_request）出账。沿用 prior art seam 形态：drain
队列后断言 stats raw 表与 request_logs 月度库的真实行值——

- request_source 在 stats 与 request_logs 双侧落同一值（ADR-0009 约定不回归）；
- requested_model / api_type / sensitivity_info 仅日志侧，四条路径全部携带
  （字段形状一致，任一路径不再漏字段）；
- 敏感头（authorization / x-api-key）在日志侧脱敏（全仓唯一脱敏住所的行为面）。
"""

import sqlite3
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import config
import request_logs
import stats
from models.api_types import APIType
from models.channel import Channel, Endpoint
from pii_errors import SensitiveBlockError
from tests.proxy.endpoint_execution_test_utils import execute_single_endpoint

SOURCE = "admin_test"
PROBE_SOURCE = "group_probe"
REQUESTED_MODEL = "gpt-4o-2024-08-06"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def sqlite_dbs(tmp_path, monkeypatch):
    """独立临时 stats.db + request_logs 月度库；raw 保存开关全开以断言脱敏后的原文落库。"""
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "stats.db"))
    monkeypatch.setattr(config, "_settings", {})
    await request_logs.close_backend()
    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": True,
            "save_response_headers": True,
            "save_request_body": True,
            "save_response_body": True,
        },
    )
    init_result = await request_logs.init_backend({"request_log_sqlite_path": str(tmp_path / "request_logs.db")})
    assert init_result["available"] is True
    yield
    await stats.stop_stats_workers()
    await stats.close_pool()
    await request_logs.close_backend()


def _chat_channel(channel_id: str = "ch_1") -> Channel:
    return Channel(
        id=channel_id,
        name="T",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.example.com")],
        api_key="sk-test",
        models=["gpt-4o"],
    )


class FakeUpstreamClient:
    """非流式假上游客户端：post 行为由注入的工厂决定（成功响应 / 500 报错）。"""

    def __init__(self, response_factory):
        self._response_factory = response_factory

    async def post(self, url, json, headers):
        return self._response_factory(url)


def _ok_response(url):
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        request=httpx.Request("POST", url),
    )


class FakeStreamResponse:
    """单 chunk + [DONE] 的 OpenAI Chat 流式响应。"""

    status_code = 200
    is_error = False
    headers = {"content-type": "text/event-stream"}

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        yield 'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"}}]}'
        yield ""
        yield "data: [DONE]"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeStreamClient:
    def stream(self, method, url, *, json=None, headers=None):
        return FakeStreamResponse()

    async def aclose(self):
        return None


async def _drain_and_fetch(channel_id: str):
    """drain 双队列后返回 (stats raw 行, request_logs 月度库行)。"""
    await stats.drain_queue()
    await request_logs.drain_queue()

    conn = sqlite3.connect(stats._DB_PATH)
    try:
        stat_rows = conn.execute(
            "SELECT request_source, success, is_stream FROM request_stats_raw WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
    finally:
        conn.close()

    listed = await request_logs.list_requests(page_size=10)
    items = [item for item in listed["items"] if item["channel_id"] == channel_id]
    assert len(items) == 1, f"channel={channel_id} 应恰好一条日志行: {items}"
    return stat_rows, items[0]


async def _get_raw_field(request_id: int, field: str):
    result = await request_logs.get_request_field(request_id, field)
    assert result is not None, f"raw 字段 {field} 未落库"
    return result["data"]


def _assert_no_sensitive_headers(headers: dict) -> None:
    """敏感头脱敏唯一住所的行为面：凭证头不得落请求日志。"""
    lowered = {k.lower() for k in headers}
    assert "authorization" not in lowered
    assert "x-api-key" not in lowered


async def test_success_path_lands_consistent_shape_on_both_stores():
    """成功路径：双侧同来源；requested_model/api_type 仅日志侧；敏感头脱敏后原文照常落库。"""
    with patch(
        "proxy.endpoint_execution.create_client",
        new_callable=AsyncMock,
        return_value=FakeUpstreamClient(_ok_response),
    ):
        await execute_single_endpoint(
            _chat_channel("ch_success"),
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            is_stream=False,
            requested_model=REQUESTED_MODEL,
            request_source=SOURCE,
        )

    stat_rows, log_item = await _drain_and_fetch("ch_success")
    assert stat_rows == [(SOURCE, 1, 0)]
    assert log_item["request_source"] == SOURCE == stat_rows[0][0]
    assert log_item["requested_model"] == REQUESTED_MODEL
    assert log_item["api_type"] == "openai-chat-completions"
    assert log_item["success"] is True
    assert log_item["is_stream"] is False
    assert log_item["finish_reason"] == "stop"
    assert log_item["sensitivity_info"] is None

    headers = await _get_raw_field(log_item["id"], "request_headers")
    _assert_no_sensitive_headers(headers)
    assert headers  # 其余上游头（Content-Type 等）照常保存
    assert (await _get_raw_field(log_item["id"], "request_body"))["model"] == "gpt-4o"
    assert (await _get_raw_field(log_item["id"], "response_body"))["id"] == "chatcmpl-1"


async def test_upstream_error_path_lands_consistent_shape_on_both_stores():
    """失败路径：与成功行同形状（仅日志侧维度贯通、敏感头脱敏），失败字段如实落库。"""
    client = FakeUpstreamClient(lambda url: httpx.Response(500, text="boom", request=httpx.Request("POST", url)))
    with (
        patch("proxy.endpoint_execution.create_client", new_callable=AsyncMock, return_value=client),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await execute_single_endpoint(
            _chat_channel("ch_failure"),
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            is_stream=False,
            requested_model=REQUESTED_MODEL,
            request_source=SOURCE,
        )

    stat_rows, log_item = await _drain_and_fetch("ch_failure")
    assert stat_rows == [(SOURCE, 0, 0)]
    assert log_item["request_source"] == SOURCE
    assert log_item["requested_model"] == REQUESTED_MODEL
    assert log_item["api_type"] == "openai-chat-completions"
    assert log_item["success"] is False
    assert log_item["error_msg"] == "boom"
    assert log_item["sensitivity_info"] is None

    headers = await _get_raw_field(log_item["id"], "request_headers")
    _assert_no_sensitive_headers(headers)
    assert (await _get_raw_field(log_item["id"], "request_body"))["model"] == "gpt-4o"
    assert (await _get_raw_field(log_item["id"], "response_body")) == "boom"


async def test_pii_block_path_lands_consistent_shape_on_both_stores():
    """PII 拦截路径：redacted body + sensitivity_info 仅日志侧落库，来源与仅日志侧维度不缺。"""
    with (
        patch(
            "proxy.endpoint_execution.apply_pii_filter",
            side_effect=SensitiveBlockError("blocked", triggered=["secret"]),
        ),
        pytest.raises(SensitiveBlockError),
    ):
        await execute_single_endpoint(
            _chat_channel("ch_pii"),
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            is_stream=False,
            requested_model=REQUESTED_MODEL,
            request_source=SOURCE,
        )

    stat_rows, log_item = await _drain_and_fetch("ch_pii")
    assert stat_rows == [(SOURCE, 0, 0)]
    assert log_item["request_source"] == SOURCE
    assert log_item["requested_model"] == REQUESTED_MODEL
    assert log_item["api_type"] == "openai-chat-completions"
    assert log_item["success"] is False
    assert "blocked" in log_item["error_msg"]
    assert log_item["sensitivity_info"]["action"] == "block"
    assert log_item["sensitivity_info"]["rules_triggered"] == ["secret"]

    headers = await _get_raw_field(log_item["id"], "request_headers")
    _assert_no_sensitive_headers(headers)
    assert (await _get_raw_field(log_item["id"], "request_body")) == {"_redacted": True, "reason": "pii_block"}
    assert (await _get_raw_field(log_item["id"], "response_body")) is None


async def test_stream_finally_path_lands_consistent_shape_on_both_stores():
    """流式 finally 路径：与非流式共用同一组装骨架，双侧同来源、仅日志侧维度贯通、敏感头脱敏。"""
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient()):
        stream = await execute_single_endpoint(
            _chat_channel("ch_stream"),
            {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            is_stream=True,
            requested_model=REQUESTED_MODEL,
            request_source=PROBE_SOURCE,
        )
        outputs = "".join([chunk async for chunk in stream])
    assert "ok" in outputs

    stat_rows, log_item = await _drain_and_fetch("ch_stream")
    assert stat_rows == [(PROBE_SOURCE, 1, 1)]
    assert log_item["request_source"] == PROBE_SOURCE == stat_rows[0][0]
    assert log_item["requested_model"] == REQUESTED_MODEL
    assert log_item["api_type"] == "openai-chat-completions"
    assert log_item["success"] is True
    assert log_item["is_stream"] is True

    headers = await _get_raw_field(log_item["id"], "request_headers")
    _assert_no_sensitive_headers(headers)
    assert (await _get_raw_field(log_item["id"], "request_body"))["model"] == "gpt-4o"
