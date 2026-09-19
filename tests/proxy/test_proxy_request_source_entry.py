"""ADR-0019 D0：``proxy_request`` 公共入口的 request_source 贯通归类断言。

请求来源归类（ADR-0009）在公共入口即完整：入口签名补 ``request_source``（默认
``"client"``）并向下贯通 单模型 / 模型组路径 → attempt → 执行器。沿既有
request_source 归类测试的前例（tests/proxy/test_request_source_seam.py 落库断言），
本文件走真实调度链（dispatch / attempt / ``_do_request``，仅注入假上游客户端）
断言双侧落库来源值——请求行为零变化，仅记账归类随入参走。
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
from models.model_group import ModelGroup
from proxy.routing import proxy_request

EXPLICIT_SOURCE = "admin_test"
PROBE_SOURCE = "group_probe"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def sqlite_dbs(tmp_path, monkeypatch):
    """独立临时 stats.db + request_logs 月度库：断言两侧真实落库值（与前例同构）。"""
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


def _group(items):
    return ModelGroup(id="grp_1", name="grp_1", items=items, enabled=True)


class FakeUpstreamClient:
    """非流式假上游客户端：恒返成功 OpenAI Chat 响应。"""

    async def post(self, url, json, headers):
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


_REQUEST_DATA = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}


async def _run_via_proxy_request(request_data, *, request_source=None, group=None):
    """经公共入口跑一次真实单模型/模型组请求；request_source 为 None 表示不传参（默认形态）。"""
    kwargs = {"request_source": request_source} if request_source is not None else {}
    with (
        patch("channel_catalog.catalog.get_model_group_by_name", new=AsyncMock(return_value=group)),
        patch("channel_catalog.catalog.channels_for_model", new=AsyncMock(return_value=[_chat_channel()])),
        patch("proxy.endpoint_execution.create_client", new_callable=AsyncMock, return_value=FakeUpstreamClient()),
    ):
        return await proxy_request("gpt-4o", request_data, APIType.OPENAI_CHAT, is_stream=False, **kwargs)


async def _assert_logged_row(source: str) -> None:
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
    assert rows == [(source, True)], f"stats raw 行不符: {rows}"

    listed = await request_logs.list_requests(page_size=10)
    assert listed["total"] >= 1
    assert all(item["request_source"] == source for item in listed["items"]), (
        f"request_logs 行不符: {[item['request_source'] for item in listed['items']]}"
    )


async def test_single_model_explicit_source_lands_on_both_stores():
    """单模型路径：入口显式传 request_source 时，统计与请求日志双侧落同一来源值。"""
    result, served = await _run_via_proxy_request(_REQUEST_DATA, request_source=EXPLICIT_SOURCE)
    assert served.id == "ch_1"
    assert result["choices"][0]["message"]["content"] == "ok"
    await _assert_logged_row(EXPLICIT_SOURCE)


async def test_single_model_default_source_is_client():
    """单模型路径：入口不传 request_source（既有调用形态）时，双侧均为 'client'。"""
    await _run_via_proxy_request(_REQUEST_DATA)
    await _assert_logged_row("client")


async def test_model_group_pure_entry_explicit_source_lands_on_both_stores():
    """模型组纯模型条目路径：入口 request_source 贯通组调度 → attempt → 执行器。"""
    group = _group([{"model": "gpt-4o"}])
    result, served = await _run_via_proxy_request(_REQUEST_DATA, request_source=PROBE_SOURCE, group=group)
    assert served.id == "ch_1"
    assert result["choices"][0]["message"]["content"] == "ok"
    await _assert_logged_row(PROBE_SOURCE)


async def test_model_group_hard_bound_entry_explicit_source_lands_on_both_stores():
    """模型组硬绑定条目路径：入口 request_source 同样贯通直连单渠道循环。"""
    group = _group([{"model": "gpt-4o", "channel_id": "ch_1"}])
    result, served = await _run_via_proxy_request(_REQUEST_DATA, request_source=PROBE_SOURCE, group=group)
    assert served.id == "ch_1"
    assert result["choices"][0]["message"]["content"] == "ok"
    await _assert_logged_row(PROBE_SOURCE)
