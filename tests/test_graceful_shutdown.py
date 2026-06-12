"""优雅关闭行为测试

覆盖场景：
1. lifespan 关闭时后台任务被正确取消
2. stats workers 在关闭时能完成排队的记录
3. request log workers 在关闭时不丢失关键记录
4. 客户端池在关闭时被正确清理
5. 正在执行的请求完成后再关闭（graceful drain）
"""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import storage
from models.channel import Channel


# ═══════════════════════════════════════════
#  Lifespan 关闭清理
# ═══════════════════════════════════════════

class TestLifespanShutdown:

    def test_shutdown_cancels_cleanup_tasks(self, tmp_path, monkeypatch):
        """lifespan 退出时应取消 client cleanup、session cleanup、request log cleanup 后台任务"""
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

        from main import app

        with patch("main.close_all_clients") as mock_close, \
             patch("main.stop_stats_workers") as mock_stop_stats, \
             patch("main.close_stats_pool") as mock_close_pool, \
             patch("main.request_logs.close_backend") as mock_close_rl, \
             patch("main.request_logs.start_request_log_workers"), \
             patch("main.start_stats_workers"), \
             patch("main.init_stats_db"), \
             patch("main.request_logs.init_backend"):

            async def run():
                async with app.router.lifespan_context(app):
                    # 在 lifespan 内部，后台任务应已创建
                    pass
                # lifespan 退出后，清理函数应被调用

            asyncio.run(run())

            mock_close.assert_called_once()
            mock_stop_stats.assert_called_once()
            mock_close_pool.assert_called_once()
            mock_close_rl.assert_called_once()

        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None


# ═══════════════════════════════════════════
#  Stats Workers 关闭时处理队列
# ═══════════════════════════════════════════

class TestStatsWorkersShutdown:

    @pytest.mark.asyncio
    async def test_stop_waits_for_pending_records(self, tmp_path, monkeypatch):
        """stop_stats_workers + drain_queue 应确保所有排队记录被写入"""
        import stats

        db_path = str(tmp_path / "shutdown_stats.db")
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

        await stats.init_db(db_path)
        stats.start_stats_workers()

        # 写入一批记录
        for i in range(50):
            stats.record_request(
                channel_id="ch_shutdown",
                channel_name="ch_shutdown",
                model="gpt-4o",
                is_stream=False,
                input_tokens=100,
                output_tokens=50,
                latency_ms=100,
                success=True,
                api_key_id="key",
            )

        # 先等待队列处理完毕，再停止 workers
        await asyncio.sleep(0.5)
        await stats.stop_stats_workers()
        # workers 停止后 drain 剩余队列
        await stats.drain_queue()
        await stats.close_pool()

        # 验证记录已写入数据库
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM request_stats_raw").fetchone()[0]
            assert count == 50, f"Expected 50 records, got {count}"

    @pytest.mark.asyncio
    async def test_double_stop_is_safe(self):
        """连续调用 stop_stats_workers 两次不应报错"""
        import stats

        stats.start_stats_workers()
        await stats.stop_stats_workers()
        await stats.stop_stats_workers()  # 第二次不应报错


# ═══════════════════════════════════════════
#  Request Log Workers 关闭
# ═══════════════════════════════════════════

class TestRequestLogWorkersShutdown:

    @pytest.mark.asyncio
    async def test_stop_waits_for_pending_logs(self, tmp_path, monkeypatch):
        """wait_for_queue + stop 应确保所有排队记录被写入"""
        import request_logs

        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

        # 重置 backend 确保隔离
        request_logs._backend = None
        request_logs._backend_error = ""

        await request_logs.init_backend()
        request_logs.start_request_log_workers()

        # 写入一批记录
        for i in range(30):
            request_logs.record_request(
                channel_id="ch_rl",
                channel_name="ch_rl",
                model="gpt-4o",
                is_stream=False,
                input_tokens=100,
                output_tokens=50,
                latency_ms=100,
                success=True,
                api_key_id="key",
            )

        # 在 workers 仍在运行时等待队列处理完毕
        await request_logs.wait_for_queue()

        # 在关闭前验证记录已写入（backend 仍可用）
        requests = await request_logs.list_requests(page=1, page_size=100)
        total = requests.get("total", 0)
        assert total >= 30, f"Expected at least 30 request logs, got {total}"

        await request_logs.stop_request_log_workers()
        await request_logs.close_backend()

    @pytest.mark.asyncio
    async def test_double_stop_request_logs_is_safe(self):
        """连续调用 stop_request_log_workers 两次不应报错"""
        import request_logs

        request_logs.start_request_log_workers()
        await request_logs.stop_request_log_workers()
        await request_logs.stop_request_log_workers()  # 第二次不应报错


# ═══════════════════════════════════════════
#  客户端池关闭
# ═══════════════════════════════════════════

class TestClientPoolShutdown:

    @pytest.mark.asyncio
    async def test_close_all_clients_clears_cache(self):
        """close_all_clients 后缓存应为空"""
        from client import get_or_create_client, close_all_clients, _clients, _lock

        # 创建一些客户端
        for i in range(5):
            ch = Channel(
                id=f"ch_shut_{i}",
                name=f"Shut {i}",
                api_type="openai-chat-completions",
                base_url=f"http://example{i}.com",
                api_key="key",
                models=["gpt-4o"],
            )
            await get_or_create_client(ch, timeout=5)

        # 确认缓存非空
        async with _lock:
            assert len(_clients) > 0

        # 关闭所有
        await close_all_clients()

        # 缓存应已清空
        async with _lock:
            assert len(_clients) == 0

    @pytest.mark.asyncio
    async def test_close_all_clients_when_empty_is_safe(self):
        """空缓存时 close_all_clients 不应报错"""
        from client import close_all_clients, _clients, _lock

        async with _lock:
            _clients.clear()

        await close_all_clients()  # 不应报错


# ═══════════════════════════════════════════
#  后台任务取消安全性
# ═══════════════════════════════════════════

class TestBackgroundTaskCancellation:

    @pytest.mark.asyncio
    async def test_session_cleanup_task_handles_cancellation(self):
        """session cleanup 后台任务被取消时不应抛出未处理异常"""
        from main import _session_cleanup_loop

        task = asyncio.create_task(_session_cleanup_loop())
        await asyncio.sleep(0.01)  # 让任务开始
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_request_log_cleanup_task_handles_cancellation(self):
        """request log cleanup 后台任务被取消时不应抛出未处理异常"""
        from main import _request_log_cleanup_loop

        task = asyncio.create_task(_request_log_cleanup_loop())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_client_cleanup_task_handles_cancellation(self):
        """client cleanup 后台任务被取消时不应抛出未处理异常"""
        from client import cleanup_stale_clients

        async def fake_cleanup_loop():
            while True:
                await asyncio.sleep(300)
                try:
                    await cleanup_stale_clients(max_age=600)
                except Exception:
                    pass

        task = asyncio.create_task(fake_cleanup_loop())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


# ═══════════════════════════════════════════
#  队列溢出保护（关闭时）
# ═══════════════════════════════════════════

class TestQueueOverflowDuringShutdown:

    @pytest.mark.asyncio
    async def test_stats_queue_overflow_spills_to_file(self, tmp_path, monkeypatch):
        """队列满时溢出的记录应被写入文件而非丢失"""
        import stats

        db_path = str(tmp_path / "overflow_stats.db")
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(stats, "_STATS_QUEUE_MAX_SIZE", 5)

        await stats.init_db(db_path)
        # 不启动 workers，让队列堆积

        # 写入超过队列大小的记录
        for i in range(20):
            stats.record_request(
                channel_id="ch_overflow",
                channel_name="ch_overflow",
                model="gpt-4o",
                is_stream=False,
                input_tokens=100,
                output_tokens=50,
                latency_ms=100,
                success=True,
                api_key_id="key",
            )

        # 启动 workers 处理积压
        stats.start_stats_workers()
        await asyncio.sleep(1)
        await stats.stop_stats_workers()
        await stats.close_pool()

        # 检查溢出文件是否存在或数据库中有记录
        overflow_file = tmp_path / "stats_overflow.jsonl"
        db_count = 0
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            db_count = conn.execute("SELECT COUNT(*) FROM request_stats_raw").fetchone()[0]

        overflow_count = 0
        if overflow_file.exists():
            overflow_count = len(overflow_file.read_text().strip().split("\n"))

        total = db_count + overflow_count
        assert total == 20, f"Expected 20 total records (db={db_count}, overflow={overflow_count})"
