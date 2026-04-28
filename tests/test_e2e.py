"""端到端集成测试 - 模拟 Claude Code / OpenCode 完整请求流程

测试流程:
1. 启动 mock 上游服务器 (端口 19999)
2. 创建临时渠道配置文件，指向 mock 服务器
3. 通过 FastAPI TestClient 发送请求，验证完整转换流程
"""
import json
import os
import sys
import tempfile
import time

import pytest
import httpx
from fastapi.testclient import TestClient
from multiprocessing import Process

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_mock_server():
    import uvicorn
    from tests.mock_server import app
    uvicorn.run(app, host="127.0.0.1", port=19999, log_level="error")


@pytest.fixture(scope="module")
def mock_server():
    """启动 mock 上游 API 服务器"""
    proc = Process(target=_run_mock_server, daemon=True)
    proc.start()
    time.sleep(1.5)
    yield proc
    proc.terminate()
    proc.join(timeout=5)


@pytest.fixture(autouse=True)
def setup_test_channels(tmp_path, monkeypatch):
    """为每个测试创建临时渠道配置"""
    channels_data = {
        "channels": [
            {
                "id": "ch_e2e_anthropic",
                "name": "E2E Anthropic Channel",
                "api_type": "anthropic",
                "base_url": "http://127.0.0.1:19999/anthropic",
                "api_key": "test-key",
                "models": ["claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-28T00:00:00Z",
            },
            {
                "id": "ch_e2e_openai",
                "name": "E2E OpenAI Channel",
                "api_type": "openai-chat-completions",
                "base_url": "http://127.0.0.1:19999/openai",
                "api_key": "test-key",
                "models": ["gpt-4o", "gpt-4"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-28T00:00:00Z",
            },
        ]
    }

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    channels_file.write_text(json.dumps(channels_data, ensure_ascii=False, indent=2))

    # Monkey-patch 配置和存储
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CHANNELS_FILE", str(channels_file))

    import config
    import storage
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))

    # 清除缓存
    storage.invalidate_cache()


@pytest.fixture
def client():
    """创建 FastAPI TestClient"""
    from main import app
    with TestClient(app) as c:
        yield c


# ─── 场景 1: Claude Code (Anthropic格式) → Anthropic 渠道 ───

class TestClaudeCodeToAnthropic:
    """场景1: Claude Code → Anthropic渠道 (直接透传)"""

    def test_non_stream(self, mock_server, client):
        resp = client.post("/v1/messages", json={
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["content"][0]["text"] == "Hello world"

    def test_stream(self, mock_server, client):
        with client.stream("POST", "/v1/messages", json={
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
            # Anthropic SSE 应包含 event: 行
            assert any(line.startswith("event:") for line in events)


# ─── 场景 2: Claude Code (Anthropic格式) → OpenAI 渠道 ───

class TestClaudeCodeToOpenAI:
    """场景2: Claude Code → OpenAI渠道 (需转换)"""

    def test_non_stream(self, mock_server, client):
        # 注意：这个场景中，入口是 Anthropic 格式，但渠道是 OpenAI
        # 需要将 Anthropic 请求转为 OpenAI，再将 OpenAI 响应转为 Anthropic
        # 但渠道只支持 gpt-4o，所以 model 要匹配
        resp = client.post("/v1/messages", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 响应应转为 Anthropic 格式
        assert data["type"] == "message"
        assert data["role"] == "assistant"


# ─── 场景 3: OpenCode (OpenAI格式) → OpenAI 渠道 ───

class TestOpenCodeToOpenAI:
    """场景3: OpenCode → OpenAI渠道 (直接透传)"""

    def test_non_stream(self, mock_server, client):
        resp = client.post("/v1/chat/completions", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello world"

    def test_stream(self, mock_server, client):
        with client.stream("POST", "/v1/chat/completions", json={
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
            # OpenAI SSE 仅有 data: 行
            assert any(line.startswith("data:") for line in chunks)


# ─── 场景 4: OpenCode (OpenAI格式) → Anthropic 渠道 ───

class TestOpenCodeToAnthropic:
    """场景4: OpenCode → Anthropic渠道 (需转换)"""

    def test_non_stream(self, mock_server, client):
        resp = client.post("/v1/chat/completions", json={
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 响应应转为 OpenAI Chat 格式
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello world"
