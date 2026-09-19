"""票据 02 — 接入点解析与渠道内回退（ADR-0006 核心调度行为）。

单元缝：resolve_endpoint_attempts 的顺序确定性与门控；
E2E 缝：TestClient + mock 上游的双接入点渠道回退行为。
"""

import json
import os

import httpx
import pytest
from loguru import logger

from models.api_types import APIType
from models.channel import Channel
from proxy.channel_attempt import ChannelAttemptExhausted
from tests.proxy.endpoint_execution_test_utils import run_channel_attempt

# ─── 测试数据 ───


def _ep(api_type: str, base_url: str, **kw) -> dict:
    return {"api_type": api_type, "base_url": base_url, **kw}


def _dual_channel(**kw) -> Channel:
    return Channel(
        id="ch_dual",
        name="Dual Channel",
        api_key="k",
        models=["m"],
        endpoints=[
            _ep("anthropic", "http://a.example.com"),
            _ep("openai-chat-completions", "http://c.example.com"),
        ],
        **kw,
    )


# ─── 单元：接入点解析顺序 ───


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (APIType.ANTHROPIC, ["anthropic", "openai-chat-completions", "openai-response"]),
        # 无原生匹配时按固定优先级：chat-completions > anthropic > responses
        (APIType.OPENAI_CHAT, ["openai-chat-completions", "anthropic", "openai-response"]),
        (APIType.OPENAI_RESPONSE, ["openai-response", "openai-chat-completions", "anthropic"]),
    ],
)
def test_resolve_attempts_order_is_deterministic(target, expected):
    from proxy.conversion import resolve_endpoint_attempts

    ch = Channel(
        id="ch_tri",
        name="Tri",
        api_key="k",
        models=["m"],
        endpoints=[
            _ep("openai-response", "http://r.example.com"),
            _ep("anthropic", "http://a.example.com"),
            _ep("openai-chat-completions", "http://c.example.com"),
        ],
    )

    attempts = resolve_endpoint_attempts(ch, target)

    assert [ep.api_type.value for ep in attempts] == expected


def test_resolve_attempts_skips_disabled_endpoints():
    from proxy.conversion import resolve_endpoint_attempts

    ch = _dual_channel()
    ch.endpoints[0].enabled = False

    attempts = resolve_endpoint_attempts(ch, APIType.ANTHROPIC)

    assert [ep.api_type.value for ep in attempts] == ["openai-chat-completions"]


def test_filter_channels_drops_channel_with_all_endpoints_disabled():
    """启用渠道但其接入点全部停用 → 从候选池剔除"""
    from proxy.conversion import filter_channels_by_conversion

    ch = _dual_channel()
    for ep in ch.endpoints:
        ep.enabled = False

    assert filter_channels_by_conversion([ch], APIType.ANTHROPIC) == []


def test_failure_counted_once_per_channel_request():
    """健康度是渠道级的：双接入点全失败也只 +1（逐接入点计数会加速渠道冷却）"""
    import asyncio
    import unittest.mock

    import httpx

    import proxy.channel_attempt as channel_attempt

    ch = _dual_channel()
    req = httpx.Request("POST", "http://a")
    resp = httpx.Response(500, request=req)
    recorded: list[tuple] = []

    async def failing_execute_endpoint(channel, endpoint, input, *, settings, wait_budget):
        raise httpx.HTTPStatusError("upstream 500", request=req, response=resp)

    def counting_record(model, channel_id, kind, *args, **kwargs):
        recorded.append((model, channel_id))

    with (
        unittest.mock.patch.object(channel_attempt, "execute_endpoint", failing_execute_endpoint),
        # 记账锚点迁到真实住所 outcomes（ADR-0025 D3）：断言健康键 (model, channel)
        unittest.mock.patch.object(channel_attempt.outcomes, "record", counting_record),
    ):
        with pytest.raises(ChannelAttemptExhausted) as ei:
            asyncio.run(run_channel_attempt(ch, {"model": "m"}, APIType.ANTHROPIC, False))

    assert recorded == [("m", "ch_dual")]
    assert ei.value.cause is not None


def test_failure_with_empty_body_model_records_channel_virtual_key():
    """判决节回归（ADR-0025 D1）：routing 失败路径 model/body-model 全空时经
    effective_model 以 (channel.id, channel_id) 虚拟键照记失败；虚拟键不进
    模型级探活管道（D1-A2 过滤的组合行为，01 票过滤 + 本票照记）。"""
    import asyncio
    import unittest.mock

    import httpx

    import proxy.channel_attempt as channel_attempt
    from proxy import outcomes

    ch = _dual_channel()
    req = httpx.Request("POST", "http://a")
    resp = httpx.Response(500, request=req)
    recorded: list[tuple] = []

    async def failing_execute_endpoint(channel, endpoint, input, *, settings, wait_budget):
        raise httpx.HTTPStatusError("upstream 500", request=req, response=resp)

    outcomes.reset()
    real_record = outcomes.record

    def counting_record(model, channel_id, kind, *args, **kwargs):
        recorded.append((model, channel_id, kind))
        return real_record(model, channel_id, kind, *args, **kwargs)

    try:
        with (
            unittest.mock.patch.object(channel_attempt, "execute_endpoint", failing_execute_endpoint),
            unittest.mock.patch.object(channel_attempt.outcomes, "record", counting_record),
        ):
            with pytest.raises(ChannelAttemptExhausted):
                asyncio.run(run_channel_attempt(ch, {"model": ""}, APIType.ANTHROPIC, False))

        assert recorded == [("ch_dual", "ch_dual", outcomes.OutcomeKind.http_5xx)]
        # 真实记账后（委托真 record），虚拟键降级对不产出探活候选
        assert outcomes.probe_targets() == []
    finally:
        outcomes.reset()


def test_resolve_attempts_conversion_gate():
    from proxy.conversion import resolve_endpoint_attempts

    gated = _dual_channel(allow_format_conversion=False)
    # 有原生匹配：只允许原生入口（原生失败即排除渠道，不转格式重试）
    assert [ep.api_type.value for ep in resolve_endpoint_attempts(gated, APIType.ANTHROPIC)] == ["anthropic"]
    # 目标格式恰为另一原生入口：直通无需转换，放行
    assert [ep.api_type.value for ep in resolve_endpoint_attempts(gated, APIType.OPENAI_CHAT)] == ["openai-chat-completions"]
    # 无原生匹配且禁止转换：渠道无法服务该格式
    single = Channel(
        id="ch_single",
        name="Single",
        api_key="k",
        models=["m"],
        endpoints=[_ep("anthropic", "http://a.example.com")],
        allow_format_conversion=False,
    )
    assert resolve_endpoint_attempts(single, APIType.OPENAI_CHAT) == []


# ─── E2E：渠道内回退行为 ───

_E2E_CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "_test_data", "channels.json")
_BASE = "http://127.0.0.1:19999"


def _write_e2e_channels(channels: list[dict]) -> None:
    with open(_E2E_CHANNELS_FILE, "w") as f:
        json.dump({"channels": channels}, f)
    _reset_storage_caches()


def _reset_storage_caches() -> None:
    """绕过 TTL：直改文件后同步清 storage 与渠道注册缓存（AGENTS.md 测试场景豁免）"""
    import storage
    from channel_catalog import catalog

    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None


def _dual_upstream_channels(*, first_base: str, allow_conversion: bool | None = None) -> list[dict]:
    dual = {
        "id": "ch_fb_dual",
        "name": "FB Dual",
        "api_key": "test-key",
        "models": ["claude-sonnet-4-20250514"],
        "weight": 10,
        "endpoints": [
            _ep("anthropic", f"{_BASE}{first_base}"),
            _ep("openai-chat-completions", f"{_BASE}/openai"),
        ],
    }
    if allow_conversion is not None:
        dual["allow_format_conversion"] = allow_conversion
    backup = {
        "id": "ch_fb_backup",
        "name": "FB Backup",
        "api_key": "test-key",
        "models": ["claude-sonnet-4-20250514"],
        "weight": 1,
        "endpoints": [_ep("anthropic", f"{_BASE}/anthropic")],
    }
    return [dual, backup]


def _reset_counts() -> None:
    # mock server 是独立进程：必须用真实 HTTP 客户端访问（TestClient 会拦截到自身 app）
    with httpx.Client(timeout=5) as hc:
        hc.post(f"{_BASE}/_test/reset-counts")


def _upstream_counts() -> dict:
    with httpx.Client(timeout=5) as hc:
        return hc.get(f"{_BASE}/_test/request-counts").json()


_ANTHROPIC_BODY = {
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}


def test_native_failure_falls_back_within_channel(e2e_client):
    """原生接入点 500 → 同渠道转格式重试成功，不落到其他渠道"""
    _write_e2e_channels(_dual_upstream_channels(first_base="/fail-anthropic"))
    _reset_counts()

    resp = e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY)

    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "Hello world"
    counts = _upstream_counts()
    assert counts.get("/fail-anthropic/v1/messages") == 1
    assert counts.get("/openai/v1/chat/completions") == 1
    # 备份渠道未被触碰
    assert counts.get("/anthropic/v1/messages") is None


def test_stream_preflight_failure_falls_back_within_channel(e2e_client):
    """流式：原生入口首包前失败 → 先渠道内回退而非换渠道"""
    _write_e2e_channels(_dual_upstream_channels(first_base="/fail-anthropic"))
    _reset_counts()

    resp = e2e_client.post("/v1/messages", json={**_ANTHROPIC_BODY, "stream": True})

    assert resp.status_code == 200
    assert "Hello" in resp.text
    counts = _upstream_counts()
    assert counts.get("/fail-anthropic/v1/messages") == 1
    assert counts.get("/openai/v1/chat/completions") == 1


def test_all_endpoints_fail_excludes_whole_channel(e2e_client):
    """同渠道全部接入点失败 → 渠道整体排除，下一候选渠道接管"""
    _write_e2e_channels(_dual_upstream_channels(first_base="/fail-anthropic"))
    # 把第二个接入点也改成失败路径
    with open(_E2E_CHANNELS_FILE) as f:
        data = json.load(f)
    data["channels"][0]["endpoints"][1]["base_url"] = f"{_BASE}/fail-openai"
    with open(_E2E_CHANNELS_FILE, "w") as f:
        json.dump({"channels": data["channels"]}, f)
    _reset_storage_caches()
    _reset_counts()

    resp = e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY)

    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "Hello world"
    counts = _upstream_counts()
    # 双接入点都被穷尽后才换渠道
    assert counts.get("/fail-anthropic/v1/messages") == 1
    assert counts.get("/fail-openai/v1/chat/completions") == 1
    assert counts.get("/anthropic/v1/messages") == 1


def test_conversion_disabled_no_intra_channel_retry(e2e_client):
    """allow_format_conversion=false：原生失败直接换渠道，无同渠道转格式重试"""
    _write_e2e_channels(_dual_upstream_channels(first_base="/fail-anthropic", allow_conversion=False))
    _reset_counts()

    resp = e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY)

    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "Hello world"
    counts = _upstream_counts()
    assert counts.get("/fail-anthropic/v1/messages") == 1
    # 未发生渠道内转格式重试
    assert counts.get("/openai/v1/chat/completions") is None
    assert counts.get("/anthropic/v1/messages") == 1


def test_warn_log_on_non_native_fallback(e2e_client):
    """非原生回退打 warn 日志（含渠道名、原生格式、回退格式）"""
    _write_e2e_channels(_dual_upstream_channels(first_base="/fail-anthropic"))
    _reset_counts()
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(str(msg)), level="WARNING")
    try:
        resp = e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY)
    finally:
        logger.remove(handler_id)

    assert resp.status_code == 200
    fallback_logs = [m for m in messages if "FB Dual" in m and "回退" in m]
    assert fallback_logs, f"未找到回退 warn 日志: {messages}"
    joined = "\n".join(fallback_logs)
    assert "anthropic" in joined
    assert "openai-chat-completions" in joined
