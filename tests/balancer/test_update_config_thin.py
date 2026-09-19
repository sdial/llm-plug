"""ADR-0025 D2 — update_config 薄化 + 两跳接线 + import 顺序解耦 回归。

验收来源：issue 03 AC 三条 + ADR-0025 D2 测试节。

只测外部行为（公共接口可观察结果），不测实现细节；每个新住所配直测缝 + 防回滑守卫。
"""

import pytest

from balancer.load_balancer import LoadBalancer
from models.channel import Channel, Endpoint
from proxy import outcomes

MODEL = "gpt-4"


@pytest.fixture(autouse=True)
def _reset_outcomes():
    outcomes.reset()
    yield
    outcomes.reset()


def _make_channel(id: str) -> Channel:
    return Channel(
        id=id,
        name=f"Channel {id}",
        endpoints=[Endpoint(api_type="openai-chat-completions", base_url="http://example.com")],
        api_key="key",
        models=[MODEL],
        enabled=True,
        weight=1,
        priority=1,
    )


# ── LB 热更新直测 ───────────────────────────────────────────────


class TestLBHotUpdateTwoHops:
    @pytest.mark.asyncio
    async def test_strategy_change_triggers_clear(self):
        lb = LoadBalancer()
        await lb.update_config(strategy="sticky", sticky_ttl=1800, sticky_cache_max_entries=10000)
        chs = [_make_channel("a"), _make_channel("b")]
        # 产生一条会话粘滞条目
        await lb.select_channel(chs, model=MODEL, client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        key = lb._build_session_fingerprint(client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        assert outcomes.session_sticky_get(key) is not None

        await lb.update_config(strategy="round_robin", sticky_ttl=1800, sticky_cache_max_entries=10000)
        assert outcomes.session_sticky_get(key) is None

    @pytest.mark.asyncio
    async def test_ttl_change_triggers_clear(self):
        lb = LoadBalancer()
        await lb.update_config(strategy="sticky", sticky_ttl=1800, sticky_cache_max_entries=10000)
        chs = [_make_channel("a"), _make_channel("b")]
        await lb.select_channel(chs, model=MODEL, client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        key = lb._build_session_fingerprint(client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        assert outcomes.session_sticky_get(key) is not None

        await lb.update_config(strategy="sticky", sticky_ttl=900, sticky_cache_max_entries=10000)
        assert outcomes.session_sticky_get(key) is None

    @pytest.mark.asyncio
    async def test_no_change_triggers_trim_not_clear(self):
        lb = LoadBalancer()
        await lb.update_config(strategy="sticky", sticky_ttl=1800, sticky_cache_max_entries=10000)
        chs = [_make_channel("a"), _make_channel("b")]
        await lb.select_channel(chs, model=MODEL, client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        key = lb._build_session_fingerprint(client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        assert outcomes.session_sticky_get(key) is not None

        # 同参更新：应走 trim 而非 clear，条目保留
        await lb.update_config(strategy="sticky", sticky_ttl=1800, sticky_cache_max_entries=10000)
        assert outcomes.session_sticky_get(key) is not None

    @pytest.mark.asyncio
    async def test_max_entries_change_triggers_trim_not_clear(self):
        lb = LoadBalancer()
        await lb.update_config(strategy="sticky", sticky_ttl=1800, sticky_cache_max_entries=10000)
        chs = [_make_channel("a"), _make_channel("b")]
        await lb.select_channel(chs, model=MODEL, client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        key = lb._build_session_fingerprint(client_ip="1.1.1.1", api_key_id="k", client_headers={"x-session-id": "s1"})
        assert outcomes.session_sticky_get(key) is not None

        # 仅 max_entries 变更：不应清缓存（trim）
        await lb.update_config(strategy="sticky", sticky_ttl=1800, sticky_cache_max_entries=2)
        assert outcomes.session_sticky_get(key) is not None
        # max_entries 已透传 outcomes
        assert outcomes._session_sticky_max_entries == 2

    @pytest.mark.asyncio
    async def test_threshold_change_direct_to_outcomes(self):
        """阈值改动应直达 outcomes 立即生效（不经 LB 中转）。"""
        # 初始阈值 3/120
        outcomes.configure(max_fail_count=3, cooldown_seconds=120)
        for _ in range(3):
            outcomes.record(MODEL, "a", outcomes.OutcomeKind.http_5xx)
        # 3 次达阈值，不健康
        assert outcomes.is_healthy(MODEL, "a") is False

        # 直调 outcomes 提高阈值到 5，应立即恢复（fail_count 3 < 5）
        outcomes.configure(max_fail_count=5, cooldown_seconds=120)
        assert outcomes.is_healthy(MODEL, "a") is True

        # 且 LB 自身不再持有阈值字段（瘦身）
        lb = LoadBalancer()
        assert not hasattr(lb, "_max_fail_count")
        assert not hasattr(lb, "_cooldown_seconds")
        assert not hasattr(lb, "_sticky_cache_max_entries")
        # 仅保留 _strategy / _sticky_ttl
        assert hasattr(lb, "_strategy")
        assert hasattr(lb, "_sticky_ttl")

    @pytest.mark.asyncio
    async def test_update_config_retains_strategy_validation(self):
        lb = LoadBalancer()
        with pytest.raises(ValueError, match="lb_strategy"):
            await lb.update_config(strategy="random", sticky_ttl=1800, sticky_cache_max_entries=10000)

    @pytest.mark.asyncio
    async def test_update_config_signature_is_three_params(self):
        import inspect

        sig = inspect.signature(LoadBalancer.update_config)
        params = [p for p in sig.parameters if p != "self"]
        assert params == ["strategy", "sticky_ttl", "sticky_cache_max_entries"]


# ── import 顺序无关性 ───────────────────────────────────────────


class TestImportOrderIndependence:
    def test_import_does_not_freeze_threshold(self):
        """不调 init_settings 直接 import 全链，阈值不被硬编码冻结；动态兜底生效。"""
        import config as cfg

        # 隔离 outcomes
        outcomes.reset()
        # 模拟 data/settings.json 中用户把阈值改成非默认值
        original = dict(cfg._settings)
        try:
            cfg._settings["max_fail_count"] = 9
            cfg._settings["cooldown_seconds"] = 999
            # outcomes 未显式 configure 时应读 config 动态兜底
            assert outcomes._max_fail_count() == 9
            assert outcomes._cooldown_seconds() == 999.0

            # 此时 import/实例化 LB 不应覆盖阈值（曾用硬编码 3/120）
            lb2 = LoadBalancer()
            assert outcomes._max_fail_count() == 9
            assert outcomes._cooldown_seconds() == 999.0
            # LB 不应再持有阈值字段
            assert not hasattr(lb2, "_max_fail_count")
        finally:
            cfg._settings.clear()
            cfg._settings.update(original)
            outcomes.reset()
            outcomes.configure(max_fail_count=3, cooldown_seconds=120)


# ── 启动路径回归 ───────────────────────────────────────────────


class TestStartupWiring:
    @pytest.mark.asyncio
    async def test_apply_lb_settings_two_hops(self):
        """config._apply_lb_settings 应两跳：阈值直调 outcomes，策略走 update_config。"""
        import config as cfg

        original = dict(cfg._settings)
        try:
            cfg._settings["max_fail_count"] = 7
            cfg._settings["cooldown_seconds"] = 77
            cfg._settings["lb_strategy"] = "sticky"
            cfg._settings["sticky_ttl"] = 600
            cfg._settings["sticky_cache_max_entries"] = 123
            outcomes.reset()
            await cfg._apply_lb_settings()
            # 阈值已直达 outcomes
            assert outcomes._max_fail_count() == 7
            assert outcomes._cooldown_seconds() == 77.0
            # 策略/粘滞已走 LB（outcomes 侧可见）
            assert outcomes._session_sticky_ttl == 600.0
            assert outcomes._session_sticky_max_entries == 123
            from balancer.load_balancer import load_balancer as global_lb

            assert global_lb._strategy == "sticky"
            assert global_lb._sticky_ttl == 600.0
            assert not hasattr(global_lb, "_sticky_cache_max_entries")
        finally:
            cfg._settings.clear()
            cfg._settings.update(original)
            outcomes.reset()
            outcomes.configure(max_fail_count=3, cooldown_seconds=120)
            # 恢复全局 LB 策略以免污染其他用例
            from balancer.load_balancer import load_balancer as global_lb

            await global_lb.update_config(strategy="round_robin", sticky_ttl=1800, sticky_cache_max_entries=10000)

    @pytest.mark.asyncio
    async def test_init_settings_two_hops(self, tmp_path, monkeypatch):
        """init_settings 启动路径同样两跳；且不依赖 LB 中转阈值。"""
        import json
        import os

        import config as cfg

        # 构造临时 settings.json
        settings_data = {
            "max_fail_count": 8,
            "cooldown_seconds": 88,
            "lb_strategy": "backup",
            "sticky_ttl": 700,
            "sticky_cache_max_entries": 321,
        }
        # 使用真实临时目录隔离 DATA_DIR
        fake_data_dir = str(tmp_path / "data")
        os.makedirs(fake_data_dir, exist_ok=True)
        fake_settings_file = os.path.join(fake_data_dir, "settings.json")
        with open(fake_settings_file, "w", encoding="utf-8") as f:
            json.dump(settings_data, f)
        monkeypatch.setattr(cfg, "DATA_DIR", fake_data_dir)
        monkeypatch.setattr(cfg, "_SETTINGS_FILE", fake_settings_file)
        # 同时让 outcomes 的 config 读取指向同一目录（outcomes 已通过 config.get_setting 动态读）
        outcomes.reset()
        # 初始化
        await cfg.init_settings()
        # 阈值直达
        assert outcomes._max_fail_count() == 8
        assert outcomes._cooldown_seconds() == 88.0
        # 策略走 LB
        from balancer.load_balancer import load_balancer as global_lb

        assert global_lb._strategy == "backup"
        assert global_lb._sticky_ttl == 700.0
        assert outcomes._session_sticky_ttl == 700.0
        assert outcomes._session_sticky_max_entries == 321
        # 清理
        outcomes.reset()
        await global_lb.update_config(strategy="round_robin", sticky_ttl=1800, sticky_cache_max_entries=10000)
