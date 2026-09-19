"""Ticket 09 — main.py:lifespan 探活后台任务启停接线（ADR-0010 D10）。

断言外部可观察行为：lifespan 启动时 ``create_task(run_group_probe_loop)``、
shutdown 时 ``cancel`` 并等待收敛（无孤儿任务）；循环体通过 main 的调用期 import
（``_group_probe_loop``）解析到 ``proxy.group_probe.run_group_probe_loop``，故用
patch 替换后置函数验证整条接线（import 解析 + 启停）。沿用 tests/test_lifespan.py
的 tmp data 目录 fixture 与 lifespan 直驱形态。
"""

import asyncio
import json
from unittest.mock import patch

import pytest

import config
import storage


@pytest.fixture(autouse=True)
def setup_data(tmp_path, monkeypatch):
    """Set up test data directory with channels and API keys."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    channels_data = {
        "channels": [
            {
                "id": "ch_1",
                "name": "Test",
                "endpoints": [
                    {"api_type": "openai-chat-completions", "base_url": "https://api.example.com"},
                ],
                "api_key": "key",
                "models": ["gpt-4o", "gpt-4"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "socks5_proxy": None,
                "created_at": "2026-04-30T00:00:00Z",
            },
        ]
    }
    api_keys_data = {"api_keys": [{"id": "key_1", "name": "test-key", "key": "sk-test"}]}
    with open(channels_file, "w") as f:
        json.dump(channels_data, f)
    with open(api_keys_file, "w") as f:
        json.dump(api_keys_data, f)

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))
    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None

    yield

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._channels_lock = None
    storage._keys_lock = None


def test_lifespan_starts_and_cancels_probe_loop():
    """lifespan 启动即创建探活任务、shutdown 时取消且无孤儿任务（恰启动一次）。"""
    from main import app

    started = asyncio.Event()
    cancelled = asyncio.Event()
    entered = {"n": 0}

    async def fake_probe_loop():
        entered["n"] += 1
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), 5)

    with (
        patch("proxy.group_probe.run_group_probe_loop", new=fake_probe_loop),
        patch("main.close_all_clients"),
    ):
        asyncio.run(run_lifespan())

    assert entered["n"] == 1  # 循环被启动（且仅一份任务）
    assert started.is_set()
    assert cancelled.is_set()  # shutdown 已 cancel 探活任务，无孤儿
