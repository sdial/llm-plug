"""REVIEW P0 端到端回归：H4 流式 [DONE] 终止行与 H1 会话状态。

- Chat 客户端 + Anthropic 上游：流必须以 `data: [DONE]` 结束（此前缺失）。
- Responses 客户端 + Anthropic 上游：completed 带 output，previous_response_id
  多轮会话不丢助手回复（依赖 completed.response.output 保存状态）。
"""

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_shared_proxy_state():
    """清除进程级共享状态，避免被同会话更早的测试污染。

    - test_proxy_routing.py 等编排测试可能覆写共享的
      tests/_test_data/channels.json 且不恢复，而 conftest 的
      _setup_e2e_channels 只在 session 级 mock server fixture 创建时执行一次，
      全量套件中途文件已被换掉——这里每次重写回标准 e2e 渠道。
    - load_balancer 的失败计数/冷却与 quota 窗口封禁是长寿命单例状态，一并清零。
    """
    from proxy import outcomes

    outcomes.reset()
    from tests.conftest import _setup_e2e_channels

    _setup_e2e_channels()
    yield


class TestH4ChatClientAnthropicUpstreamStream:
    def test_stream_ends_with_done(self, e2e_client):
        with e2e_client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                body = resp.read()
                pytest.fail(f"stream 请求失败: {resp.status_code} {body!r}")
            lines = [line for line in resp.iter_lines() if line.strip()]
        assert lines, "流不能为空"
        done_lines = [line for line in lines if line.strip() == "data: [DONE]"]
        assert done_lines, "Anthropic→Chat 流式必须以 data: [DONE] 结束"
        # 终止行必须是最后一帧
        assert lines[-1].strip() == "data: [DONE]"


class TestH1ResponsesClientAnthropicUpstreamStream:
    def test_completed_has_output_and_state_saved(self, e2e_client):
        with e2e_client.stream(
            "POST",
            "/v1/responses",
            json={
                "model": "claude-sonnet-4-20250514",
                "input": "Hello",
                "stream": True,
                "store": True,
            },
        ) as resp:
            if resp.status_code != 200:
                body = resp.read()
                pytest.fail(f"responses 请求失败: {resp.status_code} {body!r}")
            lines = [line for line in resp.iter_lines() if line.strip()]

        completed = None
        for line in lines:
            if line.startswith("data: "):
                try:
                    evt = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "response.completed":
                    completed = evt
        assert completed is not None, "流必须包含 response.completed"
        output = completed["response"].get("output") or []
        texts = [c.get("text", "") for item in output if item.get("type") == "message" for c in item.get("content", [])]
        assert any("Hello world" in t for t in texts), f"completed.output 必须携带助手回复，实际 output={output}"

        # 状态必须已保存：completed 带 output 后 _save_response_state 才能提取
        # 助手回复，previous_response_id 多轮会话才不会失忆。
        # FileStore 的 asyncio.Lock 绑定 app 事件循环，测试里直接读状态文件。
        response_id = completed["response"].get("id")
        assert response_id

        from pathlib import Path

        state_file = Path(__file__).parent / "_test_data" / "responses_session" / f"{response_id}.json"
        assert state_file.exists(), f"response.completed 必须携带 output 以保存会话状态（H1），缺文件 {state_file}"
        state_data = json.loads(state_file.read_text(encoding="utf-8"))
        conversation_str = json.dumps(state_data.get("conversation") or {}, ensure_ascii=False)
        assert "Hello world" in conversation_str, f"会话历史必须包含助手回复，实际：{conversation_str[:500]}"


class TestM2ThinkFilterAnthropicTarget:
    """M2：ThinkFilter 对 Anthropic 目标格式（Claude Code → DeepSeek 系上游）应生效。

    - 上游 content 内嵌 💭 思考块（跨 chunk）必须被过滤，不得泄漏进 Claude 上下文。
    - 末尾未闭合 💭 且上游不发 [DONE] 时，EOF flush 残余必须输出为合法的
      content_block_delta 事件（此前是畸形 Chat 形状裸 data: 行）。
    """

    def _parse_anthropic_events(self, lines):
        events = []
        current_event = None
        for line in lines:
            line = line.strip()
            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: "):
                try:
                    events.append((current_event, json.loads(line[6:])))
                except json.JSONDecodeError:
                    events.append((current_event, line[6:]))
        return events

    def test_deepseek_upstream_think_blocks_filtered_for_claude_client(self, e2e_client):
        with e2e_client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "deepseek-chat",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                body = resp.read()
                pytest.fail(f"messages 请求失败: {resp.status_code} {body!r}")
            lines = [line for line in resp.iter_lines() if line.strip()]

        events = self._parse_anthropic_events(lines)
        assert events, "流不能为空"

        text_payloads = []
        for evt_type, data in events:
            assert isinstance(data, dict), f"SSE data 必须是合法 JSON，实际：{data!r}"
            if evt_type == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text_payloads.append(delta.get("text", ""))

        # 💭 思考内容不得泄漏
        joined = "".join(text_payloads)
        assert "hidden" not in joined, f"💭 思考块泄漏进 Claude 上下文：{joined!r}"
        assert "visible" in joined, f"正文必须保留：{joined!r}"

        # 末尾未闭合 💭 仍属于思考块，EOF 时必须丢弃，不得泄漏。
        assert "partial" not in joined, f"未闭合思考块泄漏进 Claude 上下文：{joined!r}"

        # 协议收尾完整：必须出现 message_stop
        evt_types = [evt_type for evt_type, _ in events]
        assert "message_stop" in evt_types, f"流必须以 message_stop 收尾：{evt_types}"

    def test_deepseek_upstream_with_done_think_blocks_filtered_for_claude_client(self, e2e_client):
        """M2 补充：上游发 [DONE] 时，[DONE] 分支的 flush 也必须输出合法 content_block_delta。"""
        with e2e_client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "deepseek-chat-done",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                body = resp.read()
                pytest.fail(f"messages 请求失败: {resp.status_code} {body!r}")
            lines = [line for line in resp.iter_lines() if line.strip()]

        events = self._parse_anthropic_events(lines)
        assert events, "流不能为空"

        text_payloads = []
        for evt_type, data in events:
            assert isinstance(data, dict), f"SSE data 必须是合法 JSON，实际：{data!r}"
            if evt_type == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text_payloads.append(delta.get("text", ""))

        # 💭 思考内容不得泄漏
        joined = "".join(text_payloads)
        assert "hidden" not in joined, f"💭 思考块泄漏进 Claude 上下文：{joined!r}"
        assert "visible" in joined, f"正文必须保留：{joined!r}"

        # 即使上游显式发 [DONE]，未闭合 💭 思考块也必须丢弃。
        assert "partial" not in joined, f"未闭合思考块泄漏进 Claude 上下文：{joined!r}"

        # 协议收尾完整：必须出现 message_stop
        evt_types = [evt_type for evt_type, _ in events]
        assert "message_stop" in evt_types, f"流必须以 message_stop 收尾：{evt_types}"
