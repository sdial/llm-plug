"""票据05：管理端渠道嵌套契约收紧与接入点持久化语义。

锁定接缝：
- POST/PUT 收到扁平 body → 422 且错误信息点名「扁平」与废弃键（旧契约显式拒绝）
- 嵌套 body 正常创建；PUT 含 endpoints 即整组替换、不含则保持旧接入点不动
- 渠道内 api_type 重复 → 模型层不变式经 API 冒泡为 422
- 停用某接入点落盘后，调度层 resolve_endpoint_attempts 对该格式不再有原生匹配
"""

import json

import httpx
import pytest

import config
import storage
from channel_catalog import catalog
from main import app
from tests.admin_auth_utils import login_admin


@pytest.fixture
def channels_file(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    channels_path = data_dir / "channels.json"
    keys_path = data_dir / "api_keys.json"
    settings_path = data_dir / "settings.json"

    channels_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "id": "ch_test",
                        "name": "Test",
                        "api_key": "sk-test",
                        "models": ["gpt-4o"],
                        "enabled": True,
                        "weight": 1,
                        "priority": 1,
                        "socks5_proxy": None,
                        "endpoints": [
                            {"api_type": "anthropic", "base_url": "https://a.example.com"},
                            {
                                "api_type": "openai-chat-completions",
                                "base_url": "https://b.example.com",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    keys_path.write_text(json.dumps({"api_keys": []}), encoding="utf-8")
    settings_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "CHANNELS_FILE", str(channels_path))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(keys_path))
    monkeypatch.setattr(config, "_SETTINGS_FILE", str(settings_path))
    config._init_settings_sync()
    import middleware.whitelist_middleware as wmod
    import whitelist as _whitelist_mod

    monkeypatch.setattr(
        wmod,
        "_whitelist_cache",
        _whitelist_mod.WhitelistCache(str(data_dir / "whitelist.csv")),
    )
    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None
    yield channels_path
    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None


async def _client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await login_admin(client)
        yield client


# ─── 扁平契约显式拒绝 ───


@pytest.mark.anyio
async def test_post_rejects_flat_body(channels_file):
    flat_body = {
        "name": "Flat",
        "api_type": "openai-chat-completions",
        "base_url": "https://flat.example.com",
        "api_key": "sk-flat",
        "models": ["gpt-4o"],
    }

    async for client in _client():
        resp = await client.post("/admin/channels", json=flat_body)

    assert resp.status_code == 422
    assert "扁平" in resp.text
    assert "api_type" in resp.text  # 点名废弃键


@pytest.mark.anyio
async def test_put_rejects_flat_body(channels_file):
    async for client in _client():
        resp = await client.put("/admin/channels/ch_test", json={"base_url": "https://new.example.com"})

    assert resp.status_code == 422
    assert "扁平" in resp.text


# ─── 嵌套创建与 PUT 替换语义 ───


@pytest.mark.anyio
async def test_post_creates_channel_from_nested_body(channels_file):
    nested_body = {
        "name": "Nested",
        "api_key": "sk-nested",
        "models": ["claude-3"],
        "endpoints": [{"api_type": "anthropic", "base_url": "https://nested.example.com"}],
    }

    async for client in _client():
        resp = await client.post("/admin/channels", json=nested_body)

    assert resp.status_code == 200
    created = resp.json()
    assert created["endpoints"][0]["base_url"] == "https://nested.example.com"

    channels = (await catalog.snapshot()).channels
    assert len(channels) == 2
    stored = next(ch for ch in channels if ch.name == "Nested")
    assert stored.endpoints[0].api_type == "anthropic"


@pytest.mark.anyio
async def test_put_saves_channel_model_override_without_touching_endpoints(channels_file):
    override = {"gpt-4o": {"capabilities": {"input_modalities": {"image": "supported"}}}}

    async for client in _client():
        resp = await client.put("/admin/channels/ch_test", json={"model_overrides": override})

    assert resp.status_code == 200
    response_override = resp.json()["model_overrides"]["gpt-4o"]
    assert response_override["capabilities"]["input_modalities"] == {"image": "supported"}
    stored = next(ch for ch in (await catalog.snapshot()).channels if ch.id == "ch_test")
    assert stored.model_overrides["gpt-4o"].capabilities.input_modalities["image"].value == "supported"
    assert len(stored.endpoints) == 2


@pytest.mark.anyio
async def test_post_rejects_channel_level_input_modalities(channels_file):
    body = {
        "name": "Wrong scope",
        "api_key": "sk-wrong",
        "profile_overrides": {"capabilities": {"input_modalities": {"image": "supported"}}},
        "endpoints": [{"api_type": "openai-chat-completions", "base_url": "https://example.com"}],
    }

    async for client in _client():
        resp = await client.post("/admin/channels", json=body)

    assert resp.status_code == 422
    assert "渠道不能覆盖" in resp.text


@pytest.mark.anyio
async def test_put_with_endpoints_replaces_whole_group(channels_file):
    replacement = {
        "endpoints": [
            {
                "api_type": "openai-chat-completions",
                "base_url": "https://replaced.example.com",
            }
        ]
    }

    async for client in _client():
        resp = await client.put("/admin/channels/ch_test", json=replacement)

    assert resp.status_code == 200
    assert len(resp.json()["endpoints"]) == 1

    stored = next(ch for ch in (await catalog.snapshot()).channels if ch.id == "ch_test")
    assert [ep.api_type for ep in stored.endpoints] == ["openai-chat-completions"]
    assert stored.endpoints[0].base_url == "https://replaced.example.com"


@pytest.mark.anyio
async def test_put_without_endpoints_keeps_existing_endpoints(channels_file):
    async for client in _client():
        resp = await client.put("/admin/channels/ch_test", json={"name": "Renamed"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert [ep["api_type"] for ep in body["endpoints"]] == [
        "anthropic",
        "openai-chat-completions",
    ]


# ─── 序列化终态：响应只含嵌套形态（票据07-B） ───


@pytest.mark.anyio
async def test_list_channels_items_expose_only_nested_shape(channels_file):
    async for client in _client():
        resp = await client.get("/admin/channels")

    assert resp.status_code == 200
    item = next(ch for ch in resp.json() if ch["id"] == "ch_test")
    for flat_key in ("api_type", "base_url", "endpoint_url", "models_url", "anthropic_version"):
        assert flat_key not in item, f"列表项不应再有扁平视图键 {flat_key}"
    assert [ep["api_type"] for ep in item["endpoints"]] == ["anthropic", "openai-chat-completions"]


# ─── api_type 渠道内唯一性 ───


@pytest.mark.anyio
async def test_post_duplicate_api_type_returns_422(channels_file):
    dup_body = {
        "name": "Dup",
        "api_key": "sk-dup",
        "models": [],
        "endpoints": [
            {"api_type": "anthropic", "base_url": "https://a.example.com"},
            {"api_type": "anthropic", "base_url": "https://b.example.com"},
        ],
    }

    async for client in _client():
        resp = await client.post("/admin/channels", json=dup_body)

    assert resp.status_code == 422
    assert "渠道内 api_type 重复" in resp.text


# ─── 接入点停用：落盘 + 调度原生匹配消失 ───


@pytest.mark.anyio
async def test_put_disabling_endpoint_persists_and_drops_native_match(channels_file):
    payload = {
        "endpoints": [
            {"api_type": "anthropic", "base_url": "https://a.example.com"},
            {
                "api_type": "openai-chat-completions",
                "base_url": "https://b.example.com",
                "enabled": False,
            },
        ]
    }

    async for client in _client():
        resp = await client.put("/admin/channels/ch_test", json=payload)

    assert resp.status_code == 200
    assert resp.json()["endpoints"][1]["enabled"] is False

    stored = next(ch for ch in (await catalog.snapshot()).channels if ch.id == "ch_test")
    assert stored.endpoints[1].enabled is False

    from models.api_types import APIType
    from proxy.conversion import resolve_endpoint_attempts

    attempts = resolve_endpoint_attempts(stored, APIType.OPENAI_CHAT)
    # 停用接入点的原生匹配消失；仅剩的启用接入点按转换规则参与解析
    assert [ep.api_type.value for ep in attempts] == ["anthropic"]
