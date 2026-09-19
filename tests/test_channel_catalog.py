import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.channel import Channel, Endpoint
from models.model_group import ModelGroup


class RecordingEffects:
    def __init__(self) -> None:
        self.channel_changes = []
        self.model_group_changes = []

    async def apply_channel_change(self, change, snapshot) -> None:
        self.channel_changes.append((change, snapshot))

    async def apply_model_group_change(self, change, snapshot) -> None:
        self.model_group_changes.append((change, snapshot))


def _channel(channel_id: str = "ch-1") -> Channel:
    return Channel(
        id=channel_id,
        name=channel_id,
        api_key="secret",
        models=["gpt-test"],
        endpoints=[Endpoint(api_type="openai-chat-completions", base_url="https://example.test/v1")],
    )


@pytest.mark.anyio
async def test_missing_catalog_is_created_with_empty_collections(tmp_path):
    from channel_catalog import ChannelCatalog

    path = tmp_path / "channels.json"
    catalog = ChannelCatalog(path=lambda: str(path), effects=RecordingEffects())

    snapshot = await catalog.snapshot()

    assert snapshot.channels == ()
    assert snapshot.model_groups == ()
    assert path.exists()


@pytest.mark.anyio
async def test_public_models_cannot_mutate_the_authoritative_snapshot(tmp_path):
    from channel_catalog import ChannelCatalog

    path = tmp_path / "channels.json"
    catalog = ChannelCatalog(path=lambda: str(path), effects=RecordingEffects())
    supplied = _channel()
    returned = await catalog.add_channel(supplied)
    supplied.name = "mutated input"
    returned.models.append("mutated return")
    public_snapshot = await catalog.snapshot()
    public_snapshot.channels[0].name = "mutated snapshot"

    authoritative = await catalog.snapshot()
    assert authoritative.channels[0].name == "ch-1"
    assert authoritative.channels[0].models == ["gpt-test"]
    assert json.loads(path.read_text(encoding="utf-8"))["channels"][0]["name"] == "ch-1"


@pytest.mark.anyio
async def test_corrupt_catalog_is_preserved_and_rejected(tmp_path):
    from channel_catalog import CatalogCorruptionError, ChannelCatalog

    path = tmp_path / "channels.json"
    path.write_text('{"channels": [', encoding="utf-8")
    catalog = ChannelCatalog(path=lambda: str(path), effects=RecordingEffects())

    with pytest.raises(CatalogCorruptionError):
        await catalog.snapshot()

    assert path.read_text(encoding="utf-8") == '{"channels": ['
    backups = list(tmp_path.glob("channels.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"channels": ['


@pytest.mark.anyio
async def test_mutation_preserves_unparseable_entries_for_manual_recovery(tmp_path):
    from channel_catalog import ChannelCatalog

    path = tmp_path / "channels.json"
    broken = {"id": "broken", "name": "missing endpoints"}
    path.write_text(json.dumps({"channels": [broken]}), encoding="utf-8")
    catalog = ChannelCatalog(path=lambda: str(path), effects=RecordingEffects())

    await catalog.add_model_group(ModelGroup(name="fallback", models=["gpt-test"]))

    assert json.loads(path.read_text(encoding="utf-8"))["channels"] == [broken]


@pytest.mark.anyio
async def test_model_group_change_does_not_emit_channel_change(tmp_path):
    from channel_catalog import ChangeKind, ChannelCatalog

    effects = RecordingEffects()
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=effects)

    group = ModelGroup(name="fallback", models=["gpt-test"])
    await catalog.add_model_group(group)

    assert effects.channel_changes == []
    assert len(effects.model_group_changes) == 1
    change, snapshot = effects.model_group_changes[0]
    assert change.kind is ChangeKind.created
    assert change.model_group_id == group.id
    assert snapshot.model_groups == (group,)


@pytest.mark.anyio
async def test_model_group_toggle_emits_a_toggled_fact(tmp_path):
    from channel_catalog import ChangeKind, ChannelCatalog

    effects = RecordingEffects()
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=effects)
    group = ModelGroup(name="fallback", models=["gpt-test"])
    await catalog.add_model_group(group)
    effects.model_group_changes.clear()

    updated = await catalog.toggle_model_group(group.id)

    assert updated is not None and updated.enabled is False
    change, _snapshot = effects.model_group_changes[0]
    assert change.kind is ChangeKind.toggled
    assert change.before == group
    assert change.after == updated


@pytest.mark.anyio
async def test_channel_update_emits_only_the_changed_channel(tmp_path):
    from channel_catalog import ChangeKind, ChannelCatalog

    effects = RecordingEffects()
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=effects)
    original = _channel()
    await catalog.add_channel(original)
    effects.channel_changes.clear()

    updated = await catalog.update_channel(original.id, {"name": "renamed"})

    assert updated is not None
    assert updated.name == "renamed"
    assert len(effects.channel_changes) == 1
    change, snapshot = effects.channel_changes[0]
    assert change.kind is ChangeKind.updated
    assert change.channel_id == original.id
    assert change.before == original
    assert change.after == updated
    assert effects.model_group_changes == []
    assert snapshot.channels == (updated,)


@pytest.mark.anyio
async def test_atomic_replace_retries_transient_windows_sharing_violation(tmp_path, monkeypatch):
    import atomic_json
    from channel_catalog import ChannelCatalog

    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RecordingEffects())
    await catalog.snapshot()
    real_replace = atomic_json.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(atomic_json.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_json.time, "sleep", lambda _: None)
    await catalog.add_channel(_channel())

    assert attempts == 3
    assert [channel.id for channel in (await catalog.snapshot()).channels] == ["ch-1"]


@pytest.mark.anyio
async def test_atomic_replace_surfaces_persistent_windows_sharing_violation(tmp_path, monkeypatch):
    import atomic_json
    from channel_catalog import ChannelCatalog

    attempts = 0

    def locked_replace(source, destination):
        nonlocal attempts
        attempts += 1
        error = PermissionError("sharing violation")
        error.winerror = 5
        raise error

    monkeypatch.setattr(atomic_json.os, "replace", locked_replace)
    monkeypatch.setattr(atomic_json.time, "sleep", lambda _: None)
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RecordingEffects())

    with pytest.raises(PermissionError, match="sharing violation"):
        await catalog.add_channel(_channel())

    assert attempts == atomic_json.WINDOWS_REPLACE_ATTEMPTS


@pytest.mark.anyio
async def test_concurrent_snapshot_and_channel_changes_are_serialized(tmp_path):
    from channel_catalog import ChannelCatalog

    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RecordingEffects())

    async def reader():
        for _ in range(10):
            await catalog.snapshot()

    async def writer(index: int):
        await catalog.add_channel(_channel(f"ch-{index}"))

    await asyncio.gather(*[reader() for _ in range(10)], *[writer(index) for index in range(10)])

    assert {channel.id for channel in (await catalog.snapshot()).channels} == {f"ch-{index}" for index in range(10)}


@pytest.mark.anyio
async def test_concurrent_channel_and_model_group_changes_preserve_both_domains(tmp_path):
    from channel_catalog import ChannelCatalog

    path = tmp_path / "channels.json"
    catalog = ChannelCatalog(path=lambda: str(path), effects=RecordingEffects())

    await asyncio.gather(
        catalog.add_channel(_channel()),
        catalog.add_model_group(ModelGroup(name="fallback", models=["gpt-test"])),
    )

    snapshot = await catalog.snapshot()
    assert [channel.id for channel in snapshot.channels] == ["ch-1"]
    assert [group.name for group in snapshot.model_groups] == ["fallback"]
    assert set(json.loads(path.read_text(encoding="utf-8"))) >= {"channels", "model_groups"}


@pytest.mark.anyio
async def test_concurrent_duplicate_model_group_names_cannot_commit(tmp_path):
    from channel_catalog import CatalogConflictError, ChannelCatalog

    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RecordingEffects())
    results = await asyncio.gather(
        catalog.add_model_group(ModelGroup(name="same", models=["a"])),
        catalog.add_model_group(ModelGroup(name="same", models=["b"])),
        return_exceptions=True,
    )

    assert sum(isinstance(result, ModelGroup) for result in results) == 1
    assert sum(isinstance(result, CatalogConflictError) for result in results) == 1
    assert len((await catalog.snapshot()).model_groups) == 1


@pytest.mark.anyio
async def test_catalog_rejects_identity_changes(tmp_path):
    from channel_catalog import CatalogConflictError, ChannelCatalog

    effects = RecordingEffects()
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=effects)
    channel = await catalog.add_channel(_channel())
    group = await catalog.add_model_group(ModelGroup(name="fallback", models=["gpt-test"]))
    effects.channel_changes.clear()
    effects.model_group_changes.clear()

    with pytest.raises(CatalogConflictError, match="渠道 ID 不可修改"):
        await catalog.update_channel(channel.id, {"id": "renamed-channel"})
    with pytest.raises(CatalogConflictError, match="模型组 ID 不可修改"):
        await catalog.update_model_group(group.id, {"id": "renamed-group"})

    snapshot = await catalog.snapshot()
    assert [item.id for item in snapshot.channels] == [channel.id]
    assert [item.id for item in snapshot.model_groups] == [group.id]
    assert effects.channel_changes == []
    assert effects.model_group_changes == []


@pytest.mark.anyio
async def test_runtime_update_clears_only_changed_channel_permanent_state(tmp_path, monkeypatch):
    import client
    from channel_catalog import ChannelCatalog, RuntimeCatalogEffects
    from proxy import outcomes

    remove_client = AsyncMock()
    monkeypatch.setattr(client, "remove_channel_client", remove_client)
    outcomes.reset()
    outcomes.record("model", "ch-1", outcomes.OutcomeKind.http_4xx_config)
    outcomes.record("model", "ch-2", outcomes.OutcomeKind.http_4xx_config)
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RuntimeCatalogEffects())
    first = _channel("ch-1")
    await catalog.add_channel(first)
    await catalog.add_channel(_channel("ch-2"))

    await catalog.update_channel("ch-1", {"name": "renamed"})

    remove_client.assert_awaited_once_with(first)
    assert [(target.model, target.channel_id) for target in outcomes.probe_targets()] == [("model", "ch-1")]
    outcomes.reset()


@pytest.mark.anyio
async def test_runtime_model_group_change_leaves_channel_state_untouched(tmp_path, monkeypatch):
    import client
    from channel_catalog import ChannelCatalog, RuntimeCatalogEffects
    from proxy import outcomes

    remove_client = AsyncMock()
    monkeypatch.setattr(client, "remove_channel_client", remove_client)
    outcomes.reset()
    outcomes.record("model", "ch-1", outcomes.OutcomeKind.http_4xx_config)
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RuntimeCatalogEffects())

    await catalog.add_model_group(ModelGroup(name="fallback", models=["model"]))

    remove_client.assert_not_awaited()
    assert outcomes.probe_targets() == []
    outcomes.reset()


@pytest.mark.anyio
async def test_runtime_delete_cleans_all_channel_scoped_stores(tmp_path, monkeypatch):
    import client
    import quota_limits
    from balancer.load_balancer import load_balancer
    from channel_catalog import ChannelCatalog, RuntimeCatalogEffects
    from rate_limiter import rate_limiter

    remove_client = AsyncMock()
    cleanup_balancer = AsyncMock()
    cleanup_rate_limiter = MagicMock()
    cleanup_quota = MagicMock()
    monkeypatch.setattr(client, "remove_channel_client", remove_client)
    monkeypatch.setattr(load_balancer, "remove_channel", cleanup_balancer)
    monkeypatch.setattr(rate_limiter, "remove_key", cleanup_rate_limiter)
    monkeypatch.setattr(quota_limits, "unblock", cleanup_quota)
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RuntimeCatalogEffects())
    removed = _channel("ch-1")
    await catalog.add_channel(removed)
    await catalog.add_channel(_channel("ch-2"))

    await catalog.delete_channel("ch-1")

    remove_client.assert_awaited_once_with(removed)
    cleanup_balancer.assert_awaited_once_with("ch-1")
    cleanup_rate_limiter.assert_called_once_with("ch-1")
    cleanup_quota.assert_called_once_with("ch-1")


@pytest.mark.anyio
async def test_runtime_delete_preserves_unrelated_orphan_state(tmp_path, monkeypatch):
    from collections import deque

    import client
    import quota_limits
    from balancer.load_balancer import load_balancer
    from channel_catalog import ChannelCatalog, RuntimeCatalogEffects
    from proxy import outcomes
    from rate_limiter import rate_limiter

    monkeypatch.setattr(client, "remove_channel_client", AsyncMock())
    monkeypatch.setattr(quota_limits, "unblock", MagicMock())
    outcomes.reset()
    load_balancer._current_weights.clear()
    rate_limiter._windows.clear()
    outcomes.record("model", "ch-1", outcomes.OutcomeKind.http_5xx)
    outcomes.record("model", "orphan", outcomes.OutcomeKind.http_5xx)
    outcomes.remember_preferred("group-1", "model", "ch-1")
    outcomes.remember_preferred("group-2", "model", "orphan")
    outcomes.remember_session_sticky("session-1", "ch-1")
    outcomes.remember_session_sticky("session-2", "orphan")
    load_balancer._current_weights[("model", "ch-1")] = 1
    load_balancer._current_weights[("model", "orphan")] = 2
    rate_limiter._windows["ch-1"] = deque([1.0])
    rate_limiter._windows["orphan"] = deque([2.0])
    catalog = ChannelCatalog(path=lambda: str(tmp_path / "channels.json"), effects=RuntimeCatalogEffects())
    await catalog.add_channel(_channel("ch-1"))

    await catalog.delete_channel("ch-1")

    assert outcomes.is_degraded("model", "ch-1") is False
    assert outcomes.is_degraded("model", "orphan") is True
    assert outcomes.sticky_preferred("group-1", "model") is None
    assert outcomes.sticky_preferred("group-2", "model") == "orphan"
    assert outcomes.session_sticky_get("session-1") is None
    assert outcomes.session_sticky_get("session-2") == "orphan"
    assert ("model", "ch-1") not in load_balancer._current_weights
    assert load_balancer._current_weights[("model", "orphan")] == 2
    assert "ch-1" not in rate_limiter._windows
    assert "orphan" in rate_limiter._windows
    outcomes.reset()
    load_balancer._current_weights.clear()
    rate_limiter._windows.clear()
