import asyncio
import json
import os
import stat
import time

import pytest

import config
import storage


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_file = data_dir / "channels.json"
    api_keys_file = data_dir / "api_keys.json"

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(api_keys_file))

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._MODEL_GROUPS_CACHE = None
    storage._MODEL_GROUPS_CACHE_TS = 0
    storage._MODEL_GROUPS_CACHE_VERSION = 0
    storage._model_groups_lock = None
    storage._channels_lock = None
    storage._keys_lock = None

    yield

    storage._cache = None
    storage._cache_ts = 0
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._MODEL_GROUPS_CACHE = None
    storage._MODEL_GROUPS_CACHE_TS = 0
    storage._MODEL_GROUPS_CACHE_VERSION = 0
    storage._model_groups_lock = None
    storage._channels_lock = None
    storage._keys_lock = None


class TestLoadData:
    @pytest.mark.anyio
    async def test_creates_default_file_when_missing(self):
        assert not os.path.exists(config.CHANNELS_FILE)
        data = await storage.load_data()
        assert os.path.exists(config.CHANNELS_FILE)
        assert data == {"channels": []}

    @pytest.mark.anyio
    async def test_reads_existing_file(self):
        payload = {"channels": [{"id": "ch_1", "name": "test"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        storage._cache = None
        storage._cache_ts = 0
        data = await storage.load_data()
        assert data == payload

    @pytest.mark.anyio
    async def test_uses_cache_within_ttl(self):
        payload = {"channels": [{"id": "ch_1", "name": "first"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = await storage.load_data()
        assert data1["channels"][0]["name"] == "first"

        payload["channels"][0]["name"] = "second"
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data2 = await storage.load_data()
        assert data2["channels"][0]["name"] == "second"

    @pytest.mark.anyio
    async def test_cache_expires_after_ttl(self):
        payload = {"channels": [{"id": "ch_1", "name": "first"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = await storage.load_data()
        assert data1["channels"][0]["name"] == "first"

        payload["channels"][0]["name"] = "second"
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        storage._cache_ts = time.time() - 10
        data2 = await storage.load_data()
        assert data2["channels"][0]["name"] == "second"

    @pytest.mark.anyio
    async def test_malformed_channels_json_returns_empty_skeleton(self):
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        data = await storage.load_data()

        assert data == {"channels": []}


class TestSaveData:
    @pytest.mark.anyio
    async def test_writes_data_to_disk(self):
        payload = {"channels": [{"id": "ch_2", "name": "saved"}]}
        await storage.save_data(payload)

        with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk == payload

    @pytest.mark.anyio
    async def test_channels_file_is_not_world_readable(self):
        payload = {"channels": [{"id": "ch_2", "name": "saved"}]}
        await storage.save_data(payload)

        mode = stat.S_IMODE(os.stat(config.CHANNELS_FILE).st_mode)
        assert mode == 0o600

    @pytest.mark.anyio
    async def test_updates_cache_after_save(self):
        payload = {"channels": [{"id": "ch_3", "name": "cached"}]}
        await storage.save_data(payload)

        os.remove(config.CHANNELS_FILE)
        data = await storage.load_data()
        assert data == {"channels": []}

    @pytest.mark.anyio
    async def test_atomic_write(self):
        payload = {"channels": [{"id": "ch_4", "name": "atomic"}]}
        await storage.save_data(payload)

        assert os.path.exists(config.CHANNELS_FILE)
        dir_files = os.listdir(os.path.dirname(config.CHANNELS_FILE))
        tmp_files = [f for f in dir_files if f.startswith(".channels_")]
        assert len(tmp_files) == 0, f"残留的临时文件: {tmp_files}"

    @pytest.mark.anyio
    async def test_invalid_json_does_not_corrupt_existing_file(self):
        valid = {"channels": [{"id": "ch_5", "name": "safe"}]}
        await storage.save_data(valid)

        import unittest.mock

        with unittest.mock.patch("json.dump", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                await storage.save_data({"channels": []})

        with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk == valid

    @pytest.mark.anyio
    async def test_save_invalidates_model_groups_cache_without_running_loop_task(self):
        cached_group = storage.ModelGroup(name="old", models=["gpt-old"])
        storage._MODEL_GROUPS_CACHE = [cached_group]
        storage._MODEL_GROUPS_CACHE_TS = time.time()

        await storage.save_data({"channels": [], "model_groups": []})

        assert storage._MODEL_GROUPS_CACHE is None
        assert storage._MODEL_GROUPS_CACHE_TS == 0


class TestAtomicUpdateData:
    @pytest.mark.anyio
    async def test_atomic_update_data_reads_latest_disk_state_and_updates_cache(self):
        await storage.save_data({"channels": [{"id": "ch_existing"}]})

        def mutator(data):
            data["channels"].append({"id": "ch_new"})

        await storage.atomic_update_data(mutator)

        with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert [ch["id"] for ch in on_disk["channels"]] == ["ch_existing", "ch_new"]

        assert [ch["id"] for ch in storage._cache["channels"]] == ["ch_existing", "ch_new"]

    @pytest.mark.anyio
    async def test_atomic_update_data_serializes_concurrent_mutators(self):
        async def add_channel(index: int):
            await storage.atomic_update_data(
                lambda data: data.setdefault("channels", []).append({"id": f"ch_{index}"})
            )

        await asyncio.gather(*(add_channel(i) for i in range(20)))

        with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert sorted(
            (ch["id"] for ch in on_disk["channels"]),
            key=lambda item: int(item.split("_")[1]),
        ) == [
            f"ch_{i}" for i in range(20)
        ]


class TestAtomicUpdateApiKeys:
    @pytest.mark.anyio
    async def test_atomic_update_api_keys_reads_latest_disk_state_and_updates_cache(self):
        await storage.save_api_keys({"api_keys": [{"id": "key_existing"}]})

        def mutator(data):
            data["api_keys"].append({"id": "key_new"})

        await storage.atomic_update_api_keys(mutator)

        with open(config.API_KEYS_FILE, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert [key["id"] for key in on_disk["api_keys"]] == ["key_existing", "key_new"]

        assert [key["id"] for key in storage._keys_cache["api_keys"]] == ["key_existing", "key_new"]


class TestInvalidateCache:
    @pytest.mark.anyio
    async def test_forces_next_load_from_disk(self):
        payload = {"channels": [{"id": "ch_6", "name": "original"}]}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = await storage.load_data()
        assert data1["channels"][0]["name"] == "original"

        payload["channels"][0]["name"] = "modified"
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        await storage.invalidate_cache()
        data2 = await storage.load_data()
        assert data2["channels"][0]["name"] == "modified"


class TestApiKeysStorage:
    @pytest.mark.anyio
    async def test_load_api_keys_creates_default_when_missing(self):
        assert not os.path.exists(config.API_KEYS_FILE)
        data = await storage.load_api_keys()
        assert os.path.exists(config.API_KEYS_FILE)
        assert data == {"api_keys": []}

    @pytest.mark.anyio
    async def test_save_and_load_api_keys(self):
        payload = {"api_keys": [{"id": "key_1", "name": "test-key"}]}
        await storage.save_api_keys(payload)
        storage._keys_cache = None
        storage._keys_cache_ts = 0
        data = await storage.load_api_keys()
        assert data == payload

    @pytest.mark.anyio
    async def test_api_keys_file_is_not_world_readable(self):
        payload = {"api_keys": [{"id": "key_1", "name": "test-key"}]}
        await storage.save_api_keys(payload)

        mode = stat.S_IMODE(os.stat(config.API_KEYS_FILE).st_mode)
        assert mode == 0o600

    @pytest.mark.anyio
    async def test_keys_cache_uses_ttl(self):
        payload = {"api_keys": [{"id": "key_1", "name": "first"}]}
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data1 = await storage.load_api_keys()
        assert data1["api_keys"][0]["name"] == "first"

        payload["api_keys"][0]["name"] = "second"
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        data2 = await storage.load_api_keys()
        assert data2["api_keys"][0]["name"] == "second"

    @pytest.mark.anyio
    async def test_invalidate_keys_cache(self):
        payload = {"api_keys": [{"id": "key_1", "name": "original"}]}
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        await storage.load_api_keys()
        payload["api_keys"][0]["name"] = "modified"
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        await storage.invalidate_keys_cache()
        data = await storage.load_api_keys()
        assert data["api_keys"][0]["name"] == "modified"

    @pytest.mark.anyio
    async def test_malformed_api_keys_json_returns_empty_skeleton(self):
        with open(config.API_KEYS_FILE, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        data = await storage.load_api_keys()

        assert data == {"api_keys": []}


class TestModelGroupsStorage:
    @pytest.mark.anyio
    async def test_skips_invalid_model_group_entries(self):
        payload = {
            "channels": [],
            "model_groups": [
                {"id": "broken", "enabled": True},
                {"id": "grp_valid", "name": "valid", "models": ["gpt-4"], "enabled": True},
            ],
        }
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        groups = await storage.load_model_groups()

        assert [g.id for g in groups] == ["grp_valid"]

    @pytest.mark.anyio
    async def test_load_model_groups_does_not_overwrite_invalidation_with_stale_data(self, monkeypatch):
        stale_data = {
            "channels": [],
            "model_groups": [{"id": "grp_old", "name": "old", "models": ["gpt-old"], "enabled": True}],
        }
        fresh_data = {
            "channels": [],
            "model_groups": [{"id": "grp_new", "name": "new", "models": ["gpt-new"], "enabled": True}],
        }
        release_load = asyncio.Event()

        async def slow_load_data():
            await release_load.wait()
            return stale_data

        monkeypatch.setattr(storage, "load_data", slow_load_data)

        first_load = asyncio.create_task(storage.load_model_groups())
        await asyncio.sleep(0)
        invalidation = asyncio.create_task(storage.invalidate_model_groups_cache())

        release_load.set()
        await first_load
        await invalidation

        async def fresh_load_data():
            return fresh_data

        monkeypatch.setattr(storage, "load_data", fresh_load_data)
        groups = await storage.load_model_groups()

        assert [g.id for g in groups] == ["grp_new"]

    @pytest.mark.anyio
    async def test_load_model_groups_retries_when_sync_invalidation_happens_during_load(self, monkeypatch):
        stale_data = {
            "channels": [],
            "model_groups": [{"id": "grp_old", "name": "old", "models": ["gpt-old"], "enabled": True}],
        }
        fresh_data = {
            "channels": [],
            "model_groups": [{"id": "grp_new", "name": "new", "models": ["gpt-new"], "enabled": True}],
        }

        async def load_data():
            if load_data.calls == 0:
                load_data.calls += 1
                storage._invalidate_model_groups_cache_sync()
                return stale_data
            load_data.calls += 1
            return fresh_data

        load_data.calls = 0
        monkeypatch.setattr(storage, "load_data", load_data)

        groups = await storage.load_model_groups()

        assert [g.id for g in groups] == ["grp_new"]
        assert [g.id for g in storage._MODEL_GROUPS_CACHE] == ["grp_new"]

    @pytest.mark.anyio
    async def test_load_model_groups_loops_until_version_stable(self, monkeypatch):
        """If sync invalidation happens during multiple consecutive load_data()
        calls, load_model_groups must keep retrying until the version is stable
        and not cache stale data."""
        stale_data = {
            "channels": [],
            "model_groups": [{"id": "grp_old", "name": "old", "models": ["gpt-old"], "enabled": True}],
        }
        fresh_data = {
            "channels": [],
            "model_groups": [{"id": "grp_new", "name": "new", "models": ["gpt-new"], "enabled": True}],
        }
        newest_data = {
            "channels": [],
            "model_groups": [{"id": "grp_newest", "name": "newest", "models": ["gpt-newest"], "enabled": True}],
        }

        async def load_data():
            load_data.calls += 1
            if load_data.calls == 1:
                storage._invalidate_model_groups_cache_sync()
                return stale_data
            if load_data.calls == 2:
                storage._invalidate_model_groups_cache_sync()
                return fresh_data
            return newest_data

        load_data.calls = 0
        monkeypatch.setattr(storage, "load_data", load_data)

        groups = await storage.load_model_groups()

        assert [g.id for g in groups] == ["grp_newest"]
        assert [g.id for g in storage._MODEL_GROUPS_CACHE] == ["grp_newest"]

    @pytest.mark.anyio
    async def test_save_data_during_load_model_groups_does_not_leave_stale_cache(self, monkeypatch):
        """When save_data triggers _invalidate_model_groups_cache_sync while
        load_model_groups is suspended at load_data(), the stale result must
        not overwrite the invalidated cache."""
        stale_data = {
            "channels": [],
            "model_groups": [{"id": "grp_old", "name": "old", "models": ["gpt-old"], "enabled": True}],
        }
        fresh_data = {
            "channels": [],
            "model_groups": [{"id": "grp_new", "name": "new", "models": ["gpt-new"], "enabled": True}],
        }

        load_release = asyncio.Event()
        load_call_count = 0

        async def controlled_load_data():
            nonlocal load_call_count
            load_call_count += 1
            if load_call_count == 1:
                await load_release.wait()
                return stale_data
            return fresh_data

        monkeypatch.setattr(storage, "load_data", controlled_load_data)

        load_task = asyncio.create_task(storage.load_model_groups())
        await asyncio.sleep(0)

        storage._invalidate_model_groups_cache_sync()

        load_release.set()
        result = await load_task

        assert storage._MODEL_GROUPS_CACHE is not None
        assert [g.id for g in result] == ["grp_new"]
        assert [g.id for g in storage._MODEL_GROUPS_CACHE] == ["grp_new"]

    @pytest.mark.anyio
    async def test_sync_invalidation_clears_cache_and_increments_version(self):
        storage._MODEL_GROUPS_CACHE = [storage.ModelGroup(name="cached", models=["gpt-4"])]
        storage._MODEL_GROUPS_CACHE_TS = time.time()
        storage._MODEL_GROUPS_CACHE_VERSION = 3

        storage._invalidate_model_groups_cache_sync()

        assert storage._MODEL_GROUPS_CACHE is None
        assert storage._MODEL_GROUPS_CACHE_TS == 0
        assert storage._MODEL_GROUPS_CACHE_VERSION == 4


class TestConcurrency:
    @pytest.mark.anyio
    async def test_concurrent_load_data_is_safe(self):
        payload = {"channels": list(range(1000))}
        with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        storage._cache = None
        storage._cache_ts = 0

        results = await asyncio.gather(*[storage.load_data() for _ in range(20)])
        assert all(len(r["channels"]) == 1000 for r in results)

    @pytest.mark.anyio
    async def test_concurrent_save_data_is_safe(self):
        errors = []

        async def worker(idx):
            try:
                await storage.save_data({"channels": [{"id": f"ch_{idx}"}]})
            except Exception as e:
                errors.append(e)

        await asyncio.gather(*[worker(i) for i in range(20)])
        assert len(errors) == 0, f"并发保存出错: {errors}"
        data = await storage.load_data()
        assert "channels" in data

    @pytest.mark.anyio
    async def test_concurrent_load_and_save_is_safe(self):
        errors = []

        async def reader():
            try:
                for _ in range(10):
                    await storage.load_data()
            except Exception as e:
                errors.append(e)

        async def writer(idx):
            try:
                for _ in range(10):
                    await storage.save_data({"channels": [{"id": f"ch_{idx}"}]})
            except Exception as e:
                errors.append(e)

        await asyncio.gather(
            *[reader() for _ in range(10)],
            *[writer(i) for i in range(10)],
        )
        assert len(errors) == 0, f"并发读写出错: {errors}"


class TestModelGroupsAtomicity:
    """Verify that save_model_groups and the CRUD helpers use atomic_update_data
    to prevent lost-update races with concurrent channel operations."""

    @pytest.mark.anyio
    async def test_concurrent_add_model_group_preserves_all_groups(self):
        await storage.save_data({"channels": [], "model_groups": []})

        async def add(idx: int):
            group = storage.ModelGroup(name=f"grp_{idx}", models=[f"model_{idx}"])
            return await storage.add_model_group(group)

        await asyncio.gather(*(add(i) for i in range(10)))

        groups = await storage.load_model_groups()
        names = sorted(g.name for g in groups)
        assert names == [f"grp_{i}" for i in range(10)]

    @pytest.mark.anyio
    async def test_concurrent_add_model_group_and_channel_update_preserves_both(self):
        await storage.save_data(
            {"channels": [{"id": "ch_existing"}], "model_groups": []}
        )

        async def add_group(idx: int):
            group = storage.ModelGroup(name=f"grp_{idx}", models=[f"model_{idx}"])
            await storage.add_model_group(group)

        async def add_channel(idx: int):
            await storage.atomic_update_data(
                lambda data: data.setdefault("channels", []).append(
                    {"id": f"ch_new_{idx}"}
                )
            )

        await asyncio.gather(
            *(add_group(i) for i in range(5)),
            *(add_channel(i) for i in range(5)),
        )

        data = await storage.load_data()
        channel_ids = sorted(ch["id"] for ch in data["channels"])
        assert "ch_existing" in channel_ids
        assert all(f"ch_new_{i}" in channel_ids for i in range(5))

        groups = await storage.load_model_groups()
        assert sorted(g.name for g in groups) == [f"grp_{i}" for i in range(5)]

    @pytest.mark.anyio
    async def test_save_model_groups_preserves_concurrent_channel_changes(self):
        """save_model_groups must not overwrite channels modified concurrently."""
        await storage.save_data(
            {"channels": [{"id": "ch_1"}], "model_groups": []}
        )

        barrier = asyncio.Barrier(2)

        async def update_channels():
            await barrier.wait()
            await storage.atomic_update_data(
                lambda data: data["channels"].append({"id": "ch_2"})
            )

        async def save_groups():
            await barrier.wait()
            groups = [storage.ModelGroup(name="new_grp", models=["gpt-4"])]
            await storage.save_model_groups(groups)

        await asyncio.gather(update_channels(), save_groups())

        data = await storage.load_data()
        channel_ids = [ch["id"] for ch in data["channels"]]
        assert "ch_1" in channel_ids
        assert "ch_2" in channel_ids

        groups = await storage.load_model_groups()
        assert len(groups) == 1
        assert groups[0].name == "new_grp"
