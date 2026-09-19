"""Ticket 07 — 单轮探活驱动 e2e：e2e_mock_server（19999）故障端点全路径验证。

场景覆盖 spec 覆盖清单对应项：成功（目标恢复 → 解冻出视图）/ 5xx / 瞬时限速 429
/ 窗口级 429（is_blocked 置位、后续轮次跳过）/ 404（permanent 停探）/ 挂起超时
（显式 transport_failure，非仅 cancelled）。全部经真实发送链：
``take_probe_candidates`` → ``run_probe_round`` → 真实 dispatch / select_channel /
公共 ``channel_attempt.attempt_channel`` 入口 / converter /
capability / 接入点回退。

构造方式沿用 test_lb_key_isolation_e2e / test_endpoint_fallback_e2e：直写
``tests/_test_data/channels.json``（含 channels + model_groups），清 Channel Catalog /
Access Key 缓存后在同一数据上驱动；降级态用 ``outcomes.record`` 注入
（确定性：事件 t 置于过去、枚举 now 置于未来，退避必到账）。
"""

import json
import os
import time

import pytest

from proxy import outcomes
from proxy.outcomes import OutcomeKind

pytestmark = pytest.mark.asyncio

_BASE = "http://127.0.0.1:19999"
_E2E_CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "_test_data", "channels.json")


@pytest.fixture(autouse=True)
def _reset_outcomes():
    """测试间隔离：清空健康/阻塞/粘滞视图并把熔断阈值钉在 spec 默认（3 / 120）。"""
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    yield
    outcomes.reset()


_PROBE_MODEL = "probe-model"
_PROBE_CHANNEL_ID = "ch_probe"
_PROBE_GROUP = [
    {
        "id": "grp_probe",
        "name": "ProbeGroup",
        "items": [{"model": _PROBE_MODEL}],
        "enabled": True,
        "lazy_sticky": True,
    }
]


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


def _setup_probe_channels(base_prefix: str) -> None:
    data = {
        "channels": [
            {
                "id": _PROBE_CHANNEL_ID,
                "name": "Probe Channel",
                "api_key": "test-key",
                "models": [_PROBE_MODEL],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "endpoints": [{"api_type": "openai-chat-completions", "base_url": f"{_BASE}{base_prefix}"}],
            }
        ],
        "model_groups": _PROBE_GROUP,
    }
    with open(_E2E_CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    _reset_storage_caches()


def _degrade() -> None:
    """把 (probe-model, ch_probe) 置为降级且退避已到期（consecutive_failures=1，first_failed 在过去）。"""
    outcomes.record(_PROBE_MODEL, _PROBE_CHANNEL_ID, OutcomeKind.http_5xx, t=time.time() - 1000)


async def _probe_once(base_prefix: str, *, timeout: float = 5.0):
    """写渠道 + 降级 + 枚举 + 单轮探测，返回 (ProbeRoundResult, candidates)。"""
    from proxy import group_probe

    _setup_probe_channels(base_prefix)
    _degrade()
    candidates = await group_probe.take_probe_candidates(now=time.time() + 3600, interval_seconds=60)
    assert candidates, f"{base_prefix}: 应产出探活目标"
    assert {c.channel_id for c in candidates} == {_PROBE_CHANNEL_ID}
    result = await group_probe.run_probe_round(candidates, timeout=timeout)
    return result, candidates


async def _failed_row(channel_id: str):
    rows = [t for t in outcomes.probe_targets() if t.channel_id == channel_id]
    return rows[0] if rows else None


async def test_success_recovering_target_clears_degraded(e2e_mock_server):
    """目标恢复（上游正常）→ 流消费到底 → record(success) 解冻：is_degraded 出视图。"""
    result, _ = await _probe_once("/openai")

    assert result.succeeded_pairs == {(_PROBE_MODEL, _PROBE_CHANNEL_ID)}
    assert result.failed == () and result.skipped == ()
    assert outcomes.is_degraded(_PROBE_MODEL, _PROBE_CHANNEL_ID) is False
    assert outcomes.is_healthy(_PROBE_MODEL, _PROBE_CHANNEL_ID) is True
    assert await _failed_row(_PROBE_CHANNEL_ID) is None


async def test_5xx_keeps_pair_degraded_and_advances_backoff(e2e_mock_server):
    """上游 5xx → http_5xx 经既有链路记账：该对保持降级，退避计数递增。"""
    result, _ = await _probe_once("/fail-openai")

    assert result.failed[0].kind == OutcomeKind.http_5xx.value
    assert outcomes.is_degraded(_PROBE_MODEL, _PROBE_CHANNEL_ID) is True
    row = await _failed_row(_PROBE_CHANNEL_ID)
    assert row is not None and row.consecutive_failures >= 2  # 既有链路已记两次
    assert row.permanent is False


async def test_transient_429_fails_honestly_without_blocking(e2e_mock_server):
    """瞬时限速 429（预算 0）：如实失败不等待，不置 is_blocked，对保持降级。"""
    result, _ = await _probe_once("/429-chat")

    assert result.failed[0].kind == OutcomeKind.http_429.value
    assert outcomes.is_blocked(_PROBE_CHANNEL_ID) is False
    assert outcomes.is_degraded(_PROBE_MODEL, _PROBE_CHANNEL_ID) is True


async def test_window_429_blocks_channel_and_skips_next_round(e2e_mock_server):
    """窗口级 429 → quota_window → is_blocked 置位；后续枚举跳过该渠道。"""
    result, _ = await _probe_once("/quota-chat")

    assert result.failed[0].kind == OutcomeKind.http_429.value
    assert outcomes.is_blocked(_PROBE_CHANNEL_ID) is True
    # 下一轮枚举：is_blocked 渠道不再产出探活目标（is_blocked 优先于一切）
    from proxy import group_probe

    next_candidates = await group_probe.take_probe_candidates(now=time.time() + 3600, interval_seconds=60)
    assert next_candidates == []


async def test_404_sets_permanent_and_stops_probing(e2e_mock_server):
    """上游 404 → http_4xx_config → permanent 置位：保持降级、移出探活调度。"""
    result, _ = await _probe_once("/404-chat")

    assert result.failed[0].kind == OutcomeKind.http_4xx_config.value
    assert outcomes.is_degraded(_PROBE_MODEL, _PROBE_CHANNEL_ID) is True
    from proxy import group_probe

    assert outcomes.probe_targets() == []
    assert await group_probe.take_probe_candidates(now=time.time() + 3600, interval_seconds=60) == []


async def test_hang_times_out_with_transport_failure_evidence(e2e_mock_server):
    """上游挂起（不回数据）→ 探活超时 → 显式 transport_failure：退避推进而非无责 cancelled。"""
    assert outcomes.is_blocked(_PROBE_CHANNEL_ID) is False
    result, _ = await _probe_once("/hang-chat", timeout=1.0)

    assert result.failed[0].kind == OutcomeKind.transport_failure.value
    row = await _failed_row(_PROBE_CHANNEL_ID)
    assert row is not None and row.consecutive_failures >= 2  # 显式 transport_failure 已推进退避
    assert outcomes.is_degraded(_PROBE_MODEL, _PROBE_CHANNEL_ID) is True
