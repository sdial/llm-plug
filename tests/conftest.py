import asyncio
import json
import os
import shutil
import time
from multiprocessing import Process
from pathlib import Path

import pytest

from channel_catalog import catalog


@pytest.fixture(autouse=True)
def reset_channel_catalog():
    """测试之间不共享目录缓存或事件循环绑定。"""
    catalog.reset()
    yield
    catalog.reset()


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


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ─── E2E 测试环境 ───

_E2E_DATA_DIR = os.path.join(os.path.dirname(__file__), "_test_data")
_E2E_CHANNELS_FILE = os.path.join(_E2E_DATA_DIR, "channels.json")


def _setup_e2e_channels():
    """创建 E2E 测试渠道配置（嵌套 endpoints 形态）"""
    os.makedirs(_E2E_DATA_DIR, exist_ok=True)

    def _ep(api_type, base_url):
        return {"api_type": api_type, "base_url": base_url}

    channels_data = {
        "channels": [
            {
                "id": "ch_e2e_anthropic",
                "name": "E2E Anthropic Channel",
                "api_key": "test-key",
                "models": ["claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-28T00:00:00Z",
                "endpoints": [_ep("anthropic", "http://127.0.0.1:19999/anthropic")],
            },
            {
                "id": "ch_e2e_openai",
                "name": "E2E OpenAI Channel",
                "api_key": "test-key",
                "models": ["gpt-4o", "gpt-4"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-28T00:00:00Z",
                "endpoints": [_ep("openai-chat-completions", "http://127.0.0.1:19999/openai")],
            },
            {
                "id": "ch_e2e_deepseek",
                "name": "E2E DeepSeek Channel",
                "api_key": "test-key",
                "models": ["deepseek-chat", "deepseek-chat-done"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "upstream_profile_id": "deepseek",
                "catalog_revision": "builtin-2",
                "created_at": "2026-04-28T00:00:00Z",
                "endpoints": [_ep("openai-chat-completions", "http://127.0.0.1:19999/deepseek")],
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
    from channel_catalog import catalog

    config.DATA_DIR = _E2E_DATA_DIR
    config.CHANNELS_FILE = _E2E_CHANNELS_FILE
    config.API_KEYS_FILE = api_keys_file
    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None


def _run_mock_server():
    import uvicorn

    from tests.mock_server import app

    uvicorn.run(app, host="127.0.0.1", port=19999, log_level="error", loop="auto")


def _cleanup_e2e():
    # 整体删除：应用运行期间还会在 _E2E_DATA_DIR 下生成 admin_auth.json / stats.db /
    # channel_quota_limits.json / responses_session 等文件，旧的 unlink+rmdir 在目录
    # 非空时静默失败，残留 admin_auth.json 会让下一会话的 setup-login 判定密码已存在
    # 而返回 401（test_full_security_flow 偶发失败）。
    shutil.rmtree(_E2E_DATA_DIR, ignore_errors=True)


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
    """创建 E2E 测试客户端（每次清除持久化缓存和 Channel Catalog 缓存）"""
    import storage
    from channel_catalog import catalog

    # 每次使用前重写回标准 e2e 渠道：_setup_e2e_channels 只在 session 级 mock
    # server fixture 创建时执行过一次，同会话更早的编排测试若覆写共享的
    # tests/_test_data/channels.json 且不恢复，会
    # 把文件换掉，导致后续 TestClient 启动时读到 0 渠道（test_e2e.py 偶发 400）。
    _setup_e2e_channels()
    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as c:
        yield c


# ── ADR-0027 08 兼容：ctx-opt 缓存下沉 stats 层，07 快照仍通过 routers.admin.stats._CTX_OPT_CACHE / time 访问 ──
# router 层已纯透传（聚合与缓存全局删除），但 07 快照的 fixture 与 TTL 打桩仍经 router 模块句柄
# 访问（`ctx_stats._CTX_OPT_CACHE.clear()` / `ctx_stats.time.monotonic`）。本 shim 在测试进程启动时
# 将 stats 层缓存句柄回注到 router 模块，使旧快照不改一字仍隔离正确；time 为同一 stdlib 模块，
# 打桩 `time.monotonic` 全局生效。
try:
    import routers.admin.stats as _router_ctx_compat  # noqa: F401
    import stats as _stats_ctx_compat  # noqa: F401

    if not hasattr(_router_ctx_compat, "_CTX_OPT_CACHE"):
        _router_ctx_compat._CTX_OPT_CACHE = _stats_ctx_compat._CTX_OPT_CACHE  # type: ignore[attr-defined]
    if not hasattr(_router_ctx_compat, "_CTX_OPT_CACHE_TTL"):
        _router_ctx_compat._CTX_OPT_CACHE_TTL = _stats_ctx_compat._CTX_OPT_CACHE_TTL  # type: ignore[attr-defined]
    import time as _time_ctx_compat  # noqa: F401

    if not hasattr(_router_ctx_compat, "time"):
        _router_ctx_compat.time = _time_ctx_compat  # type: ignore[attr-defined]
except Exception:
    pass
