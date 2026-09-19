"""stats 队列接线薄测试。

队列语义本体（溢出 / drain / 超时 / worker 启停 / 跨 loop 重建）在
tests/test_db_write_behind.py 共享单测（fake 写回调）；本文件只确认
stats 经共享接线句柄（WriteBehindWiring）持有队列、写回调按 _type 分发正确，
以及统计侧语义对齐（wait_for_queue / 未初始化告警 / per-call worker_count）。
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from loguru import logger

import config
import db_write_behind
import stats

pytestmark = pytest.mark.asyncio


@pytest.fixture
def captured_logs():
    records = []
    handler_id = logger.add(records.append, format="{message}")
    yield records
    logger.remove(handler_id)


async def test_stats_wiring_handle_returns_shared_queue():
    """stats 经 `_wiring` 句柄持有共享队列：ensure_queue 返回 WriteBehindQueue 实例且同 loop 复用。"""
    first = stats._wiring.ensure_queue()
    assert isinstance(first, db_write_behind.WriteBehindQueue)
    assert stats._wiring.ensure_queue() is first
    await stats.close_pool()


async def test_write_callback_dispatches_context_shaping_and_record(tmp_path, monkeypatch):
    shaping = AsyncMock()
    rec = AsyncMock()
    monkeypatch.setattr(stats, "_write_context_shaping_record", shaping)
    monkeypatch.setattr(stats, "_write_record", rec)

    await stats.close_pool()
    await stats.init_db(str(tmp_path / "wiring.db"))
    try:
        stats.record_context_shaping_action(
            channel_id="c", model="m", feature="strip_ansi", action="remove_ansi", action_count=1, before_chars=10, after_chars=4
        )
        stats.record_request(
            channel_id="c",
            channel_name="n",
            model="m",
            is_stream=False,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            success=True,
        )
        await stats.drain_queue()
    finally:
        await stats.close_pool()

    shaping.assert_awaited_once()
    rec.assert_awaited_once()
    assert shaping.await_args.args[0].get("_type") == "context_shaping"
    assert rec.await_args.args[0].get("_type") != "context_shaping"


async def test_stats_overflow_file_strips_raw_fields(tmp_path, monkeypatch):
    """队列满溢出时，溢出文件剥离请求原文（与 SQLite 写侧 _normalize_record 同口径），仅保留统计字段。

    镜像日志侧溢出测试形态：真实 record_request 打满队列 → 读 data/stats_overflow.jsonl 断言。
    record_request 签名收缩（ADR-0014 D2）后入队负载本身不含 raw 字段，
    剥离断言保留为"溢出文件永不携带请求原文"的不变量兜底。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(stats, "_STATS_QUEUE_MAX_SIZE", 1)
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "stats.db"))
    try:
        for i in range(4):
            stats.record_request(
                channel_id=f"ch_{i}",
                channel_name="Ch",
                model="gpt-4o",
                is_stream=False,
                input_tokens=10,
                output_tokens=5,
                latency_ms=100,
                success=True,
                api_key_id="key_1",
                client_ip="127.0.0.1",
            )

        overflow_path = tmp_path / "stats_overflow.jsonl"
        assert overflow_path.exists()
        rows = [json.loads(line) for line in overflow_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 3
        for row in rows:
            assert "request_headers" not in row
            assert "response_headers" not in row
            assert "request_body" not in row
            assert "response_body" not in row
            assert row["channel_name"] == "Ch"
            assert row["model"] == "gpt-4o"
            assert row["input_tokens"] == 10
            assert row["output_tokens"] == 5
            assert row["latency_ms"] == 100
            assert row["success"] is True
        assert {row["channel_id"] for row in rows} == {"ch_1", "ch_2", "ch_3"}

        await stats.drain_queue()
        assert (await stats.list_requests())["total"] == 1
    finally:
        await stats.close_pool()


# --- 统计侧语义对齐（票 03）：wait_for_queue / 未初始化告警 / per-call worker_count ---


async def test_record_request_warns_and_discards_when_db_uninitialized(tmp_path, monkeypatch, captured_logs):
    """库未初始化时 record_request 告警 + 丢弃（对齐日志侧 discarding 风格），而非静默返回。"""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    await stats.close_pool()
    stats.record_request(
        channel_id="c",
        channel_name="n",
        model="m",
        is_stream=False,
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        success=True,
    )
    assert any("discarding" in m.lower() for m in captured_logs)

    # 记录确实被丢弃：之后初始化并排空，队列中无残留可落库
    await stats.init_db(str(tmp_path / "uninit.db"))
    try:
        await stats.drain_queue()
        assert (await stats.list_requests())["total"] == 0
    finally:
        await stats.close_pool()


async def test_record_context_shaping_warns_and_discards_when_db_uninitialized(tmp_path, monkeypatch, captured_logs):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    await stats.close_pool()
    stats.record_context_shaping_action(
        channel_id="c", model="m", feature="strip_ansi", action="remove_ansi", action_count=1, before_chars=10, after_chars=4
    )
    assert any("discarding" in m.lower() for m in captured_logs)

    # 记录确实被丢弃：之后初始化并排空，队列中无残留可落库
    await stats.init_db(str(tmp_path / "uninit.db"))
    try:
        await stats.drain_queue()
        assert (await stats.get_context_shaping_daily_stats()) == []
    finally:
        await stats.close_pool()


async def test_wait_for_queue_matches_drain_queue(tmp_path, monkeypatch):
    """wait_for_queue 与 drain_queue 排空等价：入队后各自等待，记录均落库。"""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "wait.db"))
    try:
        stats.record_request(
            channel_id="c1",
            channel_name="n",
            model="m",
            is_stream=False,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            success=True,
        )
        await stats.wait_for_queue()
        assert (await stats.list_requests())["total"] == 1

        stats.record_request(
            channel_id="c2",
            channel_name="n",
            model="m",
            is_stream=False,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            success=True,
        )
        await stats.drain_queue()
        assert (await stats.list_requests())["total"] == 2
    finally:
        await stats.close_pool()


async def test_wait_for_queue_drains_residuals_without_workers(tmp_path, monkeypatch):
    """无 worker 运行时 wait_for_queue 以 drain 语义兜底消费残留后返回（不沿用 raw join 悬挂）。"""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    await stats.close_pool()
    await stats.init_db(str(tmp_path / "wait.db"))
    try:
        # 不启动 workers，让记录堆积在队列
        stats.record_request(
            channel_id="c1",
            channel_name="n",
            model="m",
            is_stream=False,
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            success=True,
        )
        await asyncio.wait_for(stats.wait_for_queue(), timeout=1)
        assert (await stats.list_requests())["total"] == 1
    finally:
        await stats.close_pool()


async def test_wait_for_queue_safe_without_handle():
    """无句柄（未初始化/已 close）时 wait_for_queue 安全返回，不悬挂。"""
    await stats.close_pool()
    await asyncio.wait_for(stats.wait_for_queue(), timeout=1)


async def test_start_stats_workers_worker_count_override():
    """start_stats_workers(worker_count=N) per-call 覆盖生效。"""
    await stats.close_pool()
    try:
        stats.start_stats_workers(worker_count=2)
        queue = stats._wiring.ensure_queue()
        assert queue is not None
        assert len(queue._workers) == 2
    finally:
        await stats.stop_stats_workers()
        await stats.close_pool()


async def test_start_stats_workers_default_worker_count():
    """缺省启动用参数源的 worker_count（STATS_WORKER_COUNT，模块全局可重读）。"""
    await stats.close_pool()
    try:
        stats.start_stats_workers()
        queue = stats._wiring.ensure_queue()
        assert queue is not None
        assert len(queue._workers) == stats._STATS_WORKER_COUNT
    finally:
        await stats.stop_stats_workers()
        await stats.close_pool()
