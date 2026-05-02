"""端到端集成测试 - 模拟 Claude Code / OpenCode 完整请求流程"""
import os
import sys
import time
import pytest
import httpx
from multiprocessing import Process

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_mock_server():
    import uvicorn
    from tests.mock_server import app
    uvicorn.run(app, host="127.0.0.1", port=19999, log_level="error", loop="auto")


def _run_proxy_server():
    import uvicorn
    # 设置测试环境变量
    os.environ["PORT"] = "19998"
    os.environ["DATA_DIR"] = os.path.join(os.path.dirname(__file__), "fixtures", "test_data")
    uvicorn.run("main:app", host="127.0.0.1", port=19998, log_level="error", loop="auto")


@pytest.fixture(scope="module")
def mock_server():
    """启动 mock 上游 API 服务器"""
    proc = Process(target=_run_mock_server, daemon=True)
    proc.start()
    time.sleep(1.5)
    yield proc
    proc.terminate()
    proc.join(timeout=5)


@pytest.fixture
def mock_client():
    """直接访问 mock 上游服务器的客户端"""
    with httpx.Client(base_url="http://127.0.0.1:19999", timeout=10) as client:
        yield client


# ─── 直接测试 Mock 服务器 ───

class TestMockServer:
    """验证 mock 服务器本身工作正常"""

    def test_anthropic_endpoint(self, mock_server, mock_client):
        resp = mock_client.post("/anthropic/v1/messages", json={
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"

    def test_openai_endpoint(self, mock_server, mock_client):
        resp = mock_client.post("/openai/v1/chat/completions", json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello world"

    def test_anthropic_stream(self, mock_server, mock_client):
        with mock_client.stream("POST", "/anthropic/v1/messages", json={
            "model": "claude-sonnet-4-20250514",
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

    def test_openai_stream(self, mock_server, mock_client):
        with mock_client.stream("POST", "/openai/v1/chat/completions", json={
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
