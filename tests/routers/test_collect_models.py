"""P1-9: routers.proxy_models._collect_models 模型聚合逻辑直接测试"""

import json

import pytest

from routers.proxy_models import _collect_models
import storage


@pytest.fixture(autouse=True)
def _reset_storage():
    """每个测试清除 storage 缓存"""
    storage._cache = None
    storage._cache_ts = 0
    storage._channels_lock = None
    yield
    storage._cache = None
    storage._cache_ts = 0


class TestCollectModels:
    @pytest.mark.asyncio
    async def test_empty_channels(self, tmp_path, monkeypatch):
        """无渠道时返回空列表"""
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(json.dumps({"channels": []}))
        monkeypatch.setattr("config.CHANNELS_FILE", str(channels_file))

        models = await _collect_models()
        assert models == []

    @pytest.mark.asyncio
    async def test_disabled_channels_excluded(self, tmp_path, monkeypatch):
        """已禁用渠道的模型不应出现在结果中"""
        data = {
            "channels": [
                {
                    "id": "ch_on",
                    "name": "On",
                    "api_type": "openai-chat-completions",
                    "base_url": "http://a.com",
                    "api_key": "k",
                    "models": ["gpt-4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
                {
                    "id": "ch_off",
                    "name": "Off",
                    "api_type": "anthropic",
                    "base_url": "http://b.com",
                    "api_key": "k",
                    "models": ["claude-3"],
                    "enabled": False,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(json.dumps(data))
        monkeypatch.setattr("config.CHANNELS_FILE", str(channels_file))

        models = await _collect_models()
        ids = [m["id"] for m in models]
        assert "gpt-4" in ids
        assert "claude-3" not in ids

    @pytest.mark.asyncio
    async def test_deduplication(self, tmp_path, monkeypatch):
        """多个渠道有相同模型时，结果中去重"""
        data = {
            "channels": [
                {
                    "id": "ch_a",
                    "name": "A",
                    "api_type": "openai-chat-completions",
                    "base_url": "http://a.com",
                    "api_key": "k",
                    "models": ["gpt-4", "gpt-3.5"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
                {
                    "id": "ch_b",
                    "name": "B",
                    "api_type": "openai-chat-completions",
                    "base_url": "http://b.com",
                    "api_key": "k",
                    "models": ["gpt-4", "gpt-4o"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(json.dumps(data))
        monkeypatch.setattr("config.CHANNELS_FILE", str(channels_file))

        models = await _collect_models()
        ids = [m["id"] for m in models]
        assert ids.count("gpt-4") == 1
        assert "gpt-3.5" in ids
        assert "gpt-4o" in ids

    @pytest.mark.asyncio
    async def test_first_channel_wins_for_duplicates(self, tmp_path, monkeypatch):
        """去重时保留第一个出现的渠道的 api_type"""
        data = {
            "channels": [
                {
                    "id": "ch_first",
                    "name": "First",
                    "api_type": "openai-chat-completions",
                    "base_url": "http://a.com",
                    "api_key": "k",
                    "models": ["shared-model"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
                {
                    "id": "ch_second",
                    "name": "Second",
                    "api_type": "anthropic",
                    "base_url": "http://b.com",
                    "api_key": "k",
                    "models": ["shared-model"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(json.dumps(data))
        monkeypatch.setattr("config.CHANNELS_FILE", str(channels_file))

        models = await _collect_models()
        assert len(models) == 1
        assert models[0]["api_type"] == "openai-chat-completions"

    @pytest.mark.asyncio
    async def test_api_type_is_value_not_enum(self, tmp_path, monkeypatch):
        """返回的 api_type 应为字符串值"""
        data = {
            "channels": [
                {
                    "id": "ch_x",
                    "name": "X",
                    "api_type": "anthropic",
                    "base_url": "http://x.com",
                    "api_key": "k",
                    "models": ["claude-3"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(json.dumps(data))
        monkeypatch.setattr("config.CHANNELS_FILE", str(channels_file))

        models = await _collect_models()
        assert models[0]["api_type"] == "anthropic"

    @pytest.mark.asyncio
    async def test_multiple_models_per_channel(self, tmp_path, monkeypatch):
        """单个渠道多个模型全部被收集"""
        data = {
            "channels": [
                {
                    "id": "ch_multi",
                    "name": "Multi",
                    "api_type": "openai-chat-completions",
                    "base_url": "http://a.com",
                    "api_key": "k",
                    "models": ["m1", "m2", "m3", "m4"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(json.dumps(data))
        monkeypatch.setattr("config.CHANNELS_FILE", str(channels_file))

        models = await _collect_models()
        assert len(models) == 4
        ids = {m["id"] for m in models}
        assert ids == {"m1", "m2", "m3", "m4"}

    @pytest.mark.asyncio
    async def test_channel_with_empty_models(self, tmp_path, monkeypatch):
        """渠道 models 为空列表时不贡献任何模型"""
        data = {
            "channels": [
                {
                    "id": "ch_empty",
                    "name": "Empty",
                    "api_type": "openai-chat-completions",
                    "base_url": "http://a.com",
                    "api_key": "k",
                    "models": [],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                },
            ]
        }
        channels_file = tmp_path / "channels.json"
        channels_file.write_text(json.dumps(data))
        monkeypatch.setattr("config.CHANNELS_FILE", str(channels_file))

        models = await _collect_models()
        assert models == []
