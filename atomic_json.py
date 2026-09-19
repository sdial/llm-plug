"""可靠的原子 JSON 文件提交。"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from typing import Any

WINDOWS_REPLACE_ATTEMPTS = 5
WINDOWS_REPLACE_DELAYS = (0.01, 0.02, 0.04, 0.08)


def replace_with_retry(source: str, destination: str) -> None:
    """仅重试 Windows 瞬时共享冲突；其余错误立即透出。"""
    for attempt in range(WINDOWS_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            retryable = os.name == "nt" and getattr(exc, "winerror", None) in {5, 32}
            if not retryable or attempt == WINDOWS_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(WINDOWS_REPLACE_DELAYS[attempt])


def write_json_atomic(path: str, data: dict[str, Any], *, temp_prefix: str) -> None:
    """在目标目录落临时文件、刷盘、收紧权限，再原子替换目标。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            delete=False,
            prefix=temp_prefix,
            suffix=".tmp.json",
        ) as file:
            temp_path = file.name
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, 0o600)
        replace_with_retry(temp_path, path)
    except Exception:
        if temp_path:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
        raise
