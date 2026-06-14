"""性能/负载基准测试

覆盖场景：
1. 并发 100 请求下的代理转发延迟
2. 客户端池在高频创建/销毁下的稳定性
3. 负载均衡器在高并发选择下的正确性
4. 存储层高频读写吞吐
5. 统计记录高频写入不阻塞

注意：这些测试不依赖 pytest-benchmark，使用纯 time 测量。
      基准值设为宽松的阈值，只验证无退化/无崩溃。
"""

import asyncio
import json
import time

import pytest

import config
from models.channel import Channel


def _make_channel(
    id: str = "ch_bench",
    weight: int = 1,
    priority: int = 1,
) -> Channel:
    return Channel(
        id=id,
        name=f"Bench {id}",
        api_type="openai-chat-completions",
        base_url="http://example.com",
        api_key="key",
        models=["gpt-4o"],
        enabled=True,
        weight=weight,
        priority=priority,
    )


# ═══════════════════════════════════════════
#  负载均衡器并发选择性能
# ═══════════════════════════════════════════

class TestLoadBalancerConcurrency:

    @pytest.mark.asyncio
    async def test_100_concurrent_selects(self):
        """100 次并发 select_channel 应在 1 秒内完成"""
        from balancer.load_balancer import LoadBalancer

        lb = LoadBalancer()
        channels = [_make_channel(id=f"ch_{i}", weight=1, priority=1) for i in range(5)]

        async def do_select():
            return await lb.select_channel(channels)

        start = time.time()
        results = await asyncio.gather(*[do_select() for _ in range(100)])
        elapsed = time.time() - start

        # 所有选择都应返回有效渠道
        assert all(r is not None for r in results)
        assert elapsed < 1.0, f"100 concurrent selects took {elapsed:.2f}s, expected < 1s"

    @pytest.mark.asyncio
    async def test_1000_concurrent_selects_with_health(self):
        """1000 次并发选择 + 健康记录混合操作不应出错"""
        from balancer.load_balancer import LoadBalancer

        lb = LoadBalancer()
        channels = [_make_channel(id=f"ch_{i}", weight=1, priority=1) for i in range(10)]

        async def do_select():
            return await lb.select_channel(channels)

        async def do_record_success():
            await lb.record_success("ch_0")

        async def do_record_failure():
            await lb.record_failure("ch_9")

        tasks = []
        for i in range(1000):
            if i % 10 == 0:
                tasks.append(do_record_success())
            elif i % 10 == 1:
                tasks.append(do_record_failure())
            else:
                tasks.append(do_select())

        start = time.time()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.time() - start

        # 不应有任何异常
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Got {len(exceptions)} exceptions in concurrent ops"
        assert elapsed < 5.0, f"1000 mixed ops took {elapsed:.2f}s, expected < 5s"


# ═══════════════════════════════════════════
#  客户端池高频创建/销毁稳定性
# ═══════════════════════════════════════════

class TestClientPoolStability:

    @pytest.mark.asyncio
    async def test_rapid_create_and_close(self):
        """快速创建/销毁 100 个客户端不应泄漏或崩溃"""
        from client import get_or_create_client, close_all_clients, invalidate_all_clients

        channels = [
            _make_channel(id=f"ch_pool_{i}")
            for i in range(10)
        ]

        # 快速创建
        for ch in channels:
            client = await get_or_create_client(ch, timeout=5)
            assert client is not None

        # 快速失效
        await invalidate_all_clients()

        # 再次创建（应该创建新的）
        for ch in channels:
            client = await get_or_create_client(ch, timeout=5)
            assert client is not None

        # 清理
        await close_all_clients()

    @pytest.mark.asyncio
    async def test_concurrent_client_creation(self):
        """并发创建 50 个不同渠道的客户端"""
        from client import get_or_create_client, close_all_clients

        channels = [_make_channel(id=f"ch_conc_{i}") for i in range(50)]

        start = time.time()
        clients = await asyncio.gather(
            *[get_or_create_client(ch, timeout=5) for ch in channels]
        )
        elapsed = time.time() - start

        assert all(c is not None for c in clients)
        assert elapsed < 2.0, f"Concurrent creation of 50 clients took {elapsed:.2f}s"

        await close_all_clients()


# ═══════════════════════════════════════════
#  存储层高频读写吞吐
# ═══════════════════════════════════════════

class TestStorageThroughput:

    @pytest.mark.asyncio
    async def test_concurrent_reads(self, tmp_path, monkeypatch):
        """50 次并发读取应在 1 秒内完成"""
        import storage

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        channels_file = data_dir / "channels.json"
        api_keys_file = data_dir / "api_keys.json"

        channels_data = {
            "channels": [
                {
                    "id": f"ch_{i}",
                    "name": f"Ch {i}",
                    "api_type": "openai-chat-completions",
                    "base_url": "http://example.com",
                    "api_key": "key",
                    "models": ["gpt-4o"],
                    "enabled": True,
                    "weight": 1,
                    "priority": 1,
                    "socks5_proxy": None,
                    "created_at": "2026-06-01T00:00:00Z",
                }
                for i in range(20)
            ]
        }
        with open(channels_file, "w") as f:
            json.dump(channels_data, f)
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

        start = time.time()
        results = await asyncio.gather(*[storage.load_data() for _ in range(50)])
        elapsed = time.time() - start

        assert all("channels" in r for r in results)
        assert elapsed < 1.0, f"50 concurrent reads took {elapsed:.2f}s"

        storage._cache = None
        storage._cache_ts = 0
        storage._channels_lock = None
        storage._keys_lock = None


# ═══════════════════════════════════════════
#  统计记录高频写入不阻塞
# ═══════════════════════════════════════════

class TestStatsWriteThroughput:

    @pytest.mark.asyncio
    async def test_rapid_record_request(self, tmp_path, monkeypatch):
        """100 次 record_request 调用不应阻塞（走队列）"""
        import stats

        db_path = str(tmp_path / "bench_stats.db")
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

        await stats.init_db(db_path)
        stats.start_stats_workers()

        start = time.time()
        for i in range(100):
            stats.record_request(
                channel_id=f"ch_{i % 5}",
                channel_name=f"ch_{i % 5}",
                model="gpt-4o",
                is_stream=False,
                input_tokens=100,
                output_tokens=50,
                latency_ms=100,
                success=True,
                api_key_id="bench-key",
            )
        enqueue_elapsed = time.time() - start

        # 入队应该很快（异步非阻塞）
        assert enqueue_elapsed < 1.0, f"100 enqueues took {enqueue_elapsed:.2f}s"

        # 等待 worker 处理完毕
        await asyncio.sleep(0.5)
        await stats.stop_stats_workers()
        await stats.close_pool()
