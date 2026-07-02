"""P0-2: FileStore.cleanup_expired() / evict_lru() / _cleanup_if_needed() 直接测试"""

import json
import os
import time

import pytest

from state_store import FileStore


@pytest.fixture
def tmp_store(tmp_path):
    """创建临时 FileStore"""
    return FileStore(str(tmp_path), max_entries=5, ttl_minutes=1)


def _write_session(store: FileStore, response_id: str, ttl_offset: int = 3600) -> str:
    """直接写入一个会话文件，ttl_offset 控制过期时间（秒，正数=未来过期，负数=已过期）"""
    path = store._file_path(response_id)
    now = int(time.time())
    data = {
        "response_id": response_id,
        "conversation": {"items": []},
        "response": {"id": response_id},
        "created_at": now,
        "expires_at": now + ttl_offset,
        "last_access_at": now,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# ═══════════════════════════════════════════
#  cleanup_expired()
# ═══════════════════════════════════════════


class TestCleanupExpired:
    @pytest.mark.asyncio
    async def test_removes_expired_files(self, tmp_store):
        _write_session(tmp_store, "resp_expired1", ttl_offset=-100)
        _write_session(tmp_store, "resp_expired2", ttl_offset=-200)
        _write_session(tmp_store, "resp_valid", ttl_offset=3600)

        removed = await tmp_store.cleanup_expired()
        assert removed == 2
        # valid 文件仍然存在
        assert os.path.exists(tmp_store._file_path("resp_valid"))
        # expired 文件已删除
        assert not os.path.exists(tmp_store._file_path("resp_expired1"))
        assert not os.path.exists(tmp_store._file_path("resp_expired2"))

    @pytest.mark.asyncio
    async def test_no_expired_files(self, tmp_store):
        _write_session(tmp_store, "resp_a", ttl_offset=3600)
        _write_session(tmp_store, "resp_b", ttl_offset=7200)

        removed = await tmp_store.cleanup_expired()
        assert removed == 0

    @pytest.mark.asyncio
    async def test_empty_directory(self, tmp_store):
        removed = await tmp_store.cleanup_expired()
        assert removed == 0

    @pytest.mark.asyncio
    async def test_ignores_non_json_files(self, tmp_store):
        # 创建一个非 .json 文件
        non_json = os.path.join(tmp_store.data_dir, "readme.txt")
        with open(non_json, "w") as f:
            f.write("not a session")
        removed = await tmp_store.cleanup_expired()
        assert removed == 0
        assert os.path.exists(non_json)

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self, tmp_store):
        """损坏的 JSON 文件不应导致清理崩溃"""
        path = tmp_store._file_path("resp_bad")
        with open(path, "w") as f:
            f.write("{broken json")

        removed = await tmp_store.cleanup_expired()
        assert removed == 0
        # 损坏文件保留（不删除，因为无法判断过期时间）
        assert os.path.exists(path)

    @pytest.mark.asyncio
    async def test_boundary_exactly_expired(self, tmp_store):
        """expires_at 正好 < now 的文件应被清理"""
        _write_session(tmp_store, "resp_boundary", ttl_offset=-1)
        removed = await tmp_store.cleanup_expired()
        assert removed == 1

    @pytest.mark.asyncio
    async def test_mixed_expired_and_valid(self, tmp_store):
        for i in range(10):
            offset = -100 if i < 5 else 3600
            _write_session(tmp_store, f"resp_{i:03d}", ttl_offset=offset)

        removed = await tmp_store.cleanup_expired()
        assert removed == 5


# ═══════════════════════════════════════════
#  evict_lru()
# ═══════════════════════════════════════════


class TestEvictLru:
    @pytest.mark.asyncio
    async def test_evicts_oldest_when_over_capacity(self, tmp_store):
        """max_entries=5，写入 8 个文件，应淘汰最旧的 3 个"""
        ids = []
        for i in range(8):
            rid = f"resp_{i:03d}"
            ids.append(rid)
            _write_session(tmp_store, rid, ttl_offset=3600)
            # 确保 mtime 有序递增
            path = tmp_store._file_path(rid)
            os.utime(path, (time.time() + i, time.time() + i))

        removed = await tmp_store.evict_lru()
        assert removed == 3
        # 最新的 5 个保留
        for i in range(3, 8):
            assert os.path.exists(tmp_store._file_path(f"resp_{i:03d}"))
        # 最旧的 3 个已删除
        for i in range(3):
            assert not os.path.exists(tmp_store._file_path(f"resp_{i:03d}"))

    @pytest.mark.asyncio
    async def test_no_eviction_when_under_capacity(self, tmp_store):
        for i in range(3):
            _write_session(tmp_store, f"resp_{i:03d}", ttl_offset=3600)

        removed = await tmp_store.evict_lru()
        assert removed == 0

    @pytest.mark.asyncio
    async def test_no_eviction_at_exact_capacity(self, tmp_store):
        """正好 max_entries 个文件时不淘汰"""
        for i in range(5):
            _write_session(tmp_store, f"resp_{i:03d}", ttl_offset=3600)

        removed = await tmp_store.evict_lru()
        assert removed == 0

    @pytest.mark.asyncio
    async def test_empty_directory(self, tmp_store):
        removed = await tmp_store.evict_lru()
        assert removed == 0

    @pytest.mark.asyncio
    async def test_ignores_non_json_files(self, tmp_store):
        """非 .json 文件不参与 LRU 计数"""
        for i in range(5):
            _write_session(tmp_store, f"resp_{i:03d}", ttl_offset=3600)
        # 加一个非 json 文件
        with open(os.path.join(tmp_store.data_dir, "note.txt"), "w") as f:
            f.write("x")
        removed = await tmp_store.evict_lru()
        assert removed == 0

    @pytest.mark.asyncio
    async def test_respects_custom_max_entries(self, tmp_path):
        store = FileStore(str(tmp_path), max_entries=2, ttl_minutes=60)
        for i in range(5):
            rid = f"resp_{i:03d}"
            _write_session(store, rid, ttl_offset=3600)
            path = store._file_path(rid)
            os.utime(path, (time.time() + i, time.time() + i))

        removed = await store.evict_lru()
        assert removed == 3

    @pytest.mark.asyncio
    async def test_uses_stored_last_access_at_when_mtime_ties(self, tmp_path):
        store = FileStore(str(tmp_path), max_entries=2, ttl_minutes=60)
        paths = []
        for rid, last_access_at in (
            ("resp_old", 100),
            ("resp_recent", 300),
            ("resp_middle", 200),
        ):
            path = _write_session(store, rid, ttl_offset=3600)
            with open(path, "r+", encoding="utf-8") as f:
                data = json.load(f)
                data["last_access_at"] = last_access_at
                f.seek(0)
                json.dump(data, f)
                f.truncate()
            paths.append(path)

        for path in paths:
            os.utime(path, (1_700_000_000, 1_700_000_000))

        removed = await store.evict_lru()

        assert removed == 1
        assert not os.path.exists(store._file_path("resp_old"))
        assert os.path.exists(store._file_path("resp_middle"))
        assert os.path.exists(store._file_path("resp_recent"))


# ═══════════════════════════════════════════
#  _cleanup_if_needed()
# ═══════════════════════════════════════════


class TestCleanupIfNeeded:
    @pytest.mark.asyncio
    async def test_combines_expired_and_lru(self, tmp_store):
        """先清理过期，再 LRU 淘汰"""
        # 2 个过期
        _write_session(tmp_store, "resp_expired1", ttl_offset=-100)
        _write_session(tmp_store, "resp_expired2", ttl_offset=-200)
        # 6 个有效（超出 max_entries=5）
        for i in range(6):
            rid = f"resp_{i:03d}"
            _write_session(tmp_store, rid, ttl_offset=3600)
            path = tmp_store._file_path(rid)
            os.utime(path, (time.time() + i, time.time() + i))

        await tmp_store._cleanup_if_needed()

        # 过期文件已清除
        assert not os.path.exists(tmp_store._file_path("resp_expired1"))
        assert not os.path.exists(tmp_store._file_path("resp_expired2"))

        # 过期清除后剩 6 个，LRU 淘汰 1 个（最旧的），最终剩 5 个
        remaining = [f for f in os.listdir(tmp_store.data_dir) if f.endswith(".json")]
        assert len(remaining) == 5


# ═══════════════════════════════════════════
#  get_response / get_conversation 过期检查
# ═══════════════════════════════════════════


class TestGetExpiredReturnsNone:
    @pytest.mark.asyncio
    async def test_get_response_returns_none_for_expired(self, tmp_store):
        _write_session(tmp_store, "resp_old", ttl_offset=-100)
        result = await tmp_store.get_response("resp_old")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_conversation_returns_none_for_expired(self, tmp_store):
        _write_session(tmp_store, "resp_old", ttl_offset=-100)
        result = await tmp_store.get_conversation("resp_old")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_response_returns_data_for_valid(self, tmp_store):
        _write_session(tmp_store, "resp_ok", ttl_offset=3600)
        result = await tmp_store.get_response("resp_ok")
        assert result is not None
        assert result["id"] == "resp_ok"
