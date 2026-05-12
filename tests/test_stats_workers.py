import pytest

import stats


@pytest.mark.anyio
async def test_start_after_stop_recreates_queue_for_current_loop():
    stats.start_stats_workers()
    first_queue = stats._STATS_QUEUE
    await stats.stop_stats_workers()

    stats.start_stats_workers()
    second_queue = stats._STATS_QUEUE
    await stats.stop_stats_workers()

    assert first_queue is not second_queue
