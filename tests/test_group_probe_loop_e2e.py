"""Ticket 09 — 探活后台循环全链路 e2e：循环本身驱动主恢复 → 自动回切到主。

复用 test_group_probe_reclaim_e2e 的排布（e2e_mock_server 19999 + 直写
tests/_test_data/channels.json + TestClient 真实业务路由）：主渠道 /fail-openai
降级、粘滞记忆停在备用；恢复（/openai）后由 ``run_group_probe_loop``（短注入间隔）
自动探测——成功经既有链 ``record(success)`` 解冻 + ``reclaim_sticky_preferred``
抢回记忆（08 内建）→ 后续业务请求自首选主渠道起、不落备用。

与 08 e2e 的区别（本测试的增值点）：08 手动调 ``take_probe_candidates`` +
``run_probe_round``；本测试不动任何探活函数，断言 *循环本身*（用户故事 1/20：
主恢复 ≤ 探活间隔自动回切）把整条链驱动起来。结果以 outcomes 视图最终态 /
mock 请求计数断言，不断言循环内部时序。
"""

import asyncio
import contextlib
import json
import os
import time

import httpx
import pytest

from proxy import outcomes
from proxy.outcomes import OutcomeKind

pytestmark = pytest.mark.asyncio

_BASE = "http://127.0.0.1:19999"
_E2E_CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "_test_data", "channels.json")

_PROBE_MODEL = "probe-model"
_GRP_ID = "grp_probe_loop"
_GRP_NAME = "ProbeLoop"
_PRIMARY = "ch_primary"
_BACKUP = "ch_backup"


@pytest.fixture(autouse=True)
def _reset_outcomes():
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    yield
    outcomes.reset()


def _reset_storage_caches() -> None:
    """绕过 TTL：直改文件后同步清 storage / 模型组 / 渠道注册缓存（AGENTS.md 测试场景豁免）"""
    import storage
    from channel_catalog import catalog

    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None
    storage._MODEL_GROUPS_CACHE = None
    storage._MODEL_GROUPS_CACHE_TS = 0
    storage._model_groups_lock = None


def _write_channels(primary_path: str) -> None:
    """两个渠道服务 probe-model；主渠道接 primary_path，备用接 /deepseek。"""
    channels_data = {
        "channels": [
            {
                "id": _PRIMARY,
                "name": "Primary",
                "api_key": "test-key",
                "models": [_PROBE_MODEL],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "endpoints": [{"api_type": "openai-chat-completions", "base_url": f"{_BASE}{primary_path}"}],
            },
            {
                "id": _BACKUP,
                "name": "Backup",
                "api_key": "test-key",
                "models": [_PROBE_MODEL],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "endpoints": [{"api_type": "openai-chat-completions", "base_url": f"{_BASE}/deepseek"}],
            },
        ],
        "model_groups": [
            {
                "id": _GRP_ID,
                "name": _GRP_NAME,
                "items": [{"model": _PROBE_MODEL}, {"model": _PROBE_MODEL}],
                "enabled": True,
                "lazy_sticky": True,
            }
        ],
    }
    with open(_E2E_CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(channels_data, f)
    _reset_storage_caches()


def _business_body() -> dict:
    return {"model": _GRP_NAME, "messages": [{"role": "user", "content": "hi"}], "stream": False}


async def _wait_until(cond, *, timeout=10.0, interval=0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError("条件在限期内未达成")


async def test_probe_loop_drives_recovery_and_business_sticks_to_primary(e2e_client):
    """主恢复 → 后台循环自动探活成功 → 记忆抢回主 → 业务请求粘主、备用量镜零新增。"""
    from proxy import group_probe

    # ── 故障期：主降级、记忆停在备用 ──
    _write_channels("/fail-openai")
    outcomes.record(_PROBE_MODEL, _PRIMARY, OutcomeKind.http_5xx, t=time.time() - 1000)
    outcomes.remember_preferred(_GRP_ID, _PROBE_MODEL, _BACKUP)

    resp = e2e_client.post("/v1/chat/completions", json=_business_body())
    assert resp.status_code == 200
    assert outcomes.sticky_preferred(_GRP_ID, _PROBE_MODEL) == _BACKUP

    # ── 恢复：主渠道改为健康接入点 /openai，降级视图保持 → 启动后台循环 ──
    _write_channels("/openai")

    task = asyncio.create_task(group_probe.run_group_probe_loop(interval_seconds=1))
    try:
        # 循环本身驱动恢复：无人触碰 take/run 函数，记忆被自动抢回主渠道
        await _wait_until(lambda: outcomes.sticky_preferred(_GRP_ID, _PROBE_MODEL) == _PRIMARY)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert outcomes.is_degraded(_PROBE_MODEL, _PRIMARY) is False
    assert [t for t in outcomes.probe_targets() if t.channel_id == _PRIMARY] == []

    # ── 回切后：清空计数再发业务请求，应粘主（/openai）、备用量镜零新增 ──
    with httpx.Client(timeout=5) as hc:
        hc.post(f"{_BASE}/_test/reset-counts")
        r2 = e2e_client.post("/v1/chat/completions", json=_business_body())
        assert r2.status_code == 200
        counts = hc.get(f"{_BASE}/_test/request-counts").json()

    assert counts.get("/openai/v1/chat/completions", 0) == 1
    assert counts.get("/deepseek/v1/chat/completions", 0) == 0  # 备用未再被触碰
