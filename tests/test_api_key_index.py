"""P1-6: main._get_api_key_index / _invalidate_api_key_index 缓存机制测试"""

import json

import pytest

import main


@pytest.fixture(autouse=True)
def _reset_index():
    """每个测试前后重置索引"""
    main._api_key_index = None
    yield
    main._api_key_index = None


class TestGetApiKeyIndex:

    @pytest.mark.asyncio
    async def test_first_call_builds_index(self, tmp_path, monkeypatch):
        """首次调用应从 storage 加载并构建索引"""
        keys_data = {
            "api_keys": [
                {"id": "k1", "name": "key1", "key": "llmplug-test-aaa"},
                {"id": "k2", "name": "key2", "key": "llmplug-test-bbb"},
            ]
        }
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(json.dumps(keys_data))
        monkeypatch.setattr("config.API_KEYS_FILE", str(keys_file))

        # 清除 storage 缓存
        import storage
        storage._keys_cache = None
        storage._keys_cache_ts = 0

        index = await main._get_api_key_index()
        assert "llmplug-test-aaa" in index
        assert "llmplug-test-bbb" in index
        assert index["llmplug-test-aaa"]["name"] == "key1"

    @pytest.mark.asyncio
    async def test_subsequent_call_returns_cached(self, tmp_path, monkeypatch):
        """后续调用应返回缓存，不重新加载"""
        keys_data = {
            "api_keys": [{"id": "k1", "name": "key1", "key": "llmplug-test-aaa"}]
        }
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(json.dumps(keys_data))
        monkeypatch.setattr("config.API_KEYS_FILE", str(keys_file))

        import storage
        storage._keys_cache = None
        storage._keys_cache_ts = 0

        index1 = await main._get_api_key_index()
        index2 = await main._get_api_key_index()
        assert index1 is index2  # 同一对象，说明使用了缓存

    @pytest.mark.asyncio
    async def test_skips_empty_key(self, tmp_path, monkeypatch):
        """空 key 值不应进入索引"""
        keys_data = {
            "api_keys": [
                {"id": "k1", "name": "key1", "key": "llmplug-test-aaa"},
                {"id": "k2", "name": "key2", "key": ""},
                {"id": "k3", "name": "key3"},  # 没有 key 字段
            ]
        }
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(json.dumps(keys_data))
        monkeypatch.setattr("config.API_KEYS_FILE", str(keys_file))

        import storage
        storage._keys_cache = None
        storage._keys_cache_ts = 0

        index = await main._get_api_key_index()
        assert len(index) == 1
        assert "llmplug-test-aaa" in index

    @pytest.mark.asyncio
    async def test_empty_api_keys_returns_empty_index(self, tmp_path, monkeypatch):
        keys_data = {"api_keys": []}
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(json.dumps(keys_data))
        monkeypatch.setattr("config.API_KEYS_FILE", str(keys_file))

        import storage
        storage._keys_cache = None
        storage._keys_cache_ts = 0

        index = await main._get_api_key_index()
        assert index == {}


class TestInvalidateApiKeyIndex:

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self, tmp_path, monkeypatch):
        keys_data = {
            "api_keys": [{"id": "k1", "name": "key1", "key": "llmplug-test-aaa"}]
        }
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(json.dumps(keys_data))
        monkeypatch.setattr("config.API_KEYS_FILE", str(keys_file))

        import storage
        storage._keys_cache = None
        storage._keys_cache_ts = 0

        # 加载索引
        index1 = await main._get_api_key_index()
        assert len(index1) == 1

        # 更新文件内容
        keys_data2 = {
            "api_keys": [
                {"id": "k1", "name": "key1", "key": "llmplug-test-aaa"},
                {"id": "k2", "name": "key2", "key": "llmplug-test-bbb"},
            ]
        }
        keys_file.write_text(json.dumps(keys_data2))
        storage._keys_cache = None
        storage._keys_cache_ts = 0

        # 失效
        main._invalidate_api_key_index()

        # 重新加载应得到新数据
        index2 = await main._get_api_key_index()
        assert len(index2) == 2

    def test_invalidate_when_not_loaded_is_noop(self):
        """索引未加载时调用 invalidate 不报错"""
        main._api_key_index = None
        main._invalidate_api_key_index()
        assert main._api_key_index is None
