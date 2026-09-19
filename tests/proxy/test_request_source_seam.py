"""Ticket 03: 发送栈接缝 —— _do_request 的 request_source 贯通三条非流式落库路径。

生产主路径不传该参数（行为零变化，落 'client' 兜底）；外部调用方（04 的 admin_test、
ADR-0010 的 group_probe）显式传值时须同时写入统计与请求日志两侧。
"""

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

EXPLICIT_SOURCE = "admin_test"


@pytest_asyncio.fixture(autouse=True)
async def sqlite_dbs(tmp_path, monkeypatch):
    """独立临时 stats.db + request_logs 月度库：断言两侧真实落库值。"""
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "stats.db"))
    monkeypatch.setattr(config, "_settings", {})
    await request_logs.close_backend()
    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": False,
            "save_response_headers": False,
            "save_request_body": False,
            "save_response_body": False,
        },
    )
    init_result = await request_logs.init_backend({"request_log_sqlite_path": str(tmp_path / "request_logs.db")})
    assert init_result["available"] is True
    yield
    await stats.stop_stats_workers()
    await stats.close_pool()
    await request_logs.close_backend()


def _chat_channel():
    return Channel(
        id="ch_1",
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


async def _run_do_request(request_data, channel=None, request_source=None):
    """执行一次非流式请求；request_source 为 None 表示不传参（模拟生产主路径零改动调用）。"""
    kwargs = {"request_source": request_source} if request_source is not None else {}
    with patch(
        "proxy.endpoint_execution.create_client",
        new_callable=AsyncMock,
        return_value=FakeUpstreamClient(_ok_response),
    ):
        return await execute_single_endpoint(
            channel or _chat_channel(),
            request_data,
            APIType.OPENAI_CHAT,
            is_stream=False,
            **kwargs,
        )


async def _assert_logged_row(source: str, expected_success: bool) -> None:
    """drain 异步队列后断言：stats raw 行与 request_logs 月度库行均为指定来源。"""
    await stats.drain_queue()
    await request_logs.drain_queue()

    import sqlite3

    conn = sqlite3.connect(stats._DB_PATH)
    try:
        rows = conn.execute(
            "SELECT request_source, success FROM request_stats_raw WHERE channel_id = ?",
            ("ch_1",),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(source, expected_success)], f"stats raw 行不符: {rows}"

    listed = await request_logs.list_requests(page_size=10)
    assert listed["total"] >= 1
    assert all(item["request_source"] == source for item in listed["items"]), (
        f"request_logs 行不符: {[item['request_source'] for item in listed['items']]}"
    )


@pytest.mark.asyncio
async def test_success_path_explicit_source_lands_on_both_stores():
    """成功路径：显式传 request_source 时，统计与请求日志双侧落同一来源值。"""
    await _run_do_request({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}, request_source=EXPLICIT_SOURCE)
    await _assert_logged_row(EXPLICIT_SOURCE, expected_success=True)


@pytest.mark.asyncio
async def test_success_path_default_source_is_client():
    """成功路径：不传 request_source（生产主路径形态）时，双侧均为 'client'。"""
    await _run_do_request({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    await _assert_logged_row("client", expected_success=True)


@pytest.mark.asyncio
async def test_upstream_error_path_carries_explicit_source():
    """通用异常路径：上游 500 落失败记录时携带显式来源值。"""
    client = FakeUpstreamClient(lambda url: httpx.Response(500, text="boom", request=httpx.Request("POST", url)))
    request_data = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    with (
        patch("proxy.endpoint_execution.create_client", new_callable=AsyncMock, return_value=client),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await execute_single_endpoint(
            _chat_channel(),
            request_data,
            APIType.OPENAI_CHAT,
            is_stream=False,
            request_source=EXPLICIT_SOURCE,
        )
    await _assert_logged_row(EXPLICIT_SOURCE, expected_success=False)


@pytest.mark.asyncio
async def test_pii_block_path_carries_explicit_source():
    """PII 拦截路径：block 抛异常落 redacted 记录时携带显式来源值。"""
    request_data = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    with (
        patch(
            "proxy.endpoint_execution.apply_pii_filter",
            side_effect=SensitiveBlockError("blocked", triggered=["secret"]),
        ),
        pytest.raises(SensitiveBlockError),
    ):
        await execute_single_endpoint(
            _chat_channel(),
            request_data,
            APIType.OPENAI_CHAT,
            is_stream=False,
            request_source=EXPLICIT_SOURCE,
        )
    await _assert_logged_row(EXPLICIT_SOURCE, expected_success=False)


@pytest.mark.asyncio
async def test_record_request_wrapper_passes_source_to_both_backends(monkeypatch):
    """落库 helper 契约：request_source 双写统计与日志两侧，不得像 requested_model/api_type 一样被 pop。"""
    captured = {}

    def fake_stats_record(**kwargs):
        captured["stats_kwargs"] = kwargs

    def fake_logs_record(**kwargs):
        captured["logs_kwargs"] = kwargs

    monkeypatch.setattr(stats, "record_request", fake_stats_record)
    monkeypatch.setattr(request_logs, "record_request", fake_logs_record)

    from proxy.endpoint_execution import _record_request

    _record_request(
        channel_id="ch_1",
        channel_name="T",
        model="gpt-4o",
        is_stream=False,
        input_tokens=0,
        output_tokens=0,
        latency_ms=1,
        success=True,
        requested_model="gpt-4o-2024-08-06",
        api_type="openai-chat-completions",
        request_source=EXPLICIT_SOURCE,
    )

    assert captured["stats_kwargs"]["request_source"] == EXPLICIT_SOURCE
    assert captured["logs_kwargs"]["request_source"] == EXPLICIT_SOURCE
