import asyncio
import pytest
from unittest.mock import AsyncMock, patch


def test_load_history_returns_empty_for_none():
    """load_history 返回空列表当 previous_response_id 为 None"""
    from proxy_core import _load_history

    result = asyncio.run(_load_history(None))
    assert result == []


def test_load_history_raises_for_missing():
    """load_history 抛出 404 当 previous_response_id 不存在"""
    from proxy_core import _load_history

    with patch("proxy_core._responses_store") as mock_store:
        mock_store.get_conversation = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="not found"):
            asyncio.run(_load_history("resp_nonexistent"))


def test_save_response_state_calls_store():
    """_save_response_state 调用 store.put"""
    from proxy_core import _save_response_state

    with patch("proxy_core._responses_store") as mock_store:
        mock_store.put = AsyncMock()
        messages = [{"role": "user", "content": "hi"}]
        response = {"id": "resp_123", "output": []}
        asyncio.run(_save_response_state("resp_123", messages, response))
        mock_store.put.assert_called_once()
