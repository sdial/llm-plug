"""ADR-0008 D0 — proxy/outcomes 深模块公共 API 测试。

只测外部行为：``record`` 后立即可读到对应视图（``is_healthy`` /
``is_degraded`` / ``is_blocked`` / ``sticky_preferred`` / ``probe_targets``）；
不测内部事件环 / OrderedDict 形态，也不做 time.time() 注入（冷却用真实时钟 +
短冷却 + sleep）。

覆盖：多模型隔离、kind 区分、降级语义、quota 写穿 + 重启重载、冷却自愈被
``is_degraded`` 兜住、粘滞基础视图、探活候选、记账键兜底链
``effective_model``（ADR-0025 D0）与探活候选虚拟键过滤（ADR-0025 D1-A2）。
"""

import json
import time
from datetime import UTC, datetime

import pytest

from proxy import outcomes


@pytest.fixture(autouse=True)
def _reset_outcomes():
    """每个用例前后清空模块状态，并钉住默认阈值，避免被 config 全局状态污染。"""
    outcomes.reset()
    outcomes.set_quota_adapter(None)
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    yield
    outcomes.reset()
    outcomes.set_quota_adapter(None)
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)


# ═══════════════════════════════════════════
#  多模型隔离：A 模型的故障不污染 B 模型
# ═══════════════════════════════════════════


class TestMultiModelIsolation:
    def test_failures_are_per_model(self):
        for _ in range(3):
            outcomes.record("model_a", "ch", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("model_a", "ch") is False
        assert outcomes.is_healthy("model_b", "ch") is True

    def test_degraded_is_per_model(self):
        outcomes.record("model_a", "ch", outcomes.OutcomeKind.http_4xx_config)
        assert outcomes.is_degraded("model_a", "ch") is True
        assert outcomes.is_degraded("model_b", "ch") is False


# ═══════════════════════════════════════════
#  kind 区分：失败类 / quota 窗口 / 取消各自行为
# ═══════════════════════════════════════════


class TestKindDistinction:
    @pytest.mark.parametrize(
        "kind",
        [
            outcomes.OutcomeKind.transport_failure,
            outcomes.OutcomeKind.http_5xx,
            outcomes.OutcomeKind.http_429,
            outcomes.OutcomeKind.http_4xx_config,
            outcomes.OutcomeKind.rate_limit_exhausted,
        ],
    )
    def test_failure_kinds_set_degraded(self, kind):
        outcomes.record("m", "ch", kind)
        assert outcomes.is_degraded("m", "ch") is True

    def test_cancelled_does_not_affect_health_or_degraded(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.cancelled)
        assert outcomes.is_healthy("m", "ch") is True
        assert outcomes.is_degraded("m", "ch") is False

    def test_http_4xx_config_stays_degraded_until_success(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_4xx_config)
        assert outcomes.is_degraded("m", "ch") is True
        # 冷却假自愈不会清除配置类降级
        outcomes.configure(cooldown_seconds=0.01)
        time.sleep(0.02)
        assert outcomes.is_healthy("m", "ch") is True
        assert outcomes.is_degraded("m", "ch") is True
        # 只有真实 success 才清除
        outcomes.record("m", "ch", outcomes.OutcomeKind.success)
        assert outcomes.is_degraded("m", "ch") is False

    def test_record_accepts_string_kind(self):
        outcomes.record("m", "ch", "http_5xx")
        assert outcomes.is_degraded("m", "ch") is True
        outcomes.record("m", "ch", "success")
        assert outcomes.is_degraded("m", "ch") is False

    def test_record_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            outcomes.record("m", "ch", "not_a_kind")


# ═══════════════════════════════════════════
#  降级 / 健康语义：success 清除、失败计数、冷却自愈兜底
# ═══════════════════════════════════════════


class TestHealthAndDegraded:
    def test_success_clears_degraded_and_healthy(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.transport_failure)
        assert outcomes.is_degraded("m", "ch") is True
        outcomes.record("m", "ch", outcomes.OutcomeKind.success)
        assert outcomes.is_degraded("m", "ch") is False
        assert outcomes.is_healthy("m", "ch") is True

    def test_success_resets_fail_count(self):
        for _ in range(3):
            outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("m", "ch") is False
        outcomes.record("m", "ch", outcomes.OutcomeKind.success)
        assert outcomes.is_healthy("m", "ch") is True
        assert outcomes.is_degraded("m", "ch") is False

    def test_below_max_fail_count_stays_healthy(self):
        for _ in range(2):
            outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("m", "ch") is True

    def test_cooldown_self_heal_caught_by_degraded(self):
        """冷却到期 is_healthy 假自愈，但真实降级由 is_degraded 兜住。"""
        outcomes.configure(cooldown_seconds=0.01, max_fail_count=3)
        for _ in range(3):
            outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("m", "ch") is False
        assert outcomes.is_degraded("m", "ch") is True
        time.sleep(0.03)
        assert outcomes.is_healthy("m", "ch") is True
        assert outcomes.is_degraded("m", "ch") is True

    def test_rate_limit_exhausted_counts_as_failure(self):
        for _ in range(3):
            outcomes.record("m", "ch", outcomes.OutcomeKind.rate_limit_exhausted)
        assert outcomes.is_healthy("m", "ch") is False
        assert outcomes.is_degraded("m", "ch") is True


# ═══════════════════════════════════════════
#  quota 窗口：is_blocked + JSON 写穿 + 重启重载
# ═══════════════════════════════════════════


class TestQuotaWindow:
    def test_quota_window_blocks_channel(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
        assert outcomes.is_blocked("ch") is True

    def test_quota_window_expired_not_blocked(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.quota_window, reset_at=time.time() - 1)
        assert outcomes.is_blocked("ch") is False

    def test_quota_window_does_not_affect_health_or_degraded(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
        assert outcomes.is_healthy("m", "ch") is True
        assert outcomes.is_degraded("m", "ch") is False

    def test_write_through_and_reload(self, tmp_path):
        """kind=quota_window 触发写穿适配器；重启后 load_quota_limits 从文件恢复。"""
        quota_file = tmp_path / "channel_quota_limits.json"

        def adapter(channel_id, reset_at_unix, code):
            dt = datetime.fromtimestamp(reset_at_unix, tz=UTC)
            quota_file.write_text(json.dumps({channel_id: {"reset_at": dt.isoformat(), "code": code}}))

        outcomes.set_quota_adapter(adapter)
        outcomes.record(
            "m",
            "ch",
            outcomes.OutcomeKind.quota_window,
            reset_at=time.time() + 3600,
            code="AccountQuotaExceeded",
        )
        assert outcomes.is_blocked("ch") is True
        assert quota_file.exists()
        data = json.loads(quota_file.read_text())
        assert data["ch"]["code"] == "AccountQuotaExceeded"

        # 重启重载：清空内存后从文件恢复
        outcomes.reset()
        assert outcomes.is_blocked("ch") is False
        outcomes.load_quota_limits(str(quota_file))
        assert outcomes.is_blocked("ch") is True


# ═══════════════════════════════════════════
#  粘滞首选（基础版视图）与探活候选
# ═══════════════════════════════════════════


class TestStickyAndProbe:
    def test_sticky_preferred_basic(self):
        assert outcomes.sticky_preferred("g1", "m") is None
        outcomes.remember_preferred("g1", "m", "ch_a")
        assert outcomes.sticky_preferred("g1", "m") == "ch_a"

    def test_sticky_preferred_group_and_model_isolated(self):
        outcomes.remember_preferred("g1", "m", "ch_a")
        outcomes.remember_preferred("g2", "m", "ch_b")
        outcomes.remember_preferred("g1", "other_model", "ch_c")
        assert outcomes.sticky_preferred("g1", "m") == "ch_a"
        assert outcomes.sticky_preferred("g2", "m") == "ch_b"
        assert outcomes.sticky_preferred("g1", "other_model") == "ch_c"

    def test_probe_targets_returns_degraded_pairs(self):
        outcomes.record("m_a", "ch1", outcomes.OutcomeKind.http_5xx)
        outcomes.record("m_b", "ch2", outcomes.OutcomeKind.success)
        outcomes.record("m_c", "ch3", outcomes.OutcomeKind.cancelled)
        targets = sorted(outcomes.probe_targets(), key=lambda t: (t.model, t.channel_id))
        assert [(t.model, t.channel_id) for t in targets] == [("m_a", "ch1")]


# ═══════════════════════════════════════════
#  富视图行（ADR-0010 D6）：consecutive_failures / first_failed_at
#  只断言 probe_targets() 外部视图，不断言内部存储结构
# ═══════════════════════════════════════════


class TestProbeTargetRichView:
    @pytest.mark.parametrize(
        "kind",
        [
            outcomes.OutcomeKind.transport_failure,
            outcomes.OutcomeKind.http_5xx,
            outcomes.OutcomeKind.http_429,
            outcomes.OutcomeKind.rate_limit_exhausted,
        ],
    )
    def test_failure_kind_starts_consecutive_count(self, kind):
        outcomes.record("m", "ch", kind)
        (target,) = outcomes.probe_targets()
        assert target.consecutive_failures == 1
        assert target.first_failed_at > 0
        # 非配置类失败不置 permanent，仍产出探活目标（http_4xx_config 置位见 TestPermanentFlag）
        assert target.permanent is False

    def test_consecutive_failures_increment_across_mixed_kinds(self):
        for kind in [
            outcomes.OutcomeKind.transport_failure,
            outcomes.OutcomeKind.http_5xx,
            outcomes.OutcomeKind.http_429,
            outcomes.OutcomeKind.rate_limit_exhausted,
        ]:
            outcomes.record("m", "ch", kind)
        (target,) = outcomes.probe_targets()
        assert target.consecutive_failures == 4
        # http_4xx_config 置 permanent 后对移出探活视图（连退避都停）——
        # 混合序列含配置类时由 TestPermanentFlag 覆盖语义
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_4xx_config)
        assert outcomes.probe_targets() == []

    def test_success_zeroes_and_restarts_count(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        outcomes.record("m", "ch", outcomes.OutcomeKind.success)
        assert outcomes.probe_targets() == []
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_429)
        (target,) = outcomes.probe_targets()
        assert target.consecutive_failures == 1

    def test_cooldown_self_heal_does_not_clear_consecutive_failures(self):
        """冷却假自愈只清 fail_count，退避计数由 consecutive_failures 独立承载。"""
        outcomes.configure(max_fail_count=3, cooldown_seconds=0.01)
        for _ in range(4):
            outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_healthy("m", "ch") is False
        (before,) = outcomes.probe_targets()
        assert before.consecutive_failures == 4
        first_failed_at = before.first_failed_at
        time.sleep(0.03)
        assert outcomes.is_healthy("m", "ch") is True  # 假自愈
        (after,) = outcomes.probe_targets()
        assert after.consecutive_failures == 4  # 退避计数不清零
        assert after.first_failed_at == first_failed_at  # 降级期起点不变
        # 假自愈后的新失败仍属同一降级期：起点不刷新、计数继续累加
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_429)
        (after_more,) = outcomes.probe_targets()
        assert after_more.consecutive_failures == 5
        assert after_more.first_failed_at == first_failed_at

    def test_first_failed_at_marks_degraded_period_start(self):
        t0 = 1000.0
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx, t=t0)
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_429, t=t0 + 10)
        (target,) = outcomes.probe_targets()
        assert target.first_failed_at == t0
        assert target.consecutive_failures == 2
        # success 结束降级期；新一轮失败的起点刷新
        outcomes.record("m", "ch", outcomes.OutcomeKind.success, t=t0 + 20)
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx, t=t0 + 30)
        (target,) = outcomes.probe_targets()
        assert target.first_failed_at == t0 + 30
        assert target.consecutive_failures == 1

    def test_rich_rows_isolated_per_pair(self):
        outcomes.record("m_a", "ch1", outcomes.OutcomeKind.http_5xx)
        outcomes.record("m_a", "ch1", outcomes.OutcomeKind.http_5xx)
        outcomes.record("m_b", "ch2", outcomes.OutcomeKind.http_429, t=500.0)
        targets = sorted(outcomes.probe_targets(), key=lambda t: (t.model, t.channel_id))
        assert [(t.model, t.channel_id) for t in targets] == [("m_a", "ch1"), ("m_b", "ch2")]
        assert targets[0].consecutive_failures == 2
        assert targets[1].consecutive_failures == 1
        assert targets[1].first_failed_at == 500.0


# ═══════════════════════════════════════════
#  permanent 位（ADR-0010 D5）：401/403/404 置位、解除管线、400 不置位
#  只断言外部视图（probe_targets / is_degraded），不断言内部存储结构
# ═══════════════════════════════════════════


class TestPermanentFlag:
    def test_http_4xx_config_sets_permanent_and_excludes_from_probe_view(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_4xx_config)
        # 保留 is_degraded：组层持续不可选（不让坏对回流烧业务请求）
        assert outcomes.is_degraded("m", "ch") is True
        # 探活目标视图不再产出：调度侧停探，连退避都停
        assert outcomes.probe_targets() == []

    def test_permanent_preserves_other_pairs_probe_view(self):
        outcomes.record("m_a", "ch1", outcomes.OutcomeKind.http_4xx_config)
        outcomes.record("m_b", "ch2", outcomes.OutcomeKind.http_5xx)
        targets = [(t.model, t.channel_id) for t in outcomes.probe_targets()]
        assert targets == [("m_b", "ch2")]

    def test_success_clears_permanent_and_degraded(self):
        """解除路径②：业务流量对该对真实 success 整条出环。"""
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_4xx_config)
        assert outcomes.probe_targets() == []
        outcomes.record("m", "ch", outcomes.OutcomeKind.success)
        assert outcomes.is_degraded("m", "ch") is False
        # success 已清除 permanent：重新失败后该对回到探活视图
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_5xx)
        (target,) = outcomes.probe_targets()
        assert (target.model, target.channel_id) == ("m", "ch")
        assert target.permanent is False

    def test_channel_change_clears_only_its_permanent_flag(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.http_4xx_config)
        outcomes.record("m", "other", outcomes.OutcomeKind.http_4xx_config)
        assert outcomes.probe_targets() == []
        outcomes.clear_permanent_for_channel("ch")
        (target,) = outcomes.probe_targets()
        assert (target.model, target.channel_id) == ("m", "ch")
        assert target.permanent is False

    @pytest.mark.parametrize(
        "kind",
        [
            outcomes.OutcomeKind.transport_failure,
            outcomes.OutcomeKind.http_5xx,
            outcomes.OutcomeKind.http_429,
            outcomes.OutcomeKind.rate_limit_exhausted,
        ],
    )
    def test_non_config_failures_do_not_set_permanent(self, kind):
        """400 等歧义 4XX 不置 permanent，维持普通失败走退避（探活视图仍产出）。"""
        outcomes.record("m", "ch", kind)
        (target,) = outcomes.probe_targets()
        assert target.permanent is False
        assert outcomes.is_degraded("m", "ch") is True


# ═══════════════════════════════════════════
#  记账键兜底链（ADR-0025 D0）：effective_model 唯一住所
# ═══════════════════════════════════════════


class TestEffectiveModel:
    @pytest.mark.parametrize(
        ("model", "body_model", "requested_model", "channel_id", "expected"),
        [
            # 兜底链逐级：前一级非空即胜出
            ("m1", "m2", "m3", "ch", "m1"),
            ("", "m2", "m3", "ch", "m2"),
            (None, "m2", "m3", "ch", "m2"),
            ("", "", "m3", "ch", "m3"),
            (None, "", "m3", "ch", "m3"),
            (None, None, "m3", "ch", "m3"),
            ("", "", "", "ch", "ch"),
            (None, None, None, "ch", "ch"),
            ("", None, None, "ch", "ch"),
        ],
    )
    def test_fallback_chain(self, model, body_model, requested_model, channel_id, expected):
        """兜底链 model or body_model or requested_model or channel_id 逐级生效。"""
        assert outcomes.effective_model(model=model, body_model=body_model, requested_model=requested_model, channel_id=channel_id) == expected

    @pytest.mark.parametrize(
        ("model", "body_model", "requested_model", "expected"),
        [
            # 全函数性：任一子集为 None/"" 恒返回 str（无 None 管道）；
            # 每例至多一个非空槽位，期望值无歧义（逐级共存见 test_fallback_chain）
            ("m", None, None, "m"),
            (None, "b", None, "b"),
            (None, None, "r", "r"),
            ("", None, None, "ch_x"),
            (None, "", None, "ch_x"),
            (None, None, "", "ch_x"),
            ("", "", "", "ch_x"),
            (None, None, None, "ch_x"),
        ],
    )
    def test_total_always_returns_str(self, model, body_model, requested_model, expected):
        """全函数：任一子集为 None/"" 恒返回 str（channel_id 兜底），无 None 管道。"""
        result = outcomes.effective_model(model=model, body_model=body_model, requested_model=requested_model, channel_id="ch_x")
        assert isinstance(result, str)
        assert result == expected


# ═══════════════════════════════════════════
#  探活候选虚拟键过滤（ADR-0025 D1-A2）
# ═══════════════════════════════════════════


class TestProbeTargetsVirtualKeyFilter:
    def test_virtual_key_model_equals_channel_excluded(self):
        """model == channel_id 的虚拟键语义即「渠道级」，不参与模型级探活回切。"""
        outcomes.record("ch1", "ch1", outcomes.OutcomeKind.http_5xx)
        assert outcomes.probe_targets() == []

    def test_empty_model_historical_dirty_key_excluded(self):
        """历史 "" 脏键同样被渠道级语义覆盖：兜底照记产生的渠道键不进探活管道。

        "" 键来源：LB 兼容外壳时代 falsy model 丢账 / 兜底照记前的残留行，
        与虚拟键同为「无真实模型」的渠道级记账，语义一致故一并排除。
        """
        outcomes.record("", "ch1", outcomes.OutcomeKind.http_5xx)
        assert outcomes.probe_targets() == []

    def test_real_model_keys_unaffected(self):
        """过滤只排虚拟键：真实模型键照常产出探活候选。"""
        outcomes.record("ch1", "ch1", outcomes.OutcomeKind.http_5xx)
        outcomes.record("", "ch1", outcomes.OutcomeKind.http_429)
        outcomes.record("m_real", "ch1", outcomes.OutcomeKind.http_5xx)
        outcomes.record("m_real", "ch2", outcomes.OutcomeKind.http_5xx)
        targets = sorted(outcomes.probe_targets(), key=lambda t: (t.model, t.channel_id))
        assert [(t.model, t.channel_id) for t in targets] == [("m_real", "ch1"), ("m_real", "ch2")]

    def test_virtual_key_success_clears_degraded_still(self):
        """虚拟键的记账 / 解除语义不变：success 仍清 degraded，只是不产出探活行。"""
        outcomes.record("ch1", "ch1", outcomes.OutcomeKind.http_5xx)
        assert outcomes.is_degraded("ch1", "ch1") is True
        outcomes.record("ch1", "ch1", outcomes.OutcomeKind.success)
        assert outcomes.is_degraded("ch1", "ch1") is False


# ═══════════════════════════════════════════
#  生命周期：渠道下线清理
# ═══════════════════════════════════════════


class TestLifecycle:
    def test_remove_channel_preserves_other_channels(self):
        for _ in range(3):
            outcomes.record("m", "ch_old", outcomes.OutcomeKind.http_5xx)
            outcomes.record("m", "ch_keep", outcomes.OutcomeKind.http_5xx)
        outcomes.remove_channel("ch_old")
        assert outcomes.is_healthy("m", "ch_old") is True
        assert outcomes.is_degraded("m", "ch_old") is False
        assert outcomes.is_healthy("m", "ch_keep") is False
        assert outcomes.is_degraded("m", "ch_keep") is True

    def test_reset_clears_blocked(self):
        outcomes.record("m", "ch", outcomes.OutcomeKind.quota_window, reset_at=time.time() + 3600)
        assert outcomes.is_blocked("ch") is True
        outcomes.reset()
        assert outcomes.is_blocked("ch") is False
