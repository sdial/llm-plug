"""端到端集成测试 - 模拟 Claude Code / OpenCode 完整请求流程

场景覆盖:
1. Claude Code (Anthropic) → Anthropic 渠道 (透传)
2. Claude Code (Anthropic) → OpenAI 渠道 (转换)
3. OpenCode (OpenAI) → OpenAI 渠道 (透传)
4. OpenCode (OpenAI) → Anthropic 渠道 (转换)
"""


class TestClaudeCodeToAnthropic:
    """场景1: Claude Code → Anthropic渠道 (直接透传)"""

    def test_non_stream(self, e2e_client):
        resp = e2e_client.post("/v1/messages", json={
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["content"][0]["text"] == "Hello world"

    def test_stream(self, e2e_client):
        with e2e_client.stream("POST", "/v1/messages", json={
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "stream": True,
        }) as resp:
            assert resp.status_code == 200
            events = []
            for line in resp.iter_lines():
                if line.strip():
                    events.append(line)
            assert len(events) > 0
            assert any(line.startswith("event:") for line in events)


class TestClaudeCodeToOpenAI:
    """场景2: Claude Code → OpenAI渠道 (需转换)"""

    def test_non_stream(self, e2e_client):
        resp = e2e_client.post("/v1/messages", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"


class TestOpenCodeToOpenAI:
    """场景3: OpenCode → OpenAI渠道 (直接透传)"""

    def test_non_stream(self, e2e_client):
        resp = e2e_client.post("/v1/chat/completions", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello world"

    def test_stream(self, e2e_client):
        with e2e_client.stream("POST", "/v1/chat/completions", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
            "stream": True,
        }) as resp:
            assert resp.status_code == 200
            chunks = []
            for line in resp.iter_lines():
                if line.strip():
                    chunks.append(line)
            assert len(chunks) > 0
            assert any(line.startswith("data:") for line in chunks)


class TestOpenCodeToAnthropic:
    """场景4: OpenCode → Anthropic渠道 (需转换)"""

    def test_non_stream(self, e2e_client):
        resp = e2e_client.post("/v1/chat/completions", json={
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello world"
