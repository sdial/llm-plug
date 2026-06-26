import asyncio
import contextlib
import json
import os
import tempfile
import time
from typing import Any

from loguru import logger


class FileStore:
    """磁盘文件状态存储"""

    def __init__(self, data_dir: str, max_entries: int = 1000, ttl_minutes: int = 60):
        self.data_dir = data_dir
        self.max_entries = max_entries
        self.ttl_seconds = ttl_minutes * 60
        self._lock = asyncio.Lock()
        os.makedirs(data_dir, exist_ok=True)

    def generate_response_id(self) -> str:
        """生成 response_id: resp_ + 24字符hex"""
        import secrets
        return f"resp_{secrets.token_hex(12)}"

    def _file_path(self, response_id: str) -> str:
        if not response_id or "/" in response_id or "\\" in response_id:
            raise ValueError("response_id must not contain path separators")
        path = os.path.abspath(os.path.join(self.data_dir, f"{response_id}.json"))
        data_dir = os.path.abspath(self.data_dir)
        try:
            common = os.path.commonpath([path, data_dir])
        except ValueError:
            common = None
        if common != data_dir:
            raise ValueError("response_id must not escape data directory")
        return path

    def _read_file_sync(self, path: str) -> dict[str, Any] | None:
        """同步读取文件，返回 None 如果文件不存在或已过期"""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("expires_at", 0) < time.time():
                return None
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read session file {path}: {e}")
            return None

    def _touch_access_sync(self, path: str, data: dict[str, Any]) -> None:
        data["last_access_at"] = time.time()
        try:
            self._write_file_sync(path, data)
        except OSError as e:
            logger.warning(f"Failed to update session access time {path}: {e}")

    async def get_response(self, response_id: str) -> dict[str, Any] | None:
        """获取响应记录"""
        async with self._lock:
            try:
                path = self._file_path(response_id)
            except ValueError:
                return None
            data = await asyncio.to_thread(self._read_file_sync, path)
            if data is None:
                return None
            await asyncio.to_thread(self._touch_access_sync, path, data)
            return data.get("response")

    async def get_conversation(self, response_id: str) -> dict[str, Any] | None:
        """获取对话记录"""
        async with self._lock:
            try:
                path = self._file_path(response_id)
            except ValueError:
                return None
            data = await asyncio.to_thread(self._read_file_sync, path)
            if data is None:
                return None
            await asyncio.to_thread(self._touch_access_sync, path, data)
            return data.get("conversation")

    async def put(self, response_id: str, conversation: dict, response: dict) -> None:
        """存储会话记录"""
        async with self._lock:
            now = int(time.time())
            data = {
                "response_id": response_id,
                "conversation": conversation,
                "response": response,
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
                "last_access_at": now,
            }
            path = self._file_path(response_id)
            await asyncio.to_thread(self._write_file_sync, path, data)

    async def delete(self, response_id: str) -> bool:
        """删除会话记录"""
        async with self._lock:
            try:
                path = self._file_path(response_id)
            except ValueError:
                return False
            if not os.path.exists(path):
                return False
            try:
                await asyncio.to_thread(os.unlink, path)
            except OSError:
                return False
            else:
                return True

    def _write_file_sync(self, path: str, data: dict) -> None:
        """原子写入文件（同步）"""
        dir_name = os.path.dirname(os.path.abspath(path)) or "."
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dir_name, delete=False,
            prefix=".session_", suffix=".tmp.json",
        ) as f:
            tmp_path = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _cleanup_expired_sync(self) -> int:
        """同步清理过期文件"""
        now = int(time.time())
        removed = 0
        for filename in os.listdir(self.data_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.data_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("expires_at", 0) < now:
                    os.unlink(path)
                    removed += 1
            except (json.JSONDecodeError, OSError):
                continue
        return removed

    async def cleanup_expired(self) -> int:
        """清理过期文件"""
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_expired_sync)

    def _evict_lru_sync(self) -> int:
        """同步淘汰超出容量的最旧文件"""
        files = []
        for filename in os.listdir(self.data_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.data_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                access_time = float(data.get("last_access_at") or data.get("created_at") or os.path.getmtime(path))
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                continue
            files.append((path, access_time))

        if len(files) <= self.max_entries:
            return 0

        files.sort(key=lambda x: x[1])
        removed = 0
        for path, _ in files[:len(files) - self.max_entries]:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                continue
        return removed

    async def evict_lru(self) -> int:
        """淘汰超出容量的最旧文件（按 last_access_at LRU）"""
        async with self._lock:
            return await asyncio.to_thread(self._evict_lru_sync)

    async def _cleanup_if_needed(self) -> None:
        """执行清理：过期 + LRU"""
        await self.cleanup_expired()
        await self.evict_lru()
