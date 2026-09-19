"""Context Shaping 非流式请求链测试。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import config
import stats
from models.api_types import APIType
from models.channel import Channel, Endpoint
from pii_errors import SensitiveBlockError
from proxy.endpoint_execution import _build_upstream_headers
from tests.proxy.endpoint_execution_test_utils import execute_single_endpoint

CTX_ON = {
    "context_shaping_strip_ansi": True,
    "context_shaping_trim_trailing_whitespace": True,
    "context_shaping_collapse_blank_lines": True,
    "context_shaping_dedupe_consecutive_lines": True,
}

TOOL_CONTENT = "line1\x1b[31mred\x1b[0m\nline1   \n\n\nline2"


@pytest_asyncio.fixture(autouse=True)
async def sqlite_stats_db(tmp_path, monkeypatch):
    """每个测试用独立临时 stats.db，记录真正落库可验证。"""
    db_path = tmp_path / "stats.db"
    monkeypatch.setattr(config, "_settings", dict(CTX_ON))
    await stats.close_pool()
    await stats.init_db(str(db_path))
    yield
    await stats.stop_stats_workers()
    await stats.close_pool()


def _chat_channel():
    return Channel(
        id="ch_1",
        name="T",
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://api.example.com")],
        api_key="sk-test",
        models=["gpt-4o"],
    )


def test_upstream_headers_do_not_forward_or_log_client_cookies():
    channel = _chat_channel()
    endpoint = channel.selected_endpoint()

    headers = _build_upstream_headers(channel, endpoint, {"Cookie": "admin_session=secret", "X-Trace": "trace"})

    assert "cookie" not in {key.lower() for key in headers}
    assert headers["X-Trace"] == "trace"


def _tool_messages():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_1", "content": TOOL_CONTENT},
    ]


class FakeClient:
    """非流式假上游客户端：捕获请求体并返回固定响应。"""

    def __init__(self, captured: dict):
        self._captured = captured

    async def post(self, url, json, headers):
        self._captured["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            request=request,
        )


async def _run_non_stream(request_data, channel=None):
    captured = {}
    channel = channel or _chat_channel()
    with patch(
        "proxy.endpoint_execution.create_client",
        new_callable=AsyncMock,
        return_value=FakeClient(captured),
    ):
        await execute_single_endpoint(channel, request_data, APIType.OPENAI_CHAT, is_stream=False)
    return captured


@pytest.mark.asyncio
async def test_non_stream_hit_writes_context_shaping_daily_stats():
    await _run_non_stream({"model": "gpt-4o", "messages": _tool_messages()})
    await stats.drain_queue()

    shaping = await stats.get_context_shaping_view(channel_id="ch_1")
    assert shaping["overall"]["request_count"] == 3
    assert shaping["overall"]["action_count"] == 3
    assert shaping["overall"]["char_change"] < 0


@pytest.mark.asyncio
async def test_non_stream_shaping_uses_copy_and_sends_shaped_payload():
    request_data = {"model": "gpt-4o", "messages": _tool_messages()}

    captured = await _run_non_stream(request_data)

    assert "\x1b[" not in captured["json"]["messages"][2]["content"]
    assert "\x1b[" in request_data["messages"][2]["content"]


@pytest.mark.asyncio
async def test_prompt_extension_modified_by_pii_is_rejected_before_send(monkeypatch):
    monkeypatch.setattr(
        config,
        "_settings",
        {
            "context_shaping_strip_ansi": True,
            "context_shaping_custom_prompt_enabled": True,
            "context_shaping_custom_prompt_text": "admin@example.com",
            "context_shaping_custom_prompt_version": 1,
        },
    )

    def mutate_prompt(payload, *_, **__):
        payload["messages"][0]["content"] = payload["messages"][0]["content"].replace("admin@example.com", "<EMAIL>")
        return payload, {"enabled": True, "action": "mask", "rules_triggered": ["EMAIL_ADDRESS"]}

    create = AsyncMock()
    with (
        patch("proxy.endpoint_execution.create_client", create),
        patch("proxy.endpoint_execution.apply_pii_filter", side_effect=mutate_prompt),
        patch("proxy.endpoint_execution._record_request"),
    ):
        with pytest.raises(SensitiveBlockError):
            await execute_single_endpoint(
                _chat_channel(),
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
                APIType.OPENAI_CHAT,
                is_stream=False,
            )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_stream_no_hit_no_context_shaping_record():
    captured_calls = []
    request_data = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    with (
        patch(
            "proxy.endpoint_execution.create_client",
            new_callable=AsyncMock,
            return_value=FakeClient({}),
        ),
        patch(
            "proxy.endpoint_execution.stats.record_context_shaping_action",
            side_effect=lambda **kw: captured_calls.append(kw),
        ),
    ):
        await execute_single_endpoint(_chat_channel(), request_data, APIType.OPENAI_CHAT, is_stream=False)

    assert captured_calls == []


@pytest.mark.asyncio
async def test_non_stream_success_empty_model_records_channel_virtual_key():
    """判决节回归（ADR-0025 D1 判决·备选 A）：non_stream 成功路径上游 body
    model="" 时不再静默丢账，以 (channel.id, channel_id) 渠道级虚拟键照记。

    组合行为（D1-A2）：虚拟键语义即「渠道级」，照记后不进模型级探活管道
    （probe_targets 对其不产出行）。"""
    from proxy import outcomes
    from proxy.outcomes import OutcomeKind

    outcomes.reset()
    try:
        recorded = []
        real_record = outcomes.record

        def spy_record(model, channel_id, kind, *args, **kwargs):
            recorded.append((model, channel_id, kind))
            return real_record(model, channel_id, kind, *args, **kwargs)

        with patch.object(outcomes, "record", spy_record):
            await _run_non_stream({"model": "", "messages": [{"role": "user", "content": "hi"}]})

        # 丢账修复可见：outcomes 收到 (channel.id, channel_id) success（旧壳为静默跳过）
        assert ("ch_1", "ch_1", OutcomeKind.success) in recorded
        # 虚拟键不进探活管道（success 本就不降级，此处钉住管道零产出不因虚拟键破坏）
        assert outcomes.probe_targets() == []
    finally:
        outcomes.reset()
