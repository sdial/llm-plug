import os
import sqlite3
from contextlib import closing

import pytest

import config
import storage_stats

pytestmark = pytest.mark.asyncio


async def test_get_directory_size_empty(tmp_path):
    result = await storage_stats.get_directory_size(str(tmp_path))
    assert result == 0


async def test_get_directory_size_with_files(tmp_path):
    (tmp_path / "file1.txt").write_text("hello")
    (tmp_path / "file2.txt").write_text("world")
    result = await storage_stats.get_directory_size(str(tmp_path))
    assert result == 10  # 5 + 5


async def test_list_files_in_directory(tmp_path):
    (tmp_path / "file1.txt").write_text("hello")
    (tmp_path / "file2.log").write_text("world")
    result = await storage_stats.list_files_in_directory(str(tmp_path))
    assert len(result) == 2
    file1 = next(f for f in result if f["name"] == "file1.txt")
    file2 = next(f for f in result if f["name"] == "file2.log")
    assert file1["size"] == 5
    assert file2["size"] == 5
    assert "modified" in file1
    assert "modified" in file2


async def test_discover_month_dbs(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    (raw_logs_dir / "request_logs_2026_05.sqlite3").touch()
    (raw_logs_dir / "request_logs_2026_06.sqlite3").touch()
    (raw_logs_dir / "request_logs_2026_07.sqlite3").touch()
    (raw_logs_dir / "other_file.txt").touch()

    result = await storage_stats.discover_month_dbs(str(raw_logs_dir))
    assert len(result) == 3
    assert "202605" in result
    assert "202606" in result
    assert "202607" in result


async def test_discover_month_dbs_empty(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    result = await storage_stats.discover_month_dbs(str(raw_logs_dir))
    assert result == []


async def test_discover_month_dbs_missing_dir(tmp_path):
    raw_logs_dir = tmp_path / "nonexistent"
    result = await storage_stats.discover_month_dbs(str(raw_logs_dir))
    assert result == []


async def test_get_month_db_details(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    db_path = raw_logs_dir / "request_logs_2026_07.sqlite3"

    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE request_logs (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO request_logs VALUES (1)")
        conn.execute("INSERT INTO request_logs VALUES (2)")
        conn.commit()

    result = await storage_stats.get_month_db_details(str(raw_logs_dir), "202607")
    assert result is not None
    assert result["month"] == "2026-07"
    assert result["file"] == "request_logs_2026_07.sqlite3"
    assert result["size"] > 0
    assert result["record_count"] == 2


async def test_get_month_db_details_missing(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    result = await storage_stats.get_month_db_details(str(raw_logs_dir), "202607")
    assert result is None


async def test_get_month_db_details_no_table(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    db_path = raw_logs_dir / "request_logs_2026_07.sqlite3"

    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY)")
        conn.commit()

    result = await storage_stats.get_month_db_details(str(raw_logs_dir), "202607")
    assert result is not None
    assert result["record_count"] == 0


async def test_get_storage_stats_structure(tmp_path, monkeypatch):
    # 准备测试数据
    project_root = tmp_path
    logs_dir = project_root / "logs"
    logs_dir.mkdir()
    (logs_dir / "warning.log").write_text("warning content")
    (logs_dir / "error.log").write_text("error content")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    raw_logs_dir = data_dir / "request_raw_logs"
    raw_logs_dir.mkdir()
    (raw_logs_dir / "request_logs_2026_07.sqlite3").touch()

    (data_dir / "request_logs.db").touch()
    (data_dir / "stats.db").touch()

    # 模拟 config.DATA_DIR 和项目根目录
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(storage_stats, "_get_logs_dir", lambda: str(logs_dir))

    result = await storage_stats.get_storage_stats()

    assert "total_size" in result
    assert isinstance(result["total_size"], int)
    assert "logs" in result
    assert "request_raw_logs" in result
    assert "other_data" in result

    assert result["logs"]["path"] == "logs/"
    assert result["logs"]["size"] > 0
    assert len(result["logs"]["files"]) == 2
    assert any(f["name"] == "warning.log" for f in result["logs"]["files"])
    assert any(f["name"] == "error.log" for f in result["logs"]["files"])

    assert result["request_raw_logs"]["path"] == "data/request_raw_logs/"
    assert len(result["request_raw_logs"]["months"]) == 1
    assert result["request_raw_logs"]["months"][0]["month"] == "2026-07"

    assert result["other_data"]["path"] == "data/"
    other_names = [f["name"] for f in result["other_data"]["files"]]
    assert "request_logs.db" in other_names
    assert "stats.db" in other_names


async def test_get_storage_stats_empty_directories(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(storage_stats, "_get_logs_dir", lambda: str(logs_dir))

    result = await storage_stats.get_storage_stats()

    assert result["total_size"] == 0
    assert result["logs"]["size"] == 0
    assert result["logs"]["files"] == []
    assert result["request_raw_logs"]["size"] == 0
    assert result["request_raw_logs"]["months"] == []
    assert result["other_data"]["size"] == 0
    assert result["other_data"]["files"] == []


async def test_get_storage_stats_other_data_excludes_raw_logs(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    raw_logs_dir = data_dir / "request_raw_logs"
    raw_logs_dir.mkdir()
    # 创建真正的 SQLite 数据库
    db_path = raw_logs_dir / "request_logs_2026_07.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE request_logs (id INTEGER PRIMARY KEY)")
        conn.commit()
    db_size = os.path.getsize(str(db_path))

    (data_dir / "other.db").write_bytes(b"y" * 50)

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(storage_stats, "_get_logs_dir", lambda: str(logs_dir))

    result = await storage_stats.get_storage_stats()

    # other_data 大小应只包含 other.db（50），不包含 request_raw_logs 中的数据库
    assert result["other_data"]["size"] == 50
    assert result["request_raw_logs"]["size"] >= db_size


async def test_cleanup_month(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    db_path = raw_logs_dir / "request_logs_2026_06.sqlite3"
    db_path.write_bytes(b"x" * 1000)
    wal_path = raw_logs_dir / "request_logs_2026_06.sqlite3-wal"
    wal_path.write_bytes(b"x" * 100)
    shm_path = raw_logs_dir / "request_logs_2026_06.sqlite3-shm"
    shm_path.write_bytes(b"x" * 50)

    result = await storage_stats.cleanup_month(str(raw_logs_dir), "202606")

    assert result["success"] is True
    assert result["freed_bytes"] == 1150
    assert not db_path.exists()
    assert not wal_path.exists()
    assert not shm_path.exists()
    assert "removed_files" in result
    assert len(result["removed_files"]) == 3


async def test_cleanup_month_only_db(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    db_path = raw_logs_dir / "request_logs_2026_06.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE request_logs (id INTEGER PRIMARY KEY)")
        conn.commit()
    db_size = os.path.getsize(str(db_path))

    result = await storage_stats.cleanup_month(str(raw_logs_dir), "202606")

    assert result["success"] is True
    assert result["freed_bytes"] == db_size
    assert not db_path.exists()
    assert "removed_files" in result
    assert len(result["removed_files"]) == 1


async def test_cleanup_month_missing(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()

    result = await storage_stats.cleanup_month(str(raw_logs_dir), "202606")

    assert result["success"] is False
    assert "不存在" in result["message"]


async def test_cleanup_month_invalid_format(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()

    result = await storage_stats.cleanup_month(str(raw_logs_dir), "invalid")
    assert result["success"] is False
    assert "月份格式错误" in result["message"]


async def test_cleanup_month_non_numeric_6char(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()

    result = await storage_stats.cleanup_month(str(raw_logs_dir), "abcdef")
    assert result["success"] is False
    assert "月份格式错误" in result["message"]


async def test_get_month_db_details_invalid_format(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()

    result = await storage_stats.get_month_db_details(str(raw_logs_dir), "abcdef")
    assert result is None


async def test_preview_cleanup_delete_month(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()
    db_path = raw_logs_dir / "request_logs_2026_06.sqlite3"
    db_path.write_bytes(b"x" * 1000)
    wal_path = raw_logs_dir / "request_logs_2026_06.sqlite3-wal"
    wal_path.write_bytes(b"x" * 100)
    shm_path = raw_logs_dir / "request_logs_2026_06.sqlite3-shm"
    shm_path.write_bytes(b"x" * 50)

    result = await storage_stats.preview_cleanup(
        str(raw_logs_dir), "202606"
    )

    assert result["action"] == "delete_month"
    assert result["target"] == "2026-06"
    assert result["freed_bytes"] == 1150
    assert len(result["will_delete"]) == 3
    assert db_path.exists()  # 预览不应实际删除
    assert wal_path.exists()
    assert shm_path.exists()


async def test_preview_cleanup_delete_month_missing(tmp_path):
    raw_logs_dir = tmp_path / "request_raw_logs"
    raw_logs_dir.mkdir()

    result = await storage_stats.preview_cleanup(
        str(raw_logs_dir), "202606"
    )

    assert result["action"] == "delete_month"
    assert result["target"] == "2026-06"
    assert result["freed_bytes"] == 0
    assert result["will_delete"] == []


async def test_preview_cleanup_delete_month_missing_params():
    result = await storage_stats.preview_cleanup()

    assert result["success"] is False
    assert result["message"] == "delete_month 需要 raw_logs_dir 和 target 参数"

