"""db_write_behind 共享写穿基础设施单测（fake 写回调覆盖完整队列语义，不经真实 SQLite）。"""

import asyncio
import json
import sqlite3
from datetime import datetime

import pytest
from loguru import logger

import db_write_behind


class RecordingWrite:
    """fake 写回调：记录被调用的 record；可选按谓词抛出异常或挂起模拟超时。"""

    def __init__(self, *, raises=None, hangs=None):
        self.writes: list = []
        self._raises = raises
        self._hangs = hangs

    async def __call__(self, record):
        if self._hangs:
            delay = self._hangs(record)
            if delay:
                await asyncio.sleep(delay)
        if self._raises and self._raises(record):
            raise RuntimeError("boom")
        self.writes.append(record)


@pytest.fixture
def captured_logs():
    records = []
    handler_id = logger.add(records.append, format="{message}")
    yield records
    logger.remove(handler_id)


def _make_queue(write, *, overflow_path, **kwargs):
    return db_write_behind.WriteBehindQueue(write=write, overflow_path=str(overflow_path), **kwargs)


@pytest.mark.asyncio
async def test_normal_write_consumed_by_worker(tmp_path):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    try:
        q.enqueue({"n": 1})
        q.enqueue({"n": 2})
        q.enqueue({"n": 3})
        q.start()
        await q.drain()
        assert write.writes == [{"n": 1}, {"n": 2}, {"n": 3}]
        assert q._queue.empty()
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_overflow_spills_to_configured_file_and_not_written(tmp_path):
    overflow_path = tmp_path / "spill.jsonl"
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, maxsize=2, overflow_path=overflow_path)
    for i in range(5):
        q.enqueue({"model": f"m{i}"})
    await q.drain()
    assert [w["model"] for w in write.writes] == ["m0", "m1"]
    spilled = [json.loads(line) for line in overflow_path.read_text().strip().splitlines()]
    assert [r["model"] for r in spilled] == ["m2", "m3", "m4"]


@pytest.mark.asyncio
async def test_overflow_applies_injected_serializer(tmp_path):
    overflow_path = tmp_path / "serialized.jsonl"
    write = RecordingWrite()
    q = _make_queue(
        write,
        worker_count=1,
        maxsize=1,
        overflow_path=overflow_path,
        overflow_serialize=lambda r: {**r, "timestamp": "2026-01-01 00:00:00.000000"},
    )
    queued = {"model": "queued"}
    overflowed = {"model": "overflowed", "timestamp": datetime(2026, 1, 1)}
    q.enqueue(queued)
    q.enqueue(overflowed)
    await q.drain()
    assert write.writes == [queued]
    line = json.loads(overflow_path.read_text())
    assert line["timestamp"] == "2026-01-01 00:00:00.000000"
    assert line["model"] == "overflowed"


@pytest.mark.asyncio
async def test_start_replays_overflow_and_removes_successful_rows(tmp_path):
    overflow_path = tmp_path / "replay.jsonl"
    overflow_path.write_text(json.dumps({"n": 1, "_write_id": "saved"}) + "\n", encoding="utf-8")
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=overflow_path)
    try:
        q.start()
        await q.drain()
    finally:
        await q.stop()

    assert write.writes == [{"n": 1, "_write_id": "saved"}]
    assert overflow_path.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_start_retains_failed_overflow_rows(tmp_path):
    overflow_path = tmp_path / "replay-fail.jsonl"
    overflow_path.write_text(json.dumps({"n": 1, "_write_id": "saved"}) + "\n", encoding="utf-8")
    q = _make_queue(RecordingWrite(raises=lambda _: True), worker_count=1, overflow_path=overflow_path)
    try:
        q.start()
        await q.drain()
    finally:
        await q.stop()

    assert json.loads(overflow_path.read_text(encoding="utf-8")) == {"n": 1, "_write_id": "saved"}


@pytest.mark.asyncio
async def test_drain_consumes_residuals_without_workers(tmp_path):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    q.enqueue({"a": 1})
    q.enqueue({"b": 2})
    await q.drain()
    assert write.writes == [{"a": 1}, {"b": 2}]


@pytest.mark.asyncio
async def test_drain_catches_callback_exception_and_continues(tmp_path, captured_logs):
    write = RecordingWrite(raises=lambda r: r["n"] == "boom")
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    q.enqueue({"n": "boom"})
    q.enqueue({"n": "ok"})
    await q.drain()
    assert write.writes == [{"n": "ok"}]
    assert any("write failed" in m.lower() for m in captured_logs)


@pytest.mark.asyncio
async def test_write_timeout_discards_warns_and_does_not_block(tmp_path, captured_logs):
    write = RecordingWrite(hangs=lambda r: 60 if r.get("kind") == "slow" else None)
    q = _make_queue(write, worker_count=1, write_timeout=0.05, overflow_path=tmp_path / "o.jsonl")
    try:
        q.enqueue({"kind": "slow"})
        q.start()
        await q.drain()
        q.enqueue({"kind": "fast"})
        await q.drain()
    finally:
        await q.stop()
    assert write.writes == [{"kind": "fast"}]
    assert any("timed out" in m.lower() for m in captured_logs)


@pytest.mark.asyncio
async def test_callback_exception_warns_and_worker_continues(tmp_path, captured_logs):
    write = RecordingWrite(raises=lambda r: r["n"] == "boom")
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    try:
        q.enqueue({"n": "boom"})
        q.enqueue({"n": "ok"})
        q.start()
        await q.drain()
    finally:
        await q.stop()
    assert write.writes == [{"n": "ok"}]
    assert any("write failed" in m.lower() for m in captured_logs)


def test_enqueue_without_running_loop_warns_and_discards(tmp_path, captured_logs):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    q.enqueue({"n": 1})
    assert write.writes == []
    assert q._queue is None
    assert any("running event loop" in m.lower() for m in captured_logs)


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    try:
        q.start()
        workers = list(q._workers)
        assert len(workers) == 1
        q.start()
        assert q._workers == workers
        q.enqueue({"n": 1})
        await q.drain()
        assert write.writes == [{"n": 1}]
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_start_worker_count_override(tmp_path):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    try:
        q.start(worker_count=3)
        assert len(q._workers) == 3
        q.enqueue({"n": "a"})
        q.enqueue({"n": "b"})
        q.enqueue({"n": "c"})
        await q.drain()
        assert sorted(w["n"] for w in write.writes) == ["a", "b", "c"]
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_stop_drains_queued_residuals(tmp_path):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    q.enqueue({"n": 1})
    q.enqueue({"n": 2})
    await q.stop()
    assert q._workers == []
    assert write.writes == [{"n": 1}, {"n": 2}]


@pytest.mark.asyncio
async def test_stop_cancels_blocked_worker_write_and_clears_workers(tmp_path):
    class BlockingWrite:
        def __init__(self):
            self.started = asyncio.Event()
            self.writes = []

        async def __call__(self, record):
            self.started.set()
            await asyncio.sleep(60)
            self.writes.append(record)

    write = BlockingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    q.enqueue({"n": 1})
    q.start()
    await write.started.wait()
    await q.stop()
    assert q._workers == []
    assert write.writes == []


@pytest.mark.asyncio
async def test_double_stop_is_safe(tmp_path):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    q.start()
    await q.stop()
    await q.stop()


def test_cross_event_loop_rebuilds_queue(tmp_path):
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    queues = {}

    async def run(record, key):
        q.enqueue(record)
        await q.drain()
        queues[key] = q._queue

    asyncio.run(run({"n": 1}, "first"))
    asyncio.run(run({"n": 2}, "second"))

    assert queues["first"] is not queues["second"]
    assert [w["n"] for w in write.writes] == [1, 2]


@pytest.mark.asyncio
async def test_start_after_stop_recreates_queue(tmp_path):
    """迁自 test_stats_workers：start→stop→start 后队列对象为新队列（start 幂等但 stop 后重建）。"""
    write = RecordingWrite()
    q = _make_queue(write, worker_count=1, overflow_path=tmp_path / "o.jsonl")
    q.start()
    first_queue = q._queue
    await q.stop()

    q.start()
    second_queue = q._queue
    await q.stop()

    assert first_queue is not second_queue


# --- 统一接线句柄 WriteBehindWiring（模块级接线仪式的唯一实现，对齐 ADR-0013/D0）---


def _make_wiring(write, *, overflow_path, **param_overrides):
    params = {"worker_count": 1, "overflow_path": str(overflow_path), "maxsize": 1000, "write_timeout": 60}
    params.update(param_overrides)
    return db_write_behind.WriteBehindWiring(write=write, params=lambda: db_write_behind.WriteBehindParams(**params))


def test_wiring_ensure_without_running_loop_warns_and_returns_none(tmp_path, captured_logs):
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl")
    assert wiring.ensure_queue() is None
    wiring.start()  # 无 running loop 时安全 no-op
    assert write.writes == []
    assert any("running event loop" in m.lower() for m in captured_logs)


@pytest.mark.asyncio
async def test_wiring_start_uses_params_worker_count_and_wait_joins(tmp_path):
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl", worker_count=2)
    q = wiring.ensure_queue()
    assert q is not None
    try:
        wiring.start()
        assert len(q._workers) == 2  # 缺省用参数源的 worker_count
        q.enqueue({"n": 1})
        q.enqueue({"n": 2})
        await asyncio.wait_for(wiring.wait(), timeout=1)
        assert write.writes == [{"n": 1}, {"n": 2}]
    finally:
        await wiring.stop()


@pytest.mark.asyncio
async def test_wiring_start_worker_count_override(tmp_path):
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl", worker_count=1)
    q = wiring.ensure_queue()
    assert q is not None
    try:
        wiring.start(worker_count=3)
        assert len(q._workers) == 3
        for i in range(3):
            q.enqueue({"n": i})
        await wiring.drain()
        assert [w["n"] for w in write.writes] == [0, 1, 2]
    finally:
        await wiring.stop()


@pytest.mark.asyncio
async def test_wiring_stop_drains_residuals_and_releases_handle(tmp_path):
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl")
    q = wiring.ensure_queue()
    assert q is not None
    q.enqueue({"n": 1})
    q.enqueue({"n": 2})
    await wiring.stop()
    assert write.writes == [{"n": 1}, {"n": 2}]
    assert wiring.ensure_queue() is not q  # stop 后句柄释放，下一次使用重建全新队列


@pytest.mark.asyncio
async def test_wiring_stop_wait_drain_without_handle_are_safe(tmp_path):
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl")
    await asyncio.wait_for(wiring.wait(), timeout=1)
    await wiring.drain()
    await wiring.stop()
    wiring.ensure_queue()
    await wiring.stop()
    await asyncio.wait_for(wiring.wait(), timeout=1)
    assert write.writes == []


@pytest.mark.asyncio
async def test_wiring_wait_without_workers_drains_residuals_and_returns(tmp_path):
    """wait 在无 worker 运行时安全返回（drain 语义兜底），不沿用 raw join 无 worker 悬挂的隐患。"""
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl")
    q = wiring.ensure_queue()
    assert q is not None
    q.enqueue({"n": 1})
    q.enqueue({"n": 2})
    await asyncio.wait_for(wiring.wait(), timeout=1)
    assert write.writes == [{"n": 1}, {"n": 2}]


@pytest.mark.asyncio
async def test_wiring_reset_releases_handle_and_next_use_rebuilds_fresh_queue(tmp_path):
    """重置钩子：释放句柄，下一次使用按当前参数重建全新队列（close-后-重建语义）。"""
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl")
    first = wiring.ensure_queue()
    assert first is not None
    first.enqueue({"n": 1})
    await wiring.drain()
    assert write.writes == [{"n": 1}]

    wiring.reset()
    second = wiring.ensure_queue()
    assert second is not None and second is not first
    second.enqueue({"n": 2})
    await wiring.drain()
    assert write.writes == [{"n": 1}, {"n": 2}]


@pytest.mark.asyncio
async def test_wiring_reset_releases_handle_without_draining_pending_records(tmp_path):
    """重置只释放句柄不排空——close 类路径必须先 stop 再 reset，否则残留随旧句柄丢弃。"""
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl")
    first = wiring.ensure_queue()
    assert first is not None
    first.enqueue({"n": 1})
    wiring.reset()
    await wiring.drain()  # 句柄已释放 → no-op，旧队列残留随句柄丢弃
    second = wiring.ensure_queue()
    assert second is not None
    second.enqueue({"n": 2})
    await wiring.drain()
    assert write.writes == [{"n": 2}]


@pytest.mark.asyncio
async def test_wiring_rebuild_reevaluates_params(tmp_path):
    """（重）建时参数源重新求值：重建后新队列用新 maxsize / 新溢出路径，未重建则复用同一队列。"""
    write = RecordingWrite()
    first_overflow = tmp_path / "first.jsonl"
    second_overflow = tmp_path / "second.jsonl"
    box = {"maxsize": 1, "overflow_path": str(first_overflow)}
    wiring = db_write_behind.WriteBehindWiring(
        write=write,
        params=lambda: db_write_behind.WriteBehindParams(worker_count=1, overflow_path=box["overflow_path"], maxsize=box["maxsize"]),
    )
    first = wiring.ensure_queue()
    assert first is not None
    assert wiring.ensure_queue() is first  # 同 loop 未重建，不重新求值参数
    first.enqueue({"n": 1})
    first.enqueue({"n": 2})  # maxsize=1 → 溢出到 first.jsonl
    await wiring.drain()
    assert write.writes == [{"n": 1}]

    box.update(maxsize=5, overflow_path=str(second_overflow))
    wiring.reset()
    second = wiring.ensure_queue()
    assert second is not None and second is not first
    for i in range(5):
        second.enqueue({"n": 10 + i})  # 新 maxsize=5 → 全部入队不溢出；溢出路径已切换
    await wiring.drain()
    assert [w["n"] for w in write.writes] == [1, 10, 11, 12, 13, 14]
    assert [json.loads(line)["n"] for line in first_overflow.read_text().strip().splitlines()] == [2]
    assert not second_overflow.exists()


def test_wiring_cross_event_loop_rebuilds_queue(tmp_path):
    write = RecordingWrite()
    wiring = _make_wiring(write, overflow_path=tmp_path / "o.jsonl")
    queues = {}

    async def run(record, key):
        q = wiring.ensure_queue()
        assert q is not None
        q.enqueue(record)
        await wiring.drain()
        queues[key] = q

    asyncio.run(run({"n": 1}, "first"))
    asyncio.run(run({"n": 2}, "second"))

    assert queues["first"] is not queues["second"]
    assert [w["n"] for w in write.writes] == [1, 2]


def test_create_connection_applies_base_pragmas(tmp_path):
    conn = db_write_behind.create_connection(str(tmp_path / "test.db"))
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_create_connection_without_mmap_size_applies_none(tmp_path):
    conn = db_write_behind.create_connection(str(tmp_path / "x.db"))
    try:
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 0
    finally:
        conn.close()


def test_create_connection_mmap_size_env_and_fallback(tmp_path, monkeypatch):
    conn = db_write_behind.create_connection(str(tmp_path / "mmap.db"))
    first_size = conn.execute("PRAGMA mmap_size").fetchone()[0]
    conn.close()
    assert first_size != 98765

    monkeypatch.setenv("SQLITE_MMAP_SIZE_TEST", "not-an-int")
    conn = db_write_behind.create_connection(str(tmp_path / "mmap.db"), mmap_size=("SQLITE_MMAP_SIZE_TEST", 98765))
    try:
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 98765
    finally:
        conn.close()

    monkeypatch.setenv("SQLITE_MMAP_SIZE_TEST2", "123456")
    conn = db_write_behind.create_connection(str(tmp_path / "mmap.db"), mmap_size=("SQLITE_MMAP_SIZE_TEST2", 0))
    try:
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 123456
    finally:
        conn.close()


def test_escape_like():
    assert db_write_behind._escape_like("plain") == "plain"
    assert db_write_behind._escape_like("a\\b%c_d") == "a\\\\b\\%c\\_d"


def test_sanitize_int_env(monkeypatch):
    monkeypatch.setenv("TEST_INT_ENV", "abc")
    assert db_write_behind._sanitize_int_env("TEST_INT_ENV", 42) == 42
    monkeypatch.delenv("TEST_INT_ENV")
    assert db_write_behind._sanitize_int_env("TEST_INT_ENV", 42) == 42
    monkeypatch.setenv("TEST_INT_ENV", "7")
    assert db_write_behind._sanitize_int_env("TEST_INT_ENV", 42) == 7
    assert db_write_behind._sanitize_int_env(None, 42) == 42


def test_sanitize_pragma_env(monkeypatch):
    monkeypatch.setenv("TEST_PRAGMA_ENV", "BOGUS")
    assert db_write_behind._sanitize_pragma_env("TEST_PRAGMA_ENV", "NORMAL", db_write_behind._VALID_SYNCHRONOUS) == "NORMAL"
    monkeypatch.setenv("TEST_PRAGMA_ENV", "full")
    assert db_write_behind._sanitize_pragma_env("TEST_PRAGMA_ENV", "NORMAL", db_write_behind._VALID_SYNCHRONOUS) == "full"
