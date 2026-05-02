import pytest
import asyncio
import json
import os
import time
from pathlib import Path
from multiprocessing import Process


@pytest.fixture(scope="session")
def fixtures_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def anthropic_request():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "anthropic_request.json") as f:
        return json.load(f)


@pytest.fixture
def openai_chat_request():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "openai_chat_request.json") as f:
        return json.load(f)


@pytest.fixture
def openai_response_request():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "openai_response_request.json") as f:
        return json.load(f)


@pytest.fixture
def mock_channels():
    fixtures_dir = Path(__file__).parent / "fixtures"
    with open(fixtures_dir / "mock_channels.json") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ─── E2E 测试环境 ───

_E2E_DATA_DIR = os.path.join(os.path.dirname(__file__), "_test_data")
_E2E_CHANNELS_FILE = os.path.join(_E2E_DATA_DIR, "channels.json")


def _setup_e2e_channels():
    """创建 E2E 测试渠道配置"""
    os.makedirs(_E2E_DATA_DIR, exist_ok=True)
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
    with open(_E2E_CHANNELS_FILE, "w") as f:
        json.dump(channels_data, f)

    # 创建空的 api_keys.json，确保向后兼容的免认证模式
    api_keys_file = os.path.join(_E2E_DATA_DIR, "api_keys.json")
    with open(api_keys_file, "w") as f:
        json.dump({"api_keys": []}, f)

    import config
    import storage
    config.DATA_DIR = _E2E_DATA_DIR
    config.CHANNELS_FILE = _E2E_CHANNELS_FILE
    config.API_KEYS_FILE = api_keys_file
    storage.invalidate_cache()
    storage.invalidate_keys_cache()


def _run_mock_server():
    import uvicorn
    from tests.mock_server import app
    uvicorn.run(app, host="127.0.0.1", port=19999, log_level="error", loop="auto")


def _cleanup_e2e():
    try:
        os.unlink(_E2E_CHANNELS_FILE)
        os.rmdir(_E2E_DATA_DIR)
    except OSError:
        pass


@pytest.fixture(scope="session")
def e2e_mock_server():
    """启动 E2E mock 服务器（session 级别，只启动一次）"""
    _setup_e2e_channels()
    proc = Process(target=_run_mock_server, daemon=True)
    proc.start()
    time.sleep(1.5)
    yield proc
    proc.terminate()
    proc.join(timeout=5)
    _cleanup_e2e()


@pytest.fixture
def e2e_client(e2e_mock_server):
    """创建 E2E 测试客户端（每次清除 storage 缓存和 proxy_core 渠道缓存）"""
    import storage
    import proxy_core
    storage._cache = None
    storage._cache_ts = 0
    proxy_core._model_channels_cache = None
    from main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c
