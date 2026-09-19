# tests/test_config_group_probe.py
"""Ticket 04 — 探活节拍配置后端（schema / 数值约束 / 软约束告警）。"""

from unittest.mock import AsyncMock

import pytest

from config import _CONFIG_CONSTRAINTS, _CONFIG_SCHEMA, get_setting

GROUP_PROBE_KEYS = (
    "group_probe_interval_seconds",
    "group_probe_concurrency",
    "group_probe_timeout",
)


def test_group_probe_schema_entries():
    for key in GROUP_PROBE_KEYS:
        assert key in _CONFIG_SCHEMA, f"{key} 缺失"
        entry = _CONFIG_SCHEMA[key]
        assert entry["type"] == "int"
        assert entry["requires_restart"] is False


def test_group_probe_defaults():
    assert get_setting("group_probe_interval_seconds") == 60
    assert get_setting("group_probe_concurrency") == 5
    assert get_setting("group_probe_timeout") == 10


def test_group_probe_constraints_bounds():
    assert _CONFIG_CONSTRAINTS["group_probe_interval_seconds"] == {"min": 1, "max": 86400}
    assert _CONFIG_CONSTRAINTS["group_probe_concurrency"] == {"min": 1, "max": 100}
    assert _CONFIG_CONSTRAINTS["group_probe_timeout"] == {"min": 1, "max": 300}


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """把 config 隔离到独立 settings 文件，避免污染共享状态。"""
    import config

    monkeypatch.setattr(config, "_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "_settings", {})
    monkeypatch.setattr(config, "_save_settings_to_disk", AsyncMock())
    monkeypatch.setattr(config, "_apply_lb_settings", AsyncMock())
    return config


@pytest.mark.anyio
async def test_group_probe_hot_update(isolated_config):
    config = isolated_config

    result = await config.update_settings(
        {
            "group_probe_interval_seconds": 30,
            "group_probe_concurrency": 8,
            "group_probe_timeout": 5,
        }
    )

    assert set(result["updated"]) == set(GROUP_PROBE_KEYS)
    assert result["needs_restart"] is False
    assert result["warnings"] == []
    # 热生效：内存值已更新
    assert config.get_setting("group_probe_interval_seconds") == 30
    assert config.get_setting("group_probe_concurrency") == 8
    assert config.get_setting("group_probe_timeout") == 5


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("updates",),
    [
        ({"group_probe_interval_seconds": 0},),
        ({"group_probe_interval_seconds": 86401},),
        ({"group_probe_concurrency": 0},),
        ({"group_probe_concurrency": 101},),
        ({"group_probe_timeout": 0},),
        ({"group_probe_timeout": 301},),
    ],
)
async def test_group_probe_out_of_bounds_rejected(isolated_config, updates):
    with pytest.raises(ValueError, match="must be"):
        await isolated_config.update_settings(updates)


@pytest.mark.anyio
async def test_interval_over_cooldown_allowed_with_warning(isolated_config):
    """软约束违反：interval > cooldown 时保存允许但返回 warn。"""
    config = isolated_config

    result = await config.update_settings({"group_probe_interval_seconds": 300})

    assert "group_probe_interval_seconds" in result["updated"]
    assert config.get_setting("group_probe_interval_seconds") == 300  # 未被硬拒绝
    assert len(result["warnings"]) == 1
    assert "cooldown_seconds" in result["warnings"][0]
    assert "group_probe_interval_seconds" in result["warnings"][0]


@pytest.mark.anyio
async def test_cooldown_lowered_below_interval_warns(isolated_config):
    """把 cooldown 调小到 interval 之下同样触发软约束告警（不变量双向成立）。"""
    config = isolated_config

    result = await config.update_settings({"cooldown_seconds": 30})

    assert config.get_setting("cooldown_seconds") == 30  # 保存允许
    assert len(result["warnings"]) == 1


@pytest.mark.anyio
async def test_interval_within_cooldown_no_warning(isolated_config):
    config = isolated_config

    result = await config.update_settings({"group_probe_interval_seconds": 30, "cooldown_seconds": 120})

    assert result["warnings"] == []


@pytest.mark.anyio
async def test_raising_cooldown_restores_invariant_no_warning(isolated_config):
    """预置违反状态后把 cooldown 调大，不变量恢复 → 无告警（合并视图评估）。"""
    config = isolated_config
    config._settings = {
        "group_probe_interval_seconds": 300,
        "cooldown_seconds": 120,
    }

    result = await config.update_settings({"cooldown_seconds": 500})

    assert result["warnings"] == []
    assert config.get_setting("group_probe_interval_seconds") == 300
    assert config.get_setting("cooldown_seconds") == 500


@pytest.mark.anyio
async def test_unrelated_update_does_not_warn_on_pre_existing_violation(isolated_config):
    """历史违反状态下保存无关配置不重复告警——软约束只在触及相关键时评估。"""
    config = isolated_config
    config._settings = {
        "group_probe_interval_seconds": 300,
        "cooldown_seconds": 120,
    }

    result = await config.update_settings({"request_timeout": 60})

    assert result["warnings"] == []
