"""跨格式转换开关（全局 + 渠道级）的单元/集成测试。"""
from unittest.mock import patch, AsyncMock

import pytest

from models.api_types import APIType
from models.channel import Channel
from proxy_core import (
    _filter_channels_by_conversion,
    _proxy_single_model_request,
)


def _mk(id_: str, api_type: APIType, allow=None) -> Channel:
    return Channel(
        id=id_,
        name=id_,
        api_type=api_type,
        base_url=f"https://{id_}.example",
        api_key="k",
        models=["m"],
        allow_format_conversion=allow,
    )


class TestFilterChannelsByConversion:
    def test_same_format_always_passes(self):
        ch = _mk("ch_same", APIType.ANTHROPIC)
        with patch("proxy_core.get_setting", return_value=False):
            out = _filter_channels_by_conversion([ch], APIType.ANTHROPIC)
        assert [c.id for c in out] == ["ch_same"]

    def test_cross_format_blocked_when_global_disabled(self):
        ch = _mk("ch_cross", APIType.OPENAI_CHAT)
        with patch("proxy_core.get_setting", return_value=False):
            out = _filter_channels_by_conversion([ch], APIType.ANTHROPIC)
        assert out == []

    def test_cross_format_allowed_when_global_enabled(self):
        ch = _mk("ch_cross", APIType.OPENAI_CHAT)
        with patch("proxy_core.get_setting", return_value=True):
            out = _filter_channels_by_conversion([ch], APIType.ANTHROPIC)
        assert [c.id for c in out] == ["ch_cross"]

    def test_channel_true_overrides_global_false(self):
        ch = _mk("ch_cross", APIType.OPENAI_CHAT, allow=True)
        with patch("proxy_core.get_setting", return_value=False):
            out = _filter_channels_by_conversion([ch], APIType.ANTHROPIC)
        assert [c.id for c in out] == ["ch_cross"]

    def test_channel_false_overrides_global_true(self):
        ch = _mk("ch_cross", APIType.OPENAI_CHAT, allow=False)
        with patch("proxy_core.get_setting", return_value=True):
            out = _filter_channels_by_conversion([ch], APIType.ANTHROPIC)
        assert out == []

    def test_mixed_same_and_cross_format(self):
        same = _mk("same", APIType.ANTHROPIC)
        cross_blocked = _mk("blocked", APIType.OPENAI_CHAT, allow=False)
        cross_allowed = _mk("allowed", APIType.OPENAI_RESPONSE, allow=True)
        cross_inherit = _mk("inherit", APIType.OPENAI_CHAT)  # follows global
        with patch("proxy_core.get_setting", return_value=False):
            out = _filter_channels_by_conversion(
                [same, cross_blocked, cross_allowed, cross_inherit],
                APIType.ANTHROPIC,
            )
        assert sorted(c.id for c in out) == ["allowed", "same"]

    def test_get_setting_returns_none_defaults_to_allowed(self):
        # If config returns None (unlikely but safe), default to allowed (backward compat).
        ch = _mk("ch_cross", APIType.OPENAI_CHAT)
        with patch("proxy_core.get_setting", return_value=None):
            out = _filter_channels_by_conversion([ch], APIType.ANTHROPIC)
        assert [c.id for c in out] == ["ch_cross"]


class TestProxySingleModelRequestErrorMessages:
    @pytest.mark.anyio
    async def test_raises_clear_error_when_all_channels_filtered(self):
        cross = _mk("ch_cross", APIType.OPENAI_CHAT)
        with (
            patch(
                "proxy_core._get_channels_for_model",
                new_callable=AsyncMock,
                return_value=[cross],
            ),
            patch("proxy_core.get_setting", return_value=False),
        ):
            with pytest.raises(ValueError) as exc:
                await _proxy_single_model_request(
                    model="m",
                    request_data={"model": "m"},
                    target_api_type=APIType.ANTHROPIC,
                    is_stream=False,
                    query_string=None,
                    client_headers=None,
                    api_key_id=None,
                    client_ip=None,
                )
        msg = str(exc.value)
        assert "禁止跨格式转换" in msg
        assert "anthropic" in msg

    @pytest.mark.anyio
    async def test_no_channel_at_all_still_raises_original_error(self):
        with patch(
            "proxy_core._get_channels_for_model",
            new_callable=AsyncMock,
            return_value=[],
        ):
            with pytest.raises(ValueError) as exc:
                await _proxy_single_model_request(
                    model="m",
                    request_data={"model": "m"},
                    target_api_type=APIType.ANTHROPIC,
                    is_stream=False,
                    query_string=None,
                    client_headers=None,
                    api_key_id=None,
                    client_ip=None,
                )
        assert "没有可用渠道支持模型" in str(exc.value)
