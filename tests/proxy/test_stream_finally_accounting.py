"""ADR-0014 D1 / 工单 03：流式 finally 记账四类行为 + 首包前失败不双记 行为测试。

只测外部行为（spec 测试决策）：在上游边界注入假 SSE 流 / 指定状态码，断言
(a) outcomes 只读视图（is_degraded / probe_targets 的 permanent 与
consecutive_failures 位）与 (b) stats 落库行（drain 后查 request_stats_raw 真实行值）。
不 patch 分类函数、记账内部或 outcomes.record——create_stream_client 是唯一
被替换的上游边界。

四类 finally 记账语义（工单 03，逐项钉住）：
- 客户端取消 → cancelled（渠道无责，不进健康/降级视图）；
- 流正常收尾 → success；
- 明确的 4xx/5xx/429 → 对应 kind（401 经 permanent 位可观察、5xx 经 probe 目标可观察）；
- 首包后上游错误事件 / 无证据的执行器自有失败（非 SSE body 解析失败）→ 兜底 transport_failure；
- 首包前失败不在流执行器记账——由接入点回退层按原始异常记一次（同一失败不双记）。
"""

import json
import sqlite3
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

import config
import request_logs
import stats
import storage
from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy import outcomes
from proxy.channel_attempt import ChannelAttemptExhausted
from proxy.stream_executor import _StreamPreflightError
from tests.proxy.endpoint_execution_test_utils import execute_single_endpoint, run_channel_attempt

pytestmark = pytest.mark.asyncio

MODEL = "gpt-4o"
CHAT_CHUNK = 'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"}}]}'


@pytest_asyncio.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    """独立临时 data 目录 + stats.db + request_logs 月度库（与 seam 测试同环境）。"""
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

    outcomes.reset()
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "stats.db"))
    await request_logs.close_backend()
    init_result = await request_logs.init_backend({"request_log_sqlite_path": str(tmp_path / "request_logs.db")})
    assert init_result["available"] is True
    yield
    outcomes.reset()
    await stats.stop_stats_workers()
    await stats.close_pool()
    await request_logs.close_backend()


def _chat_channel(channel_id="ch_1"):
    return Channel(
        id=channel_id,
        name="T",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.example.com")],
        api_key="sk-test",
        models=[MODEL],
    )


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": f"upstream {status}"}})
    return httpx.HTTPStatusError(f"upstream {status}", request=request, response=response)


class FakeStreamResponse:
    """可编程 SSE 流：按行吐数据，可选在流末尾抛指定异常。"""

    status_code = 200
    is_error = False
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines: list[str], mid_exc: Exception | None = None, status: int = 200):
        self._lines = lines
        self._mid_exc = mid_exc
        self.status_code = status
        self.is_error = status >= 400
        self.request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
        self.response = httpx.Response(status, request=self.request, json={"error": {"message": "upstream error"}})

    def raise_for_status(self):
        if self.is_error:
            raise httpx.HTTPStatusError("upstream error", request=self.request, response=self.response)

    async def aread(self):
        return await self.response.aread()

    @property
    def text(self):
        return self.response.text

    @property
    def content(self):
        return self.response.content

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._mid_exc is not None:
            raise self._mid_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeStreamClient:
    def __init__(self, lines: list[str], mid_exc: Exception | None = None, status: int = 200):
        self._lines = lines
        self._mid_exc = mid_exc
        self._status = status

    def stream(self, *args, **kwargs):
        return FakeStreamResponse(self._lines, self._mid_exc, self._status)

    async def aclose(self):
        return None


async def _make_stream(channel: Channel):
    """经 _do_request 真实前置链（转换/capability/PII/发送预算）构造流式生成器。

    上游边界由调用方 patch ``proxy.stream_executor.create_stream_client`` 注入。
    """
    return await execute_single_endpoint(
        channel,
        {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        APIType.OPENAI_CHAT,
        is_stream=True,
    )


async def _drained_stats_rows(channel_id: str) -> list[tuple]:
    """drain 异步队列后查 stats raw 真实行：(success, error_msg)。"""
    await stats.drain_queue()
    conn = sqlite3.connect(stats._DB_PATH)
    try:
        return conn.execute(
            "SELECT success, error_msg FROM request_stats_raw WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
    finally:
        conn.close()


def _probe_pair(model: str, channel_id: str):
    return [t for t in outcomes.probe_targets() if t.model == model and t.channel_id == channel_id]


# ─── 四类 finally 记账 ───


async def test_success_records_success_and_no_degradation():
    """流正常收尾 → success；渠道不进降级/探活视图。"""
    channel = _chat_channel()
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient([CHAT_CHUNK, "", "data: [DONE]"])):
        stream = await _make_stream(channel)
        outputs = "".join([chunk async for chunk in stream])

    assert "ok" in outputs
    rows = await _drained_stats_rows(channel.id)
    assert rows == [(1, None)], f"stats 落库行应为 success: {rows}"
    assert outcomes.is_degraded(MODEL, channel.id) is False
    assert _probe_pair(MODEL, channel.id) == []


async def test_client_cancelled_records_cancelled_without_failure():
    """客户端取消 → cancelled（渠道无责）：落库行 success=False 但渠道不进失败视图。"""
    channel = _chat_channel()
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient([CHAT_CHUNK, "", "data: [DONE]"])):
        stream = await _make_stream(channel)
        got = 0
        async for _ in stream:
            got += 1
            if got >= 1:
                await stream.aclose()
                break

    rows = await _drained_stats_rows(channel.id)
    assert len(rows) == 1 and rows[0][0] == 0, f"取消请求落库行 success=False: {rows}"
    assert "client_disconnected" in (rows[0][1] or "")
    # 渠道无责：cancelled 不进健康/降级视图（与 transport_failure 的对照见下一条）
    assert outcomes.is_degraded(MODEL, channel.id) is False
    assert _probe_pair(MODEL, channel.id) == []


async def test_mid_stream_5xx_records_http_5xx_exactly_once():
    """流中 5xx（已有输出后）→ http_5xx 记一次：probe 目标 consecutive_failures == 1。"""
    channel = _chat_channel()
    exc = _http_status_error(500)
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient([CHAT_CHUNK, ""], mid_exc=exc)):
        stream = await _make_stream(channel)
        _ = [chunk async for chunk in stream]

    rows = await _drained_stats_rows(channel.id)
    assert len(rows) == 1 and rows[0][0] == 0, f"stats 落库行应为失败: {rows}"
    assert outcomes.is_degraded(MODEL, channel.id) is True
    targets = _probe_pair(MODEL, channel.id)
    assert len(targets) == 1
    assert targets[0].consecutive_failures == 1, "5xx 应恰好记一次失败"
    assert targets[0].permanent is False


async def test_mid_stream_401_sets_permanent_config_kind():
    """流中 401（已有输出后）→ http_4xx_config：降级 + permanent 位（探活不再产出该对）。"""
    channel = _chat_channel()
    exc = _http_status_error(401)
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient([CHAT_CHUNK, ""], mid_exc=exc)):
        stream = await _make_stream(channel)
        _ = [chunk async for chunk in stream]

    assert outcomes.is_degraded(MODEL, channel.id) is True
    # permanent 对不产出 probe_targets（ADR-0010 D5）——"降级却无探活目标"唯一标识 401/403/404 kind
    assert _probe_pair(MODEL, channel.id) == []


async def test_upstream_error_event_records_transport_failure_fallback():
    """首包后上游错误事件 → 兜底 transport_failure 记一次（kind 由 finally 分类，非分支自定）。"""
    channel = _chat_channel()
    lines = [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"m","type":"message","role":"assistant",'
        '"content":[],"model":"claude-3","usage":{"input_tokens":1,"output_tokens":0}}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}',
        "",
        "event: error",
        'data: {"type":"error","error":{"type":"api_error","message":"midstream failure"}}',
        "",
    ]
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient(lines)):
        stream = await _make_stream(channel)
        outputs = "".join([chunk async for chunk in stream])

    assert "midstream failure" in outputs
    rows = await _drained_stats_rows(channel.id)
    assert len(rows) == 1 and rows[0][0] == 0
    assert outcomes.is_degraded(MODEL, channel.id) is True
    targets = _probe_pair(MODEL, channel.id)
    assert len(targets) == 1
    assert targets[0].consecutive_failures == 1
    assert targets[0].permanent is False, "错误事件兜底应为 transport_failure 而非 4xx_config"


async def test_non_sse_body_parse_failure_records_transport_failure_fallback():
    """非 SSE body 解析失败（执行器自有失败、无异常证据）→ finally 兜底 transport_failure。"""
    channel = _chat_channel()
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient(["[1,2,3]"])):
        stream = await _make_stream(channel)
        _ = [chunk async for chunk in stream]

    rows = await _drained_stats_rows(channel.id)
    assert len(rows) == 1 and rows[0][0] == 0
    assert outcomes.is_degraded(MODEL, channel.id) is True
    targets = _probe_pair(MODEL, channel.id)
    assert len(targets) == 1
    assert targets[0].consecutive_failures == 1
    assert targets[0].permanent is False


# ─── 首包前失败：执行器不记账，回退层记一次 ───


async def test_preflight_failure_not_recorded_by_executor():
    """首包前失败（预检包装）→ 流执行器 finally 不记账（失败证据移交回退层）。"""
    channel = _chat_channel()
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient([], status=500)):
        stream = await _make_stream(channel)
        with pytest.raises(_StreamPreflightError):
            _ = [chunk async for chunk in stream]

    assert outcomes.is_degraded(MODEL, channel.id) is False
    assert _probe_pair(MODEL, channel.id) == []


async def test_preflight_failure_recorded_once_by_fallback_layer():
    """首包前失败经预检解包由接入点回退层记一次（同一失败不双记：consecutive_failures == 1）。"""
    channel = _chat_channel(channel_id="ch_fb")
    with patch("proxy.stream_executor.create_stream_client", return_value=FakeStreamClient([], status=500)):
        with pytest.raises(ChannelAttemptExhausted):
            await run_channel_attempt(
                channel,
                {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                APIType.OPENAI_CHAT,
                True,
            )

    assert outcomes.is_degraded(MODEL, channel.id) is True
    targets = _probe_pair(MODEL, channel.id)
    assert len(targets) == 1
    # 回退层记 1 次（classify_failure(exc) → http_5xx）；若流执行器 finally 兜底重复记账则为 2
    assert targets[0].consecutive_failures == 1
    assert targets[0].permanent is False
