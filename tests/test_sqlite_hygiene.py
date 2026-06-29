"""SQLite 鲁棒性配方守门测试。

为常驻服务定下短连接 + 小 cache + 共享 mmap 的配方,本文件用最小测试集守门,
防止后续 PR 把 cache_size=-64000 / PARSE_DECLTYPES / 裸 with sqlite3.connect 改回去。
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import pytest_asyncio

import request_logs
import stats


# ─── 源码层守门:避免回归到不健壮的写法 ───


def _read_source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


# 匹配「未用 closing 包裹的 sqlite 连接获取」:
# - `with sqlite3.connect(...)`
# - `with _connect()` / `with self._connect()`
# 这俩模式的 with 块退出时只 commit/rollback,**不会** close;
# 必须改成 `with closing(...) as conn, conn:` 才能保证 fd 立即释放。
_BARE_CONN_PATTERNS = (
    re.compile(r"\bwith\s+sqlite3\.connect\("),
    re.compile(r"\bwith\s+(?:self\.)?_connect\s*\(\s*\)\s+as"),
)


def _find_bare_connections(src: str) -> list[str]:
    hits: list[str] = []
    for pat in _BARE_CONN_PATTERNS:
        hits.extend(pat.findall(src))
    return hits


def test_stats_source_no_64mb_cache_no_parse_decltypes_no_bare_with_connect():
    src = _read_source(stats)
    # 禁止 64MB 私有 cache (短连接场景纯浪费)
    assert "cache_size=-64000" not in src, (
        "stats.py 不应使用 cache_size=-64000:短连接场景每次申请 64MB 是浪费,"
        "应使用 SQLite 默认 cache 配合 mmap_size 共享 OS page cache"
    )
    # PARSE_DECLTYPES 是无效配置(没有 DECLTYPE 列),删掉避免误导
    assert "PARSE_DECLTYPES" not in src, (
        "stats.py 不应使用 PARSE_DECLTYPES:表里没有 TIMESTAMP/DATE 列声明,该 flag 无效"
    )
    bare = _find_bare_connections(src)
    assert not bare, (
        f"stats.py 出现 {len(bare)} 处不会 close 的连接获取 {bare!r};"
        "必须改成 `with closing(...) as conn, conn:` 模式"
    )


def test_request_logs_source_no_64mb_cache_no_bare_with_connect():
    src = _read_source(request_logs)
    assert "cache_size=-64000" not in src, (
        "request_logs.py 不应使用 cache_size=-64000:短连接场景每次申请 64MB 是浪费"
    )
    bare = _find_bare_connections(src)
    assert not bare, (
        f"request_logs.py 出现 {len(bare)} 处不会 close 的连接获取 {bare!r};"
        "必须改成 `with closing(...) as conn, conn:` 模式"
    )


# ─── 运行时守门:_connect() 返回的连接必须带正确 PRAGMA ───


def _assert_robust_short_conn_pragmas(conn: sqlite3.Connection) -> None:
    """断言一个新连接已配置为'短连接友好'配方。"""
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout >= 5000, f"busy_timeout 必须 >= 5000ms (got {busy_timeout})"

    # synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA — WAL 下 NORMAL 既安全又快
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    assert synchronous in (1, 2), f"synchronous 应为 NORMAL(1) 或 FULL(2) (got {synchronous})"

    # temp_store: 0=DEFAULT, 1=FILE, 2=MEMORY — 生产环境可配置为 FILE 或 MEMORY
    temp_store = conn.execute("PRAGMA temp_store").fetchone()[0]
    assert temp_store in (1, 2), f"temp_store 必须为 FILE(1) 或 MEMORY(2) (got {temp_store})"

    # mmap_size > 0 表示走 OS page cache 共享,替代私有 cache
    mmap_size = conn.execute("PRAGMA mmap_size").fetchone()[0]
    assert mmap_size > 0, f"mmap_size 必须 > 0 (got {mmap_size})"

    # cache_size 必须不是 -64000(64MB),避免每个短连接申请大块内存
    cache_size = conn.execute("PRAGMA cache_size").fetchone()[0]
    assert cache_size != -64000, (
        f"cache_size 不应为 -64000 (64MB);短连接场景应使用 SQLite 默认或更小值,"
        f"got {cache_size}"
    )


# ─── stats.py 运行时 PRAGMA 测试 ───


@pytest_asyncio.fixture
async def stats_db(tmp_path):
    db_path = tmp_path / "stats.db"
    await stats.close_pool()
    await stats.init_db(str(db_path))
    yield db_path
    await stats.stop_stats_workers()
    await stats.close_pool()


@pytest.mark.asyncio
async def test_stats_connect_has_robust_short_conn_pragmas(stats_db):
    with closing(stats._connect()) as conn:
        _assert_robust_short_conn_pragmas(conn)


@pytest.mark.asyncio
async def test_stats_init_db_sets_persistent_wal_mode(stats_db):
    with closing(sqlite3.connect(str(stats_db))) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"DB 文件应持久化为 WAL 模式 (got {mode})"


@pytest.mark.asyncio
async def test_stats_writes_still_work_after_pragma_changes(stats_db):
    """端到端冒烟:改了 PRAGMA 之后写入和读取链路仍正常。"""
    stats.record_request(
        channel_id="ch_hyg",
        channel_name="Hygiene",
        model="gpt-hyg",
        is_stream=False,
        input_tokens=1,
        output_tokens=1,
        latency_ms=10,
        success=True,
    )
    await stats.drain_queue()
    result = await stats.list_requests()
    assert result["total"] == 1
    assert result["items"][0]["channel_id"] == "ch_hyg"


# ─── request_logs.py 运行时 PRAGMA 测试 ───


@pytest_asyncio.fixture
async def request_logs_db(tmp_path, monkeypatch):
    await request_logs.close_backend()
    monkeypatch.setattr(
        request_logs,
        "_get_save_flags",
        lambda: {
            "save_request_headers": False,
            "save_response_headers": False,
            "save_request_body": False,
            "save_response_body": False,
        },
    )
    db_path = tmp_path / "request_logs.db"
    result = await request_logs.init_backend(
        {
            "request_log_sqlite_path": str(db_path),
        }
    )
    assert result["available"] is True
    yield db_path
    await request_logs.close_backend()


@pytest.mark.asyncio
async def test_request_logs_sqlite_connect_has_robust_short_conn_pragmas(request_logs_db):
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    current_ym = backend._current_year_month()
    month_db = backend._month_db_path(current_ym)
    with closing(backend._connect_to(month_db)) as conn:
        _assert_robust_short_conn_pragmas(conn)


@pytest.mark.asyncio
async def test_request_logs_init_sets_persistent_wal_mode(request_logs_db):
    backend = request_logs._backend
    assert isinstance(backend, request_logs.SQLiteRequestLogBackend)
    current_ym = backend._current_year_month()
    month_db = backend._month_db_path(current_ym)
    with closing(sqlite3.connect(month_db)) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


@pytest.mark.asyncio
async def test_request_logs_writes_still_work_after_pragma_changes(request_logs_db):
    request_logs.record_request(
        channel_id="ch_hyg",
        channel_name="Hygiene",
        model="gpt-hyg",
        is_stream=False,
        input_tokens=1,
        output_tokens=1,
        latency_ms=10,
        success=True,
    )
    await request_logs.drain_queue()
    result = await request_logs.list_requests()
    assert result["available"] is True
    assert result["total"] == 1
    assert result["items"][0]["channel_id"] == "ch_hyg"
