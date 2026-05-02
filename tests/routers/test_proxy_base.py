import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(autouse=True)
def setup_test_data():
    """为每个测试设置隔离的数据目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建空的 channels.json 和 api_keys.json
        channels_file = os.path.join(tmpdir, "channels.json")
        api_keys_file = os.path.join(tmpdir, "api_keys.json")

        with open(channels_file, "w") as f:
            json.dump({"channels": []}, f)
        with open(api_keys_file, "w") as f:
            json.dump({"api_keys": []}, f)

        import config
        import storage
        old_data_dir = config.DATA_DIR
        old_channels_file = config.CHANNELS_FILE
        old_api_keys_file = config.API_KEYS_FILE

        config.DATA_DIR = tmpdir
        config.CHANNELS_FILE = channels_file
        config.API_KEYS_FILE = api_keys_file
        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._lock = None

        yield

        config.DATA_DIR = old_data_dir
        config.CHANNELS_FILE = old_channels_file
        config.API_KEYS_FILE = old_api_keys_file
        storage._cache = None
        storage._cache_ts = 0
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        storage._lock = None


def test_invalid_json_request_returns_400():
    """测试无效JSON请求返回400错误"""
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"not valid json {",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert "Invalid JSON" in response.text or "invalid" in response.text.lower()


def test_missing_model_returns_error():
    """测试缺失model字段返回错误"""
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        # 应该返回错误（没有渠道支持空模型）
        assert response.status_code in (400, 500)
