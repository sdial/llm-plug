"""共享 SQLite 写穿基础设施（Write-Behind Queue / 模块级接线句柄 / 连接工厂 / db 纯工具），对齐 ADR-0012/D0、ADR-0013/D0。"""

import asyncio
import contextlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from loguru import logger

WriteCallback = Callable[[dict[str, Any]], Awaitable[None]]
Serialize = Callable[[dict[str, Any]], dict[str, Any]]

_VALID_SYNCHRONOUS = {"OFF", "NORMAL", "FULL", "EXTRA", "0", "1", "2", "3"}
_VALID_TEMP_STORE = {"DEFAULT", "FILE", "MEMORY", "0", "1", "2"}
_VALID_JOURNAL_MODE = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}


def _sanitize_pragma_env(name: str, default: str, valid: set[str]) -> str:
    val = os.environ.get(name, default)
    if val.upper() not in valid:
        logger.warning("非法 {}={!r}, 回退默认 {}", name, val, default)
        return default
    return val


def _sanitize_int_env(name: str | None, default: int | None) -> int | None:
    if name is None:
        return default
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("非法 {}={!r} 不是整数,回退默认 {}", name, val, default)
        return default


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def create_connection(db_path: str, mmap_size: tuple[str, int] | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"PRAGMA synchronous={_sanitize_pragma_env('SQLITE_SYNCHRONOUS', 'NORMAL', _VALID_SYNCHRONOUS)}")
    conn.execute(f"PRAGMA temp_store={_sanitize_pragma_env('SQLITE_TEMP_STORE', 'FILE', _VALID_TEMP_STORE)}")
    cache_size = _sanitize_int_env("SQLITE_CACHE_SIZE", None)
    if cache_size is not None:
        conn.execute(f"PRAGMA cache_size={cache_size}")
    if mmap_size is not None:
        conn.execute(f"PRAGMA mmap_size={_sanitize_int_env(mmap_size[0], mmap_size[1])}")
    conn.row_factory = sqlite3.Row
    return conn


class WriteBehindQueue:
    def __init__(
        self,
        *,
        write: WriteCallback,
        worker_count: int,
        overflow_path: str,
        overflow_serialize: Serialize | None = None,
        maxsize: int = 1000,
        write_timeout: float = 60,
        name: str = "write-back",
    ):
        self._queue: asyncio.Queue | None = None
        self._queue_loop: asyncio.AbstractEventLoop | None = None
        self._workers: list[asyncio.Task] = []
        self._replay_task: asyncio.Task | None = None
        self._overflow_lock = threading.Lock()
        self._write = write
        self._worker_count = worker_count
        self._overflow_path = overflow_path
        self._overflow_serialize = overflow_serialize or (lambda record: record)
        self._maxsize = maxsize
        self._write_timeout = write_timeout
        self._name = name

    def _ensure_queue(self) -> asyncio.Queue | None:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"[{self._name}] Write-back queue requires a running event loop; discarding record")
            return None
        if self._queue is None or self._queue_loop is not current_loop:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
            self._queue_loop = current_loop
        return self._queue

    def enqueue(self, record: dict[str, Any]) -> None:
        queue = self._ensure_queue()
        if queue is None:
            return
        try:
            queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning(f"[{self._name}] Write-back queue full (maxsize={self._maxsize}); spilling record to overflow file")
            self._spill(record)

    def _spill(self, record: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._overflow_path)), exist_ok=True)
            payload = dict(self._overflow_serialize(record))
            payload.setdefault("_write_id", uuid.uuid4().hex)
            with self._overflow_lock, open(self._overflow_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            logger.error(f"[{self._name}] Failed to spill record to overflow file: {exc}")

    def start(self, worker_count: int | None = None) -> None:
        queue = self._ensure_queue()
        if queue is None:
            return
        if self._workers:
            return
        count = worker_count or self._worker_count
        for _ in range(count):
            self._workers.append(asyncio.create_task(self._worker()))
        self._replay_task = asyncio.create_task(self._replay_overflow())
        logger.info(f"[{self._name}] Write-back workers started: {count} workers, queue max={queue.maxsize}, write timeout={self._write_timeout}s")

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()
        await self.drain()
        self._queue_loop = None

    async def drain(self) -> None:
        if self._replay_task is not None:
            replay_task, self._replay_task = self._replay_task, None
            await replay_task
        queue = self._queue
        if queue is None:
            return
        # 无 worker 时由 drain 同步消费残留；有 worker 时靠 worker 消费，drain 仅 join 等待
        if not self._workers:
            while not queue.empty():
                record = await queue.get()
                try:
                    if not await self._write_one(record):
                        self._spill(record)
                finally:
                    queue.task_done()
        await queue.join()

    async def _write_one(self, record: dict[str, Any]) -> bool:
        try:
            await asyncio.wait_for(self._write(record), timeout=self._write_timeout)
            return True
        except TimeoutError:
            logger.warning(f"[{self._name}] Write timed out ({self._write_timeout}s); spilling record for replay")
        except Exception as exc:
            logger.warning(f"[{self._name}] Write failed: {exc}; spilling record for replay")
        return False

    async def _replay_overflow(self) -> None:
        """启动时回放溢出记录；成功行移除，失败或畸形行继续保留。"""
        try:
            with self._overflow_lock:
                if not os.path.exists(self._overflow_path):
                    return
                with open(self._overflow_path, encoding="utf-8") as f:
                    lines = f.readlines()
                with open(self._overflow_path, "w", encoding="utf-8"):
                    pass
            failed_lines: list[str] = []
            for line in lines:
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("overflow row is not an object")
                    record.setdefault("_write_id", uuid.uuid4().hex)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning(f"[{self._name}] Invalid overflow record retained: {exc}")
                    failed_lines.append(line)
                    continue
                if not await self._write_one(record):
                    failed_lines.append(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            if failed_lines:
                with self._overflow_lock, open(self._overflow_path, "a", encoding="utf-8") as f:
                    f.writelines(failed_lines)
        except Exception as exc:
            logger.exception(f"[{self._name}] Overflow replay failed; records remain on disk: {exc}")

    async def _worker(self) -> None:
        while True:
            try:
                record = await self._queue.get()
                try:
                    if not await self._write_one(record):
                        self._spill(record)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[{self._name}] Write-back worker error: {exc}")


@dataclass(frozen=True)
class WriteBehindParams:
    """WriteBehindQueue 的（重）建参数快照：调用方参数源在每次（重）建时重新求值返回。"""

    worker_count: int
    overflow_path: str
    overflow_serialize: Serialize | None = None
    maxsize: int = 1000
    write_timeout: float = 60
    name: str = "write-back"


class WriteBehindWiring:
    """模块级队列接线句柄——loop 检查/重建、启动、停止、等待排空、重置钩子的共享唯一实现（ADR-0013/D0）。

    消费方持有本句柄替代各自的模块层重建副本与影子全局；差异全经参数注入（写回调 + `params`
    参数源），本类对 record 内容零感知。`params` 在每次（重）建时重新求值——保持「重建时重新
    读取 maxsize / 数据目录」的现有行为，测试 monkeypatch 后立即生效。
    """

    def __init__(self, *, write: WriteCallback, params: Callable[[], WriteBehindParams]) -> None:
        self._write = write
        self._params = params
        self._queue_obj: WriteBehindQueue | None = None
        self._queue_loop: asyncio.AbstractEventLoop | None = None

    def ensure_queue(self) -> WriteBehindQueue | None:
        """返回当前 loop 的队列句柄；无 running loop 时告警并返回 None（调用方丢弃记录）。"""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"[{self._params().name}] Write-back queue requires a running event loop; discarding record")
            return None
        if self._queue_obj is None or self._queue_loop is not current_loop:
            self._queue_obj = WriteBehindQueue(write=self._write, **asdict(self._params()))
            self._queue_loop = current_loop
        return self._queue_obj

    def start(self, worker_count: int | None = None) -> None:
        """启动 worker（per-call worker_count 覆盖，缺省用参数源的 worker_count）。"""
        queue = self.ensure_queue()
        if queue is None:
            return
        queue.start(worker_count)

    async def stop(self) -> None:
        """取消 worker 再 drain 残留（沿用队列类 stop 语义），随后释放句柄——下一次使用重建全新队列。"""
        queue = self._queue_obj
        if queue is None:
            return
        await queue.stop()
        self._release()

    async def drain(self) -> None:
        """排空当前队列；句柄未建时 no-op。"""
        queue = self._queue_obj
        if queue is None:
            return
        await queue.drain()

    async def wait(self) -> None:
        """等待排空：无 worker 运行时由 drain 同步消费残留兜底，不沿用 raw join 无 worker 悬挂的隐患。"""
        await self.drain()

    def reset(self) -> None:
        """重置钩子：释放句柄（不排空），下一次使用按当前参数重建全新队列（close-后-重建语义）。"""
        self._release()

    def _release(self) -> None:
        self._queue_obj = None
        self._queue_loop = None
