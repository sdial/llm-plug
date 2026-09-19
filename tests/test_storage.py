import json
import os
import stat
from pathlib import Path

import pytest

import config
import storage


@pytest.fixture(autouse=True)
def isolated_api_keys(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(data_dir / "api_keys.json"))
    storage._keys_lock = None
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_cache_file_sig = None


@pytest.mark.anyio
async def test_load_api_keys_creates_default_when_missing():
    assert not os.path.exists(config.API_KEYS_FILE)
    assert await storage.load_api_keys() == {"api_keys": []}
    assert os.path.exists(config.API_KEYS_FILE)


@pytest.mark.anyio
async def test_corrupt_api_keys_file_is_preserved_and_rejected():
    os.makedirs(config.DATA_DIR)
    with open(config.API_KEYS_FILE, "w", encoding="utf-8") as file:
        file.write('{"api_keys": [')

    with pytest.raises(storage.StorageCorruptionError):
        await storage.load_api_keys()

    with open(config.API_KEYS_FILE, encoding="utf-8") as file:
        assert file.read() == '{"api_keys": ['
    assert len(list(Path(config.DATA_DIR).glob("api_keys.json.corrupt-*"))) == 1


@pytest.mark.anyio
async def test_atomic_update_api_keys_refuses_to_overwrite_corrupt_file():
    os.makedirs(config.DATA_DIR)
    with open(config.API_KEYS_FILE, "w", encoding="utf-8") as file:
        file.write('{"api_keys": [')

    with pytest.raises(storage.StorageCorruptionError):
        await storage.atomic_update_api_keys(lambda data: data["api_keys"].append({"id": "new"}))

    with open(config.API_KEYS_FILE, encoding="utf-8") as file:
        assert file.read() == '{"api_keys": ['


@pytest.mark.anyio
async def test_atomic_update_api_keys_reads_latest_disk_state():
    await storage.save_api_keys({"api_keys": [{"id": "existing"}]})
    with open(config.API_KEYS_FILE, "w", encoding="utf-8") as file:
        json.dump({"api_keys": [{"id": "external"}]}, file)

    await storage.atomic_update_api_keys(lambda data: data["api_keys"].append({"id": "new"}))

    assert [item["id"] for item in (await storage.load_api_keys())["api_keys"]] == ["external", "new"]


@pytest.mark.anyio
async def test_concurrent_atomic_key_updates_preserve_every_entry():
    import asyncio

    await storage.save_api_keys({"api_keys": []})

    async def add(index: int):
        await storage.atomic_update_api_keys(lambda data: data["api_keys"].append({"id": f"key-{index}"}))

    await asyncio.gather(*(add(index) for index in range(20)))
    assert {item["id"] for item in (await storage.load_api_keys())["api_keys"]} == {f"key-{index}" for index in range(20)}


@pytest.mark.anyio
async def test_invalidate_keys_cache_forces_reload():
    await storage.save_api_keys({"api_keys": [{"id": "before"}]})
    with open(config.API_KEYS_FILE, "w", encoding="utf-8") as file:
        json.dump({"api_keys": [{"id": "after"}]}, file)

    await storage.invalidate_keys_cache()

    assert (await storage.load_api_keys())["api_keys"][0]["id"] == "after"


@pytest.mark.anyio
async def test_api_keys_file_is_not_world_readable():
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not reliable on Windows")
    await storage.save_api_keys({"api_keys": [{"id": "key-1"}]})
    assert stat.S_IMODE(os.stat(config.API_KEYS_FILE).st_mode) == 0o600
