import asyncio
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
        return os.path.join(self.data_dir, f"{response_id}.json")

    async def get_response(self, response_id: str) -> dict[str, Any] | None:
        """获取响应记录"""
        async with self._lock:
            path = self._file_path(response_id)
            if not os.path.exists(path):
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("expires_at", 0) < time.time():
                    return None
                data["last_access_at"] = int(time.time())
                self._write_file(path, data)
                return data.get("response")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read session {response_id}: {e}")
                return None

    async def get_conversation(self, response_id: str) -> dict[str, Any] | None:
        """获取对话记录"""
        async with self._lock:
            path = self._file_path(response_id)
            if not os.path.exists(path):
                return None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("expires_at", 0) < time.time():
                    return None
                return data.get("conversation")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read session {response_id}: {e}")
                return None

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
            self._write_file(path, data)

    async def delete(self, response_id: str) -> bool:
        """删除会话记录"""
        async with self._lock:
            path = self._file_path(response_id)
            if os.path.exists(path):
                try:
                    os.unlink(path)
                    return True
                except OSError:
                    return False
            return False

    def _write_file(self, path: str, data: dict) -> None:
        """原子写入文件"""
        dir_name = os.path.dirname(os.path.abspath(path)) or "."
        f = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dir_name, delete=False,
            prefix=".session_", suffix=".tmp.json",
        )
        tmp_path = f.name
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(tmp_path, path)
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def cleanup_expired(self) -> int:
        """清理过期文件"""
        async with self._lock:
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

    async def evict_lru(self) -> int:
        """淘汰超出容量的最旧文件"""
        async with self._lock:
            files = []
            for filename in os.listdir(self.data_dir):
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(self.data_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    files.append((path, data.get("last_access_at", 0)))
                except (json.JSONDecodeError, OSError):
                    continue

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

    async def _cleanup_if_needed(self) -> None:
        """执行清理：过期 + LRU"""
        await self.cleanup_expired()
        await self.evict_lru()
