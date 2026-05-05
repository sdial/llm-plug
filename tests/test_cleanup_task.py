import asyncio
from unittest.mock import AsyncMock, patch


def test_cleanup_loop_calls_cleanup():
    """清理循环调用 _cleanup_if_needed"""
    from main import _session_cleanup_loop

    with patch("main._responses_store") as mock_store, \
         patch("main.get_setting", return_value=0.001):
        mock_store._cleanup_if_needed = AsyncMock()

        async def run_cleanup():
            task = asyncio.create_task(_session_cleanup_loop())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_cleanup())
        mock_store._cleanup_if_needed.assert_called()
