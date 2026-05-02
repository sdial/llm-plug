import json
import os
import threading
import time

import pytest

import config
import storage


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    """每个测试使用独立的临时目录，避免相互污染。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))

    # 清除缓存状态
    storage.invalidate_cache()
    storage.invalidate_keys_cache()

    yield

    # teardown: 再次清理缓存
    storage.invalidate_cache()
    storage.invalidate_keys_cache()


class TestLoadData:
    def test_creates_default_file_when_missing(self):
        assert not os.path.exists(config.CHANNELS_FILE)
        data = storage.load_data()
        assert os.path.exists(config.CHANNELS_FILE)
        assert data == {"channels": []}

    def test_reads_existing_file(self):
        payload = {"channels": [{"id": "ch_1", "name": "test"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        storage.invalidate_cache()
        data = storage.load_data()
        assert data == payload

    def test_uses_cache_within_ttl(self):
        payload = {"channels": [{"id": "ch_1", "name": "first"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = storage.load_data()
        assert data1["channels"][0]["name"] == "first"

        # 直接修改底层文件，绕过缓存
        payload["channels"][0]["name"] = "second"
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        # 缓存仍然有效（5秒TTL），应返回旧数据
        data2 = storage.load_data()
        assert data2["channels"][0]["name"] == "first"

    def test_cache_expires_after_ttl(self):
        payload = {"channels": [{"id": "ch_1", "name": "first"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = storage.load_data()
        assert data1["channels"][0]["name"] == "first"

        # 修改底层文件
        payload["channels"][0]["name"] = "second"
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        # 强制让缓存过期（通过 monkeypatch 或直接修改内部状态）
        # 这里直接修改内部 _cache_ts 使其过期
        with storage.get_lock():
            storage._cache_ts = time.time() - 10  # 10秒前，超过5秒TTL

        data2 = storage.load_data()
        assert data2["channels"][0]["name"] == "second"


class TestSaveData:
    def test_writes_data_to_disk(self):
        payload = {"channels": [{"id": "ch_2", "name": "saved"}]}
        storage.save_data(payload)

        with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk == payload

    def test_updates_cache_after_save(self):
        payload = {"channels": [{"id": "ch_3", "name": "cached"}]}
        storage.save_data(payload)

        # 直接删除文件，验证缓存仍然能提供数据
        os.remove(config.CHANNELS_FILE)
        data = storage.load_data()
        assert data == payload

    def test_atomic_write(self):
        payload = {"channels": [{"id": "ch_4", "name": "atomic"}]}
        storage.save_data(payload)

        # 确认目标文件存在且是最终名称（而非临时文件）
        assert os.path.exists(config.CHANNELS_FILE)
        dir_files = os.listdir(os.path.dirname(config.CHANNELS_FILE))
        tmp_files = [f for f in dir_files if f.startswith(".channels_")]
        assert len(tmp_files) == 0, f"残留的临时文件: {tmp_files}"

    def test_invalid_json_does_not_corrupt_existing_file(self):
        # 先写入有效数据
        valid = {"channels": [{"id": "ch_5", "name": "safe"}]}
        storage.save_data(valid)

        # 模拟写入过程中抛出异常：通过 patch json.dump 使其失败
        import unittest.mock
        with unittest.mock.patch("json.dump", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                storage.save_data({"channels": []})

        # 原文件应保持不变
        with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk == valid


class TestInvalidateCache:
    def test_forces_next_load_from_disk(self):
        payload = {"channels": [{"id": "ch_6", "name": "original"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = storage.load_data()
        assert data1["channels"][0]["name"] == "original"

        # 修改文件
        payload["channels"][0]["name"] = "modified"
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        storage.invalidate_cache()
        data2 = storage.load_data()
        assert data2["channels"][0]["name"] == "modified"


class TestApiKeysStorage:
    def test_load_api_keys_creates_default_when_missing(self):
        assert not os.path.exists(config.API_KEYS_FILE)
        data = storage.load_api_keys()
        assert os.path.exists(config.API_KEYS_FILE)
        assert data == {"api_keys": []}

    def test_save_and_load_api_keys(self):
        payload = {"api_keys": [{"id": "key_1", "name": "test-key"}]}
        storage.save_api_keys(payload)
        storage.invalidate_keys_cache()
        data = storage.load_api_keys()
        assert data == payload

    def test_keys_cache_uses_ttl(self):
        payload = {"api_keys": [{"id": "key_1", "name": "first"}]}
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = storage.load_api_keys()
        assert data1["api_keys"][0]["name"] == "first"

        payload["api_keys"][0]["name"] = "second"
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        # 缓存未过期
        data2 = storage.load_api_keys()
        assert data2["api_keys"][0]["name"] == "first"

    def test_invalidate_keys_cache(self):
        payload = {"api_keys": [{"id": "key_1", "name": "original"}]}
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        storage.load_api_keys()
        payload["api_keys"][0]["name"] = "modified"
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        storage.invalidate_keys_cache()
        data = storage.load_api_keys()
        assert data["api_keys"][0]["name"] == "modified"


class TestConcurrency:
    def test_concurrent_load_data_is_thread_safe(self):
        payload = {"channels": list(range(1000))}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        storage.invalidate_cache()

        results = []

        def worker():
            data = storage.load_data()
            results.append(len(data["channels"]))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == 1000 for r in results)

    def test_concurrent_save_data_is_thread_safe(self):
        errors = []

        def worker(idx):
            try:
                storage.save_data({"channels": [{"id": f"ch_{idx}"}]})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"并发保存出错: {errors}"
        # 最终文件应包含某个有效 JSON
        data = storage.load_data()
        assert "channels" in data
