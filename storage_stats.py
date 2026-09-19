"""Storage statistics and cleanup utilities.

月度分库（request_raw_logs）部分是纯视图（ADR-0017 D1）：文件名格式、目录
发现、db/-wal/-shm 伴随文件删除等 on-disk 知识单一属主是 request_logs 的
SQLiteRequestLogBackend，这里只消费其公开接口；通用文件系统 helper（目录
大小、文件列表）不涉及月库布局知识，保持本地实现。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime
from typing import Any

import config
from request_logs import SQLiteRequestLogBackend


def _month_store(raw_logs_dir: str) -> SQLiteRequestLogBackend:
    """构造绑定给定月库目录的后端实例——月度分库 on-disk 知识全在后端（ADR-0017 D1）。

    db_path 仅为构造签名占位：视图操作（枚举 / 详情 / 删除 / 预览）只消费
    月库目录与月库路径，从不读写该入口库。
    """
    return SQLiteRequestLogBackend(os.path.join(raw_logs_dir, "request_logs.db"), logs_dir=raw_logs_dir)


async def get_directory_size(path: str) -> int:
    """Calculate total size of all files in directory recursively."""

    def _sync() -> int:
        total = 0
        if not os.path.isdir(path):
            return total
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.isfile(fp):
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(fp)
        return total

    return await asyncio.to_thread(_sync)


async def discover_month_dbs(raw_logs_dir: str) -> list[str]:
    """Discover all month database files in raw logs directory.

    Returns sorted list of YYYYMM strings. 目录发现转调后端公开接口：
    容差归一（isdigit），非数字命名的杂散文件一律忽略。
    """

    def _sync() -> list[str]:
        return _month_store(raw_logs_dir).discover_month_dbs()

    return await asyncio.to_thread(_sync)


async def get_month_db_details(raw_logs_dir: str, month: str) -> dict[str, Any] | None:
    """Get details for a specific month database.

    Args:
        raw_logs_dir: Directory containing month databases.
        month: Month string in YYYYMM format.

    Returns:
        Dictionary with month/file/size/record_count, or None if not found.
    """

    def _sync() -> dict[str, Any] | None:
        store = _month_store(raw_logs_dir)
        if not store.is_valid_month_key(month):
            return None
        db_path = store.month_db_path(month)
        if not os.path.exists(db_path):
            return None

        record_count = store.month_db_record_count(db_path)

        try:
            stat = os.stat(db_path)
        except OSError:
            return None
        return {
            "month": f"{month[:4]}-{month[4:]}",
            "file": os.path.basename(db_path),
            "size": stat.st_size,
            "record_count": record_count,
        }

    return await asyncio.to_thread(_sync)


def _get_logs_dir() -> str:
    """Get the logs directory path. Override-able for tests."""
    project_root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(project_root, "logs")


async def _get_all_month_details(raw_logs_dir: str) -> list[dict[str, Any]]:
    """Get details for all month databases."""
    months = await discover_month_dbs(raw_logs_dir)
    details: list[dict[str, Any]] = []
    for month in months:
        detail = await get_month_db_details(raw_logs_dir, month)
        if detail:
            details.append(detail)
    return details


async def _get_other_data_size(data_dir: str, raw_logs_dir: str) -> int:
    """Get size of other data files (excluding raw_logs subdirectory)."""

    def _sync() -> int:
        total = 0
        if not os.path.isdir(data_dir):
            return total
        raw_logs_abs = os.path.abspath(raw_logs_dir)
        for dirpath, _, filenames in os.walk(data_dir):
            if os.path.abspath(dirpath) == raw_logs_abs:
                continue
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.isfile(fp):
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(fp)
        return total

    return await asyncio.to_thread(_sync)


async def _get_other_data_files(data_dir: str) -> list[dict[str, Any]]:
    """List other data files (top-level only, no recursion)."""

    def _sync() -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        if not os.path.isdir(data_dir):
            return files
        for f in os.listdir(data_dir):
            fp = os.path.join(data_dir, f)
            if os.path.isfile(fp):
                try:
                    stat = os.stat(fp)
                    files.append({"name": f, "size": stat.st_size})
                except OSError:
                    pass
        return files

    return await asyncio.to_thread(_sync)


async def get_storage_stats() -> dict[str, Any]:
    """Get complete storage statistics for logs, request_raw_logs, and other data.

    Returns a dict with keys:
        total_size: Total bytes across all three sections.
        logs: {path, size, files: [{name, size, modified}]}
        request_raw_logs: {path, size, months: [{month, file, size, record_count}]}
        other_data: {path, size, files: [{name, size}]}
    """
    logs_dir = _get_logs_dir()
    data_dir = config.DATA_DIR
    raw_logs_dir = os.path.join(data_dir, "request_raw_logs")

    (
        logs_size,
        logs_files,
        raw_logs_size,
        months,
        other_size,
        other_files,
    ) = await asyncio.gather(
        get_directory_size(logs_dir),
        list_files_in_directory(logs_dir),
        get_directory_size(raw_logs_dir),
        _get_all_month_details(raw_logs_dir),
        _get_other_data_size(data_dir, raw_logs_dir),
        _get_other_data_files(data_dir),
    )

    return {
        "total_size": logs_size + raw_logs_size + other_size,
        "logs": {
            "path": "logs/",
            "size": logs_size,
            "files": logs_files,
        },
        "request_raw_logs": {
            "path": "data/request_raw_logs/",
            "size": raw_logs_size,
            "months": months,
        },
        "other_data": {
            "path": "data/",
            "size": other_size,
            "files": other_files,
        },
    }


async def list_files_in_directory(path: str) -> list[dict[str, Any]]:
    """List all files in directory with size and modified time."""

    def _sync() -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        if not os.path.isdir(path):
            return files
        for f in os.listdir(path):
            fp = os.path.join(path, f)
            if os.path.isfile(fp):
                try:
                    stat = os.stat(fp)
                    files.append(
                        {
                            "name": f,
                            "size": stat.st_size,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        }
                    )
                except OSError:
                    pass
        return files

    return await asyncio.to_thread(_sync)


async def cleanup_month(raw_logs_dir: str, month: str) -> dict[str, Any]:
    """Delete a specific month's database files (-wal, -shm included).

    Args:
        raw_logs_dir: Directory containing month databases.
        month: Month string in YYYYMM format.

    Returns:
        Dict with success/message/freed_bytes/removed_files. 文件删除转调后端
        公开接口，三件套（db/-wal/-shm）连根清掉。
    """

    def _sync() -> dict[str, Any]:
        store = _month_store(raw_logs_dir)
        if not store.is_valid_month_key(month):
            return {"success": False, "message": f"月份格式错误: {month}"}

        db_path = store.month_db_path(month)
        if not os.path.exists(db_path):
            return {
                "success": False,
                "message": f"月份 {month[:4]}-{month[4:]} 数据库不存在",
            }

        removed = store.remove_month_db_files(db_path)
        if not removed:
            return {
                "success": False,
                "message": f"月份 {month[:4]}-{month[4:]} 数据库文件无法删除",
            }

        return {
            "success": True,
            "message": f"已删除 {month[:4]}-{month[4:]} 月份数据",
            "freed_bytes": sum(size for _, size in removed),
            "removed_files": [name for name, _ in removed],
        }

    return await asyncio.to_thread(_sync)


async def preview_cleanup(
    raw_logs_dir: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Preview what would be deleted by a cleanup action (no actual deletion).

    Args:
        raw_logs_dir: Path to request_raw_logs directory
            (required for delete_month).
        target: Month string in YYYYMM format (required for delete_month).

    Returns:
        Dict with will_delete/freed_bytes (and target for delete_month),
        or {"success": False, "message": ...} if parameters are missing.
    """
    if not raw_logs_dir or not target:
        return {
            "success": False,
            "message": "delete_month 需要 raw_logs_dir 和 target 参数",
        }
    return await _preview_delete_month(raw_logs_dir, target)


async def _preview_delete_month(raw_logs_dir: str, target: str) -> dict[str, Any]:
    """Preview deletion of a specific month database. 三件套清单转调后端，不产生实际删除。"""

    def _sync() -> dict[str, Any]:
        store = _month_store(raw_logs_dir)
        if not store.is_valid_month_key(target):
            return {"success": False, "message": f"月份格式错误: {target}"}

        db_path = store.month_db_path(target)
        siblings = store.month_db_sibling_files(db_path)

        return {
            "success": True,
            "action": "delete_month",
            "target": f"{target[:4]}-{target[4:]}",
            "will_delete": [path for path, _ in siblings],
            "freed_bytes": sum(size for _, size in siblings),
        }

    return await asyncio.to_thread(_sync)
