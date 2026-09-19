"""Ticket 08 — e2e：探活成功→记忆抢回→业务请求粘主（D7 承诺落地）。

全真实发送链（e2e_mock_server 19999）：渠道重配模拟主恢复（``/fail-openai`` →
``/openai``），``take_probe_candidates`` → ``run_probe_round`` 探活成功后
粘滞记忆从备用抢回主渠道；随后业务请求经 ``e2e_client`` 自首选（被抢回）渠道
起、不落备用。用 mock 服务器的请求计数区分实际服务的上游路径：
``/openai``（恢复后的主）与 ``/deepseek``（备用）。

与 Single-status e2e 的取舍：真实探活（openai 流 [[DONE]] 收尾，既有已验证路径）
+ 真实业务路由，覆盖 D2/D7/用户故事 3&5；不重测 07 的记账细节（unit/integration
已覆盖），不测多对收敛（unit 已覆盖）。
"""

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
_GRP_ID = "grp_probe_reclaim"
_GRP_NAME = "ProbeReclaim"
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


async def test_probe_success_reclaims_sticky_and_next_request_sticks_to_primary(e2e_client):
    """主恢复 → 探活成功抢回记忆 → 组内首个业务请求即粘主、不落备用。"""
    from proxy import group_probe

    # ── 故障期：主降级、记忆停在备用 ──
    _write_channels("/fail-openai")
    outcomes.record(_PROBE_MODEL, _PRIMARY, OutcomeKind.http_5xx, t=time.time() - 1000)
    outcomes.remember_preferred(_GRP_ID, _PROBE_MODEL, _BACKUP)

    # 故障期业务请求命中备用（deepseek），记忆仍指备用
    resp = e2e_client.post("/v1/chat/completions", json=_business_body())
    assert resp.status_code == 200
    assert outcomes.sticky_preferred(_GRP_ID, _PROBE_MODEL) == _BACKUP

    # ── 恢复：主渠道改为健康接入点 /openai，降级视图保持 → 探活验证 ──
    _write_channels("/openai")

    candidates = await group_probe.take_probe_candidates(now=time.time() + 3600, interval_seconds=60)
    assert {(c.model, c.channel_id) for c in candidates} == {(_PROBE_MODEL, _PRIMARY)}
    result = await group_probe.run_probe_round(candidates, timeout=5)

    assert result.succeeded_pairs == {(_PROBE_MODEL, _PRIMARY)}
    # 记忆被抢回主渠道（D7），降级对出视图
    assert outcomes.sticky_preferred(_GRP_ID, _PROBE_MODEL) == _PRIMARY
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
