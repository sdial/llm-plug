#!/usr/bin/env python3
"""手动迁移请求日志月度库：为 request_logs 表补充 requested_model 列。

背景
----
requested_model 用于记录客户端请求的模型名（模型组请求时为组名）。
新分月表由 _ensure_month_db 按新 schema 自动创建，自带该列；
只有升级前已存在的历史月度库缺少这一列，需要手动执行本脚本补齐。

注意
----
这是「纯手动、无容错」迁移：在历史库执行本脚本补齐列之前，
代码引用 requested_model 会让这些月份查询/写入报 no such column。
请升级后尽早运行本脚本。脚本幂等，可重复执行。

用法
----
    uv run python scripts/migrate_request_logs.py             # 执行迁移
    uv run python scripts/migrate_request_logs.py --dry-run   # 仅预览，不修改
    uv run python scripts/migrate_request_logs.py --path <dir>  # 指定 data 目录
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# 保证从项目根 import config（脚本位于 scripts/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

import config

COLUMN = "requested_model"


def discover_month_dbs(logs_dir: str) -> list[str]:
    if not os.path.isdir(logs_dir):
        return []
    paths = []
    for path in glob.glob(os.path.join(logs_dir, "request_logs_????_??.sqlite3")):
        basename = os.path.basename(path)
        parts = basename.replace("request_logs_", "").replace(".sqlite3", "").split("_")
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            paths.append(path)
    return sorted(paths)


def has_column(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(request_logs)").fetchall()
    return any(row[1] == COLUMN for row in rows)


def logs_dir_for(data_dir: str) -> str:
    # 月度库位于 request_logs.db 同级的 request_raw_logs/ 下
    db_path = os.environ.get("REQUEST_LOG_SQLITE_PATH", os.path.join(data_dir, "request_logs.db"))
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "request_raw_logs")


def migrate_one(db_path: str, dry_run: bool) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        if has_column(conn):
            return "skip"
        if dry_run:
            return "pending"
        conn.execute(f"ALTER TABLE request_logs ADD COLUMN {COLUMN} TEXT")
        return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移请求日志月度库，补充 requested_model 列")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际修改")
    parser.add_argument("--path", default=None, help="data 目录（默认用 config.DATA_DIR）")
    args = parser.parse_args()

    data_dir = args.path or config.DATA_DIR
    logs_dir = logs_dir_for(data_dir)
    month_dbs = discover_month_dbs(logs_dir)

    if not month_dbs:
        logger.info(f"未发现月度库: {logs_dir}")
        return 0

    logger.info("发现 {} 个月度库，目录: {}", len(month_dbs), logs_dir)
    changed = 0
    for db_path in month_dbs:
        try:
            status = migrate_one(db_path, args.dry_run)
        except sqlite3.OperationalError as exc:
            logger.error("{}: 迁移失败: {}", os.path.basename(db_path), exc)
            continue
        if status == "ok":
            changed += 1
            logger.info("{}: 已补齐 {} 列", os.path.basename(db_path), COLUMN)
        elif status == "pending":
            changed += 1
            logger.info("{}: [dry-run] 需要补齐 {} 列", os.path.basename(db_path), COLUMN)
        else:
            logger.info("{}: 已存在 {} 列，跳过", os.path.basename(db_path), COLUMN)

    action = "预览（未修改）" if args.dry_run else "已迁移"
    logger.info("完成：{} 个月度库{}", changed, action)
    return 0


if __name__ == "__main__":
    sys.exit(main())
