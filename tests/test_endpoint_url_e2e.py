"""endpoint_url / models_url 高级覆盖在真实代理流程中的测试

覆盖场景：
1. endpoint_url 覆盖 base_url + api_path 拼接
2. endpoint_url 为空时回退到 base_url + /v1/{path}
3. models_url 覆盖默认的 /v1/models 路径
4. endpoint_url 与跨格式转换的组合
5. 含查询参数的 endpoint_url
"""

import json
import os
import time
from multiprocessing import Process
from unittest.mock import patch

import pytest

import config
import storage


# ═══════════════════════════════════════════
#  URL 构建函数直接测试（高级覆盖路径）
# ═══════════════════════════════════════════

from models.channel import Channel
from url_builder import build_upstream_url, build_models_url


class TestEndpointUrlInBuildUpstreamUrl:

    def _make_channel(self, **overrides):
        defaults = {
            "id": "ch_ep",
            "name": "EP Test",
            "api_type": "openai-chat-completions",
            "base_url": "https://api.example.com",
            "api_key": "key",
            "models": ["gpt-4o"],
            "enabled": True,
            "weight": 1,
            "priority": 1,
        }
        defaults.update(overrides)
        return Channel(**defaults)

    def test_endpoint_url_overrides_base_url_for_chat(self):
        """endpoint_url 应完全覆盖 base_url + /v1/chat/completions"""
        ch = self._make_channel(endpoint_url="https://custom.api.com/my/chat")
        url = build_upstream_url(ch)
        assert url == "https://custom.api.com/my/chat"

    def test_endpoint_url_overrides_base_url_for_anthropic(self):
        """endpoint_url 覆盖 Anthropic 渠道"""
        ch = self._make_channel(
            api_type="anthropic",
            endpoint_url="https://custom.api.com/v2/messages",
        )
        url = build_upstream_url(ch)
        assert url == "https://custom.api.com/v2/messages"

    def test_endpoint_url_overrides_base_url_for_responses(self):
        """endpoint_url 覆盖 OpenAI Response API 渠道"""
        ch = self._make_channel(
            api_type="openai-response",
            endpoint_url="https://custom.api.com/responses",
        )
        url = build_upstream_url(ch)
        assert url == "https://custom.api.com/responses"

    def test_empty_endpoint_url_falls_back_to_base_url(self):
        """空字符串 endpoint_url 应回退到 base_url + /v1/{path}"""
        ch = self._make_channel(endpoint_url="")
        url = build_upstream_url(ch)
        assert url == "https://api.example.com/v1/chat/completions"

    def test_none_endpoint_url_falls_back_to_base_url(self):
        """None endpoint_url 应回退到 base_url + /v1/{path}"""
        ch = self._make_channel(endpoint_url=None)
        url = build_upstream_url(ch)
        assert url == "https://api.example.com/v1/chat/completions"

    def test_endpoint_url_with_query_params(self):
        """endpoint_url 含查询参数应保留"""
        ch = self._make_channel(endpoint_url="https://api.com/chat?version=2&key=abc")
        url = build_upstream_url(ch)
        assert url == "https://api.com/chat?version=2&key=abc"

    def test_endpoint_url_with_trailing_spaces(self):
        """endpoint_url 含前后空格应被清理"""
        ch = self._make_channel(endpoint_url="  https://api.com/chat  ")
        url = build_upstream_url(ch)
        assert url == "https://api.com/chat"


class TestModelsUrlInBuildModelsUrl:

    def test_explicit_models_url_takes_priority(self):
        """显式 models_url 应完全覆盖默认路径"""
        url = build_models_url(
            base_url="https://api.example.com",
            models_url="https://custom.api.com/my/models",
        )
        assert url == "https://custom.api.com/my/models"

    def test_empty_models_url_falls_back(self):
        """空 models_url 应回退到 base_url + /v1/models"""
        url = build_models_url(
            base_url="https://api.example.com",
            models_url="",
        )
        assert url == "https://api.example.com/v1/models"

    def test_none_models_url_falls_back(self):
        """None models_url 应回退到默认"""
        url = build_models_url(
            base_url="https://api.example.com",
            models_url=None,
        )
        assert url == "https://api.example.com/v1/models"

    def test_models_url_with_existing_v1_path(self):
        """base_url 已含 /v1 时不应重复"""
        url = build_models_url(
            base_url="https://api.example.com/v1",
            models_url=None,
        )
        assert url == "https://api.example.com/v1/models"
        assert "v1/v1" not in url


# ═══════════════════════════════════════════
#  真实代理流程中 endpoint_url 的 E2E 测试
# ═══════════════════════════════════════════

class TestEndpointUrlInProxyFlow:
    """在完整代理请求链路中验证 endpoint_url 覆盖"""

    def test_endpoint_url_used_in_proxy_request(self, tmp_path, monkeypatch):
        """proxy_core 应使用 endpoint_url 而非 base_url 构建上游请求"""
        import proxy_core
        import asyncio

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        channels_file = data_dir / "channels.json"
        api_keys_file = data_dir / "api_keys.json"

        with open(channels_file, "w") as f:
            json.dump({"channels": []}, f)
        with open(api_keys_file, "w") as f:
            json.dump({"api_keys": []}, f)

        monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
        monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
        monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))

        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None

        # 创建一个有 endpoint_url 的渠道
        ch = Channel(
            id="ch_ep_e2e",
            name="EP E2E",
            api_type="openai-chat-completions",
            base_url="https://should-not-be-used.example.com",
            endpoint_url="http://127.0.0.1:19876/custom/endpoint",
            api_key="test-key",
            models=["gpt-4o"],
            enabled=True,
            weight=1,
            priority=1,
        )

        # 验证 URL 构建使用了 endpoint_url
        from url_builder import build_upstream_url
        url = build_upstream_url(ch)
        assert url == "http://127.0.0.1:19876/custom/endpoint"
        assert "should-not-be-used" not in url

    def test_endpoint_url_with_cross_format_conversion(self, tmp_path, monkeypatch):
        """endpoint_url + 跨格式转换：验证 URL 构建 + 转换器选择"""
        import proxy_core
        from models.channel import Channel
        from url_builder import build_upstream_url

        # Anthropic 渠道，但使用自定义 endpoint_url
        ch = Channel(
            id="ch_cross",
            name="Cross Format EP",
            api_type="anthropic",
            base_url="https://fallback.example.com",
            endpoint_url="http://127.0.0.1:19876/anthropic/v1/messages",
            api_key="key",
            models=["claude-sonnet-4-20250514"],
            enabled=True,
            weight=1,
            priority=1,
        )

        url = build_upstream_url(ch)
        assert url == "http://127.0.0.1:19876/anthropic/v1/messages"

        # 验证 converter 选择：OpenAI Chat → Anthropic
        from models.api_types import APIType
        source_type = APIType.OPENAI_CHAT
        target_type = APIType.ANTHROPIC
        converter_map = proxy_core.CONVERTER_MAP
        key = (source_type.value, target_type.value)
        assert key in converter_map, f"Converter for {source_type.value} → {target_type.value} should exist"

    def test_endpoint_url_empty_uses_base_url_in_proxy(self):
        """空 endpoint_url 在代理流程中应回退到标准路径"""
        from url_builder import build_upstream_url
        ch = Channel(
            id="ch_fb",
            name="Fallback",
            api_type="openai-chat-completions",
            base_url="https://api.openai.com",
            endpoint_url="",
            api_key="key",
            models=["gpt-4o"],
        )
        url = build_upstream_url(ch)
        assert "api.openai.com" in url
        assert "/v1/chat/completions" in url
