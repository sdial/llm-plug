"""
统计数据模块 - 使用 SQLite 存储请求统计
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import DATA_DIR

STATS_DB_PATH = Path(DATA_DIR) / "stats.db"
_db_lock = threading.Lock()


@contextmanager
def _get_conn():
    """获取数据库连接（线程安全）"""
    with _db_lock:
        conn = sqlite3.connect(str(STATS_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def init_db():
    """初始化数据库表"""
    STATS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                model TEXT NOT NULL,
                is_stream INTEGER NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                latency_ms INTEGER NOT NULL,
                success INTEGER NOT NULL,
                error_msg TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_requests INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
            CREATE INDEX IF NOT EXISTS idx_requests_channel ON requests(channel_id);
            CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
        """)
        # Migration: add api_key_id column if not exists
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN api_key_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_api_key ON requests(api_key_id)")
        conn.commit()


def record_request(
    channel_id: str,
    channel_name: str,
    model: str,
    is_stream: bool,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    success: bool,
    error_msg: str | None = None,
    api_key_id: str | None = None,
):
    """记录一次请求"""
    now = datetime.now()
    timestamp = now.isoformat()
    date_str = now.strftime("%Y-%m-%d")

    with _get_conn() as conn:
        # 插入请求记录
        conn.execute(
            """
            INSERT INTO requests
            (timestamp, channel_id, channel_name, model, is_stream, input_tokens, output_tokens, latency_ms, success, error_msg, api_key_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, channel_id, channel_name, model, int(is_stream), input_tokens, output_tokens, latency_ms, int(success), error_msg, api_key_id),
        )

        # 更新每日汇总（使用 UPSERT）
        conn.execute(
            """
            INSERT INTO daily_stats (date, total_requests, success_count, fail_count, total_input_tokens, total_output_tokens)
            VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_requests = total_requests + 1,
                success_count = success_count + ?,
                fail_count = fail_count + ?,
                total_input_tokens = total_input_tokens + ?,
                total_output_tokens = total_output_tokens + ?
            """,
            (date_str, int(success), int(not success), input_tokens, output_tokens, int(success), int(not success), input_tokens, output_tokens),
        )

        conn.commit()


def get_overall_stats() -> dict[str, Any]:
    """获取总体统计数据"""
    with _get_conn() as conn:
        # 总体统计
        row = conn.execute("""
            SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as fail_count,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens
            FROM requests
        """).fetchone()

        # 渠道分布
        channel_rows = conn.execute("""
            SELECT channel_name, COUNT(*) as count
            FROM requests
            GROUP BY channel_id, channel_name
            ORDER BY count DESC
        """).fetchall()

        # 模型分布
        model_rows = conn.execute("""
            SELECT model, COUNT(*) as count
            FROM requests
            GROUP BY model
            ORDER BY count DESC
            LIMIT 20
        """).fetchall()

        # API Key 分布
        key_rows = conn.execute("""
            SELECT api_key_id, COUNT(*) as count,
                   COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens
            FROM requests
            WHERE api_key_id IS NOT NULL AND api_key_id != ''
            GROUP BY api_key_id
            ORDER BY count DESC
        """).fetchall()

        return {
            "total_requests": row["total_requests"] or 0,
            "success_count": row["success_count"] or 0,
            "fail_count": row["fail_count"] or 0,
            "total_input_tokens": row["total_input_tokens"] or 0,
            "total_output_tokens": row["total_output_tokens"] or 0,
            "channels": [{"name": r["channel_name"], "count": r["count"]} for r in channel_rows],
            "models": [{"name": r["model"], "count": r["count"]} for r in model_rows],
            "api_keys": [{"key_id": r["api_key_id"], "count": r["count"], "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]} for r in key_rows],
        }


def get_daily_stats(days: int = 7) -> list[dict[str, Any]]:
    """获取最近 N 天的每日统计"""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT date, total_requests, success_count, fail_count, total_input_tokens, total_output_tokens
            FROM daily_stats
            WHERE date >= date('now', 'localtime', ?)
            ORDER BY date ASC
            """,
            (f"-{days - 1} days",),
        ).fetchall()

        return [
            {
                "date": r["date"],
                "total_requests": r["total_requests"],
                "success_count": r["success_count"],
                "fail_count": r["fail_count"],
                "total_input_tokens": r["total_input_tokens"],
                "total_output_tokens": r["total_output_tokens"],
            }
            for r in rows
        ]


def cleanup_old_data(keep_days: int) -> int:
    """清理 N 天前的数据，返回删除的记录数"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")

    with _get_conn() as conn:
        # 删除请求记录
        cursor = conn.execute("DELETE FROM requests WHERE date(timestamp) < ?", (cutoff,))
        deleted_count = cursor.rowcount

        # 删除每日汇总
        conn.execute("DELETE FROM daily_stats WHERE date < ?", (cutoff,))

        conn.commit()

    return deleted_count


# 模块加载时初始化数据库
init_db()
