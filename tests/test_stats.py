import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
import stats


@pytest.fixture(autouse=True)
def isolate_stats_db(tmp_path, monkeypatch):
    """每个测试使用独立的 SQLite 数据库。"""
    db_path = tmp_path / "test_stats.db"
    monkeypatch.setattr(stats, "STATS_DB_PATH", db_path)
    # 重新初始化数据库（会创建新表）
    stats.init_db()
    yield
    # 可选：关闭所有连接，确保文件可删除
    # SQLite 文件在连接关闭后自动释放


class TestInitDb:
    def test_creates_requests_table(self):
        conn = sqlite3.connect(str(stats.STATS_DB_PATH))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='requests'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_creates_daily_stats_table(self):
        conn = sqlite3.connect(str(stats.STATS_DB_PATH))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_stats'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_creates_indexes(self):
        conn = sqlite3.connect(str(stats.STATS_DB_PATH))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_requests_timestamp'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_migration_adds_api_key_id_column(self):
        # 模拟旧数据库：只创建表，不添加 api_key_id
        conn = sqlite3.connect(str(stats.STATS_DB_PATH))
        conn.execute("DROP TABLE IF EXISTS requests")
        conn.execute("""
            CREATE TABLE requests (
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
            )
        """)
        conn.commit()
        conn.close()

        # 重新 init_db 应执行迁移添加列
        stats.init_db()

        conn = sqlite3.connect(str(stats.STATS_DB_PATH))
        cursor = conn.execute("PRAGMA table_info(requests)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "api_key_id" in columns
        conn.close()


class TestRecordRequest:
    def test_inserts_request_row(self):
        stats.record_request(
            channel_id="ch_1",
            channel_name="Test Channel",
            model="gpt-4",
            is_stream=False,
            input_tokens=100,
            output_tokens=50,
            latency_ms=200,
            success=True,
            error_msg=None,
            api_key_id="key_1",
        )

        with stats._get_conn() as conn:
            row = conn.execute("SELECT * FROM requests").fetchone()
            assert row["channel_id"] == "ch_1"
            assert row["channel_name"] == "Test Channel"
            assert row["model"] == "gpt-4"
            assert row["is_stream"] == 0
            assert row["input_tokens"] == 100
            assert row["output_tokens"] == 50
            assert row["latency_ms"] == 200
            assert row["success"] == 1
            assert row["error_msg"] is None
            assert row["api_key_id"] == "key_1"

    def test_creates_daily_stats_on_first_request(self):
        stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=False,
            input_tokens=10,
            output_tokens=20,
            latency_ms=100,
            success=True,
        )

        with stats._get_conn() as conn:
            row = conn.execute("SELECT * FROM daily_stats").fetchone()
            today = datetime.now().strftime("%Y-%m-%d")
            assert row["date"] == today
            assert row["total_requests"] == 1
            assert row["success_count"] == 1
            assert row["fail_count"] == 0
            assert row["total_input_tokens"] == 10
            assert row["total_output_tokens"] == 20

    def test_increments_daily_stats_on_subsequent_requests(self):
        for _ in range(3):
            stats.record_request(
                channel_id="ch_1",
                channel_name="Test",
                model="gpt-4",
                is_stream=False,
                input_tokens=10,
                output_tokens=20,
                latency_ms=100,
                success=True,
            )

        with stats._get_conn() as conn:
            row = conn.execute("SELECT * FROM daily_stats").fetchone()
            assert row["total_requests"] == 3
            assert row["success_count"] == 3
            assert row["total_input_tokens"] == 30
            assert row["total_output_tokens"] == 60

    def test_records_failure_correctly(self):
        stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=True,
            input_tokens=0,
            output_tokens=0,
            latency_ms=50,
            success=False,
            error_msg="timeout",
        )

        with stats._get_conn() as conn:
            row = conn.execute("SELECT * FROM daily_stats").fetchone()
            assert row["total_requests"] == 1
            assert row["success_count"] == 0
            assert row["fail_count"] == 1

    def test_stream_bool_converted_to_integer(self):
        stats.record_request(
            channel_id="ch_1",
            channel_name="Test",
            model="gpt-4",
            is_stream=True,
            input_tokens=0,
            output_tokens=0,
            latency_ms=50,
            success=True,
        )

        with stats._get_conn() as conn:
            row = conn.execute("SELECT is_stream FROM requests").fetchone()
            assert row["is_stream"] == 1


class TestGetOverallStats:
    def test_returns_zero_when_empty(self):
        result = stats.get_overall_stats()
        assert result["total_requests"] == 0
        assert result["success_count"] == 0
        assert result["fail_count"] == 0
        assert result["total_input_tokens"] == 0
        assert result["total_output_tokens"] == 0
        assert result["channels"] == []
        assert result["models"] == []

    def test_aggregates_multiple_requests(self):
        stats.record_request("ch_1", "Chan A", "gpt-4", False, 100, 50, 200, True)
        stats.record_request("ch_1", "Chan A", "gpt-4", False, 200, 100, 300, True)
        stats.record_request("ch_2", "Chan B", "gpt-3.5", False, 50, 25, 100, False, error_msg="err")

        result = stats.get_overall_stats()
        assert result["total_requests"] == 3
        assert result["success_count"] == 2
        assert result["fail_count"] == 1
        assert result["total_input_tokens"] == 350
        assert result["total_output_tokens"] == 175

    def test_groups_channels_and_models(self):
        stats.record_request("ch_1", "Chan A", "gpt-4", False, 10, 10, 100, True)
        stats.record_request("ch_1", "Chan A", "gpt-4", False, 10, 10, 100, True)
        stats.record_request("ch_2", "Chan B", "gpt-3.5", False, 10, 10, 100, True)

        result = stats.get_overall_stats()
        channels = {c["name"]: c["count"] for c in result["channels"]}
        assert channels == {"Chan A": 2, "Chan B": 1}

        models = {m["name"]: m["count"] for m in result["models"]}
        assert models == {"gpt-4": 2, "gpt-3.5": 1}

    def test_api_keys_stats_ignores_empty_key_id(self):
        stats.record_request("ch_1", "Chan A", "gpt-4", False, 10, 10, 100, True, api_key_id="key_1")
        stats.record_request("ch_1", "Chan A", "gpt-4", False, 10, 10, 100, True, api_key_id="")
        stats.record_request("ch_1", "Chan A", "gpt-4", False, 10, 10, 100, True, api_key_id=None)

        result = stats.get_overall_stats()
        assert len(result["api_keys"]) == 1
        assert result["api_keys"][0]["key_id"] == "key_1"


class TestGetDailyStats:
    def test_returns_empty_list_when_no_data(self):
        result = stats.get_daily_stats(days=7)
        assert result == []

    def test_returns_recent_days_only(self):
        today = datetime.now()
        # 插入今天的数据
        stats.record_request("ch_1", "Test", "gpt-4", False, 10, 10, 100, True)

        result = stats.get_daily_stats(days=7)
        assert len(result) == 1
        assert result[0]["date"] == today.strftime("%Y-%m-%d")

    def test_limits_to_requested_days(self):
        # 手动插入过去10天的数据
        with stats._get_conn() as conn:
            for i in range(10):
                date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT INTO daily_stats (date, total_requests) VALUES (?, 1)",
                    (date_str,),
                )
            conn.commit()

        result = stats.get_daily_stats(days=5)
        assert len(result) == 5


class TestCleanupOldData:
    def test_removes_old_requests(self):
        old_time = (datetime.now() - timedelta(days=10)).isoformat()
        old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")

        with stats._get_conn() as conn:
            conn.execute(
                "INSERT INTO requests (timestamp, channel_id, channel_name, model, is_stream, latency_ms, success) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (old_time, "ch_1", "Old", "gpt-4", 0, 100, 1),
            )
            conn.execute(
                "INSERT INTO daily_stats (date, total_requests) VALUES (?, 1)",
                (old_date,),
            )
            conn.commit()

        deleted = stats.cleanup_old_data(keep_days=5)
        assert deleted == 1

        with stats._get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            assert count == 0
            count_daily = conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0]
            assert count_daily == 0

    def test_keeps_recent_requests(self):
        stats.record_request("ch_1", "Test", "gpt-4", False, 10, 10, 100, True)

        deleted = stats.cleanup_old_data(keep_days=7)
        assert deleted == 0

        with stats._get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            assert count == 1

    def test_clears_all_data_with_zero_days(self):
        """keep_days=0 应清除所有数据"""
        # 插入一些测试数据
        stats.record_request("ch_1", "Test", "gpt-4", False, 10, 10, 100, True)
        old_time = (datetime.now() - timedelta(days=10)).isoformat()
        old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        with stats._get_conn() as conn:
            conn.execute(
                "INSERT INTO requests (timestamp, channel_id, channel_name, model, is_stream, latency_ms, success) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (old_time, "ch_2", "Old", "gpt-3", 0, 50, 1),
            )
            conn.execute(
                "INSERT INTO daily_stats (date, total_requests) VALUES (?, 1)",
                (old_date,),
            )
            conn.commit()

        # keep_days=0 应清除所有数据
        deleted = stats.cleanup_old_data(keep_days=0)
        assert deleted == 2  # 两条请求记录

        with stats._get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            assert count == 0
            count_daily = conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0]
            assert count_daily == 0
