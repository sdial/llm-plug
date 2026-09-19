"""窗口级限速硬限制存储。

内存 map 为准，变更即写穿到 data/channel_quota_limits.json（原子写）。
重启后从文件重载，避免窗口内重启服务导致重新徒劳重试。
"""

import contextlib
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from typing import Any

import config


def _path() -> str:
    return os.path.join(config.DATA_DIR, "channel_quota_limits.json")


def _now() -> datetime:
    return datetime.now(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _atomic_write(data: dict[str, Any]) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = _path()
    dir_name = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".quota_limits_",
        suffix=".tmp.json",
    ) as f:
        tmp_path = f.name
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


class QuotaLimitStore:
    def __init__(self) -> None:
        self._limits: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self) -> None:
        self._limits = {}
        if not os.path.exists(_path()):
            return
        try:
            with open(_path(), encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            return
        now = _now()
        for ch_id, entry in raw.items():
            try:
                reset_at = _to_utc(datetime.fromisoformat(entry["reset_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if reset_at <= now:
                continue
            self._limits[ch_id] = {
                "reset_at": reset_at,
                "code": entry.get("code", ""),
            }

    def mark_blocked(self, channel_id: str, reset_at: datetime, code: str) -> None:
        reset_at = _to_utc(reset_at)
        self._mutate(lambda: self._limits.__setitem__(channel_id, {"reset_at": reset_at, "code": code}))

    def has_active_window(self, channel_id: str) -> bool:
        """渠道是否有未过期的窗口硬限制条目（reset_at 在未来）。

        持久化视图语义（ADR-0021 D3）：与 ``proxy.outcomes.is_blocked``（内存
        实时视图）同名异义已拆除，准入语境下 ``is_blocked`` 唯一指向 outcomes。
        """
        entry = self._limits.get(channel_id)
        if entry is None:
            return False
        if entry["reset_at"] <= _now():
            self._limits.pop(channel_id, None)
            return False
        return True

    def unblock(self, channel_id: str) -> None:
        self._mutate(lambda: self._limits.pop(channel_id, None))

    def cleanup(self, active_channel_ids: set[str]) -> None:
        now = _now()
        removed = [ch_id for ch_id, entry in self._limits.items() if ch_id not in active_channel_ids or entry["reset_at"] <= now]
        if removed:
            self._mutate(lambda: [self._limits.pop(ch_id) for ch_id in removed])

    def _mutate(self, fn) -> None:
        # 磁盘写穿在锁内完成，避免并发 mark/unblock 丢失更新
        with self._lock:
            fn()
            self._save_locked()

    def _save_locked(self) -> None:
        serialized = {ch_id: {"reset_at": entry["reset_at"].isoformat(), "code": entry["code"]} for ch_id, entry in self._limits.items()}
        _atomic_write(serialized)


store = QuotaLimitStore()
