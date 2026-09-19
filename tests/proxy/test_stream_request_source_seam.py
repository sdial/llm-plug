"""Ticket 03 (group-active-probe): 流式发送链 request_source 贯通（ADR-0009 遗留缺口）。

非流式三路径已在 tests/proxy/test_request_source_seam.py 贯通；本文件覆盖流式链：
Channel Attempt → Endpoint Execution → _do_stream_request → 落库。

生产主路径不传该参数（行为零变化，落 'client' 兜底）；探活（07 的 group_probe）
显式传值时须同时写入统计与请求日志两侧。管理端来源筛选/徽标由既有测试覆盖
（tests/routers/test_admin.py::TestRequestSourceFiltering 过滤、test_admin_frontend_regressions.py
来源 checkbox + renderSourceBadge），本文件补"真实流式行"那一环。
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import config
import request_logs
import stats
import storage
from main import app
from models.api_types import APIType
from models.channel import Channel, Endpoint
from tests.admin_auth_utils import login_admin
from tests.proxy.endpoint_execution_test_utils import execute_single_endpoint, run_channel_attempt

PROBE_SOURCE = "group_probe"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    """独立临时 data 目录 + stats.db + request_logs 月度库（admin 端到端同环境）。

    与 tests/routers/test_admin.py::setup_test_db 同构：既供落库断言（stats/request_logs），
    也供 /admin/requests 的 ASGI client（DATA_DIR / whitelist / storage 缓存复位）。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"
    channels_path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    monkeypatch.setattr(config, "_settings", {})
    config._init_settings_sync()
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None

    import middleware.whitelist_middleware as wmod
    import whitelist as _whitelist_mod

    monkeypatch.setattr(
        wmod,
        "_whitelist_cache",
        _whitelist_mod.WhitelistCache(str(data_dir / "whitelist.csv")),
    )

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
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "stats.db"))
    await request_logs.close_backend()
    init_result = await request_logs.init_backend({"request_log_sqlite_path": str(tmp_path / "request_logs.db")})
    assert init_result["available"] is True
    yield
    await stats.stop_stats_workers()
    await stats.close_pool()
    await request_logs.close_backend()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await login_admin(c)
        yield c


def _chat_channel():
    return Channel(
        id="ch_1",
        name="T",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.example.com")],
        api_key="sk-test",
        models=["gpt-4o"],
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


class FakeClient:
    def stream(self, method, url, *, json=None, headers=None):
        return FakeStreamResponse()

    async def aclose(self):
        return None


async def _run_stream_request(request_data, channel=None, request_source=None):
    """执行一次流式请求并消费到底；request_source 为 None 表示不传参（生产主路径形态）。"""
    kwargs = {"request_source": request_source} if request_source is not None else {}
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeClient()):
        stream = await execute_single_endpoint(
            channel or _chat_channel(),
            request_data,
            APIType.OPENAI_CHAT,
            is_stream=True,
            **kwargs,
        )
        outputs = "".join([chunk async for chunk in stream])
    return outputs


async def _assert_logged_row(source: str, expected_success: bool) -> None:
    """drain 异步队列后断言：stats raw 行与 request_logs 月度库行均为指定来源。"""
    await stats.drain_queue()
    await request_logs.drain_queue()

    import sqlite3

    conn = sqlite3.connect(stats._DB_PATH)
    try:
        rows = conn.execute(
            "SELECT request_source, success, is_stream FROM request_stats_raw WHERE channel_id = ?",
            ("ch_1",),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(source, expected_success, True)], f"stats raw 行不符: {rows}"

    listed = await request_logs.list_requests(page_size=10)
    assert listed["total"] >= 1
    assert all(item["request_source"] == source for item in listed["items"]), (
        f"request_logs 行不符: {[item['request_source'] for item in listed['items']]}"
    )
    assert all(item["is_stream"] is True for item in listed["items"])


async def test_stream_success_explicit_source_lands_on_both_stores():
    """流式成功路径：显式传 request_source 时，统计与请求日志双侧落同一来源值。"""
    outputs = await _run_stream_request(
        {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        request_source=PROBE_SOURCE,
    )
    assert "ok" in outputs
    await _assert_logged_row(PROBE_SOURCE, expected_success=True)


async def test_stream_success_default_source_is_client():
    """流式成功路径：不传 request_source（生产主路径形态）时，双侧均为 'client'。"""
    outputs = await _run_stream_request({"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert "ok" in outputs
    await _assert_logged_row("client", expected_success=True)


async def test_channel_attempt_threads_request_source_to_endpoint_execution():
    """Channel Attempt 将默认与显式 request_source 原样传给 Endpoint Execution。"""
    channel = _chat_channel()

    with patch("proxy.channel_attempt.execute_endpoint", new_callable=AsyncMock, return_value="ok") as mock_execute:
        await run_channel_attempt(
            channel,
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            is_stream=False,
        )
    assert mock_execute.await_count == 1
    assert mock_execute.await_args.args[2].request_source == "client"

    with patch("proxy.channel_attempt.execute_endpoint", new_callable=AsyncMock, return_value="ok") as mock_execute_explicit:
        await run_channel_attempt(
            channel,
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            APIType.OPENAI_CHAT,
            is_stream=False,
            request_source=PROBE_SOURCE,
        )
    assert mock_execute_explicit.await_count == 1
    assert mock_execute_explicit.await_args.args[2].request_source == PROBE_SOURCE


async def test_streamed_group_probe_row_filterable_in_admin_requests(client):
    """端到端：真实流式探活行落库后，/admin/requests 可按 group_probe 过滤查回。

    徽标渲染由前端回归测试覆盖（test_admin_frontend_regressions.py 断言
    renderSourceBadge + requests.sourceGroupProbe），本测试补"流式行本身进库可筛"这一环。
    """
    outputs = await _run_stream_request(
        {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        request_source=PROBE_SOURCE,
    )
    assert "ok" in outputs
    await request_logs.drain_queue()

    resp = await client.get("/admin/requests", params={"request_source": PROBE_SOURCE})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    matches = [item for item in data["items"] if item["channel_id"] == "ch_1"]
    assert matches, "流式探活行应可通过 request_source=group_probe 筛出"
    assert all(item["request_source"] == PROBE_SOURCE for item in matches)
