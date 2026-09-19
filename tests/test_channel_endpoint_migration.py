"""渠道两级结构的存量迁移与终态契约（票据01 引入、07-A/07-B 收口）。

存储文件缝：扁平 channels.json → 加载时归一化为"渠道 + 接入点数组"（id 不变），
写盘前备份，二次加载幂等。FLAT_CHANNEL 是全库唯一 legacy 形态 fixture，专测存储迁移路径。
模型缝：嵌套构造终态——Channel 不暴露扁平协议属性、api_type 渠道内唯一、
legacy 归一纯函数的合并覆写规则、持久化字典稳定往返。
"""

import asyncio
import glob
import json

import pytest
from pydantic import ValidationError

import config
from channel_catalog import catalog
from models.channel import Channel, ChannelCreate, Endpoint, normalize_channel_payload

FLAT_CHANNEL = {
    "id": "ch_flat_1",
    "name": "Flat Channel",
    "api_type": "anthropic",
    "base_url": "http://example.com",
    "endpoint_url": "http://example.com/anthropic-override",
    "models_url": None,
    "api_key": "sk-test",
    "models": ["claude-3"],
    "enabled": True,
    "weight": 2,
    "priority": 1,
    "socks5_proxy": None,
    "created_at": "2026-04-28T00:00:00Z",
    "anthropic_version": "2023-06-01",
}


@pytest.fixture
def channels_file(tmp_path):
    """隔离的渠道数据文件；重置存储缓存与锁（仿 conftest e2e 模式）"""
    path = str(tmp_path / "channels.json")
    old_file = config.CHANNELS_FILE
    config.CHANNELS_FILE = path
    catalog.reset()
    yield path
    config.CHANNELS_FILE = old_file
    catalog.reset()


def _seed(channels_file, payload):
    with open(channels_file, "w", encoding="utf-8") as f:
        json.dump(payload, f)


# ─── 存储迁移 ───


def test_flat_channels_file_migrates_on_load(channels_file):
    _seed(channels_file, {"channels": [dict(FLAT_CHANNEL)]})

    snapshot = asyncio.run(catalog.snapshot())

    ch = snapshot.channels[0]
    assert ch.id == "ch_flat_1"
    assert ch.name == "Flat Channel"
    assert ch.weight == 2
    assert len(ch.endpoints) == 1
    ep = ch.endpoints[0]
    assert ep.api_type == "anthropic"
    assert ep.base_url == "http://example.com"
    assert ep.url_override == "http://example.com/anthropic-override"
    assert ep.anthropic_version == "2023-06-01"
    assert ep.enabled is True


def test_migration_writes_backup_and_strips_flat_keys_from_disk(channels_file):
    _seed(channels_file, {"channels": [dict(FLAT_CHANNEL)]})

    asyncio.run(catalog.snapshot())

    backups = glob.glob(f"{channels_file}.pre-endpoints-*")
    assert len(backups) == 1, "迁移写盘前应生成恰好一份备份"
    with open(backups[0], encoding="utf-8") as f:
        backup_payload = json.load(f)
    assert backup_payload["channels"][0]["endpoint_url"] == (FLAT_CHANNEL["endpoint_url"]), "备份应保留原始扁平内容"

    with open(channels_file, encoding="utf-8") as f:
        migrated = json.load(f)
    disk_ch = migrated["channels"][0]
    assert len(disk_ch["endpoints"]) == 1
    for flat_key in ("api_type", "base_url", "endpoint_url"):
        assert flat_key not in disk_ch, f"落盘结构不应残留扁平键 {flat_key}"


def test_migration_is_idempotent_across_loads(channels_file):
    _seed(channels_file, {"channels": [dict(FLAT_CHANNEL)]})
    asyncio.run(catalog.snapshot())
    backups_after_first = glob.glob(f"{channels_file}.pre-endpoints-*")

    # 模拟进程重启或热重载后的真实读盘路径；仅命中内存缓存无法验证迁移幂等性。
    catalog.reset()
    snapshot = asyncio.run(catalog.snapshot())

    assert len(glob.glob(f"{channels_file}.pre-endpoints-*")) == len(backups_after_first), "二次加载不得重复备份/二次包裹"
    assert snapshot.channels[0].endpoints[0].api_type == "anthropic"


def test_nested_channels_file_gets_profile_reference_migration(channels_file):
    nested = {
        "channels": [
            {
                "id": "ch_nested_1",
                "name": "Nested",
                "api_key": "sk-test",
                "models": ["m"],
                "created_at": "2026-04-28T00:00:00Z",
                "endpoints": [
                    {
                        "api_type": "anthropic",
                        "base_url": "http://a.example.com",
                        "enabled": True,
                    },
                    {
                        "api_type": "openai-chat-completions",
                        "base_url": "http://b.example.com",
                        "enabled": False,
                    },
                ],
            }
        ]
    }
    _seed(channels_file, nested)

    snapshot = asyncio.run(catalog.snapshot())

    assert len(glob.glob(f"{channels_file}.pre-endpoints-*")) == 1
    eps = snapshot.channels[0].endpoints
    assert [ep.base_url for ep in eps] == ["http://a.example.com", "http://b.example.com"]
    assert eps[1].enabled is False
    with open(channels_file, encoding="utf-8") as file:
        stored = json.load(file)
    assert stored["schema_version"] == 3
    assert stored["channels"][0]["upstream_profile_id"] == "generic"
    assert stored["channels"][0]["catalog_revision"] == "builtin-2"


def test_capability_scope_migration_moves_model_overrides_and_drops_channel_modalities(channels_file):
    nested = {
        "channels": [
            {
                "id": "ch_caps",
                "name": "Capabilities",
                "api_key": "sk-test",
                "models": ["vision-1"],
                "upstream_profile_id": "generic",
                "catalog_revision": "builtin-2",
                "profile_overrides": {
                    "capabilities": {
                        "input_modalities": {"image": "unsupported"},
                        "features": {"reasoning": "supported"},
                    },
                    "filter_think_content": True,
                },
                "endpoints": [
                    {
                        "api_type": "openai-chat-completions",
                        "base_url": "http://example.com",
                        "model_overrides": {"vision-1": {"capabilities": {"input_modalities": {"image": "supported"}}}},
                    }
                ],
            }
        ]
    }
    _seed(channels_file, nested)

    channel = asyncio.run(catalog.snapshot()).channels[0]

    assert channel.profile_overrides.capabilities.input_modalities == {}
    assert channel.profile_overrides.capabilities.state("features", "reasoning").value == "supported"
    assert channel.profile_overrides.filter_think_content is True
    assert channel.model_overrides["vision-1"].capabilities.state("input_modalities", "image").value == "supported"
    with open(channels_file, encoding="utf-8") as file:
        stored = json.load(file)["channels"][0]
    assert "model_overrides" not in stored["endpoints"][0]
    assert stored["profile_overrides"]["capabilities"]["input_modalities"] == {}


# ─── 模型层 ───


def test_duplicate_api_type_within_channel_rejected():
    endpoints = [
        Endpoint(api_type="anthropic", base_url="http://a.example.com"),
        Endpoint(api_type="anthropic", base_url="http://b.example.com"),
    ]
    with pytest.raises(ValidationError):
        Channel(name="dup", api_key="k", endpoints=endpoints)


def test_legacy_flat_keys_override_first_endpoint_in_normalize():
    """legacy 磁盘数据归一规则：已有 endpoints 时扁平键按"键存在即生效"覆写首个接入点，其余不动。

    该分支仅服务于存储迁移（管理端 PUT 已显式拒绝扁平键），经纯函数缝直接锁定。
    """
    old = {
        "name": "multi",
        "api_key": "k",
        "endpoints": [
            {"api_type": "anthropic", "base_url": "http://old-a"},
            {"api_type": "openai-chat-completions", "base_url": "http://old-b"},
        ],
    }

    merged = normalize_channel_payload({**old, "base_url": "http://new-a"})

    assert len(merged["endpoints"]) == 2
    assert merged["endpoints"][0]["base_url"] == "http://new-a"
    assert merged["endpoints"][0]["api_type"] == "anthropic"
    assert merged["endpoints"][1]["base_url"] == "http://old-b"


def test_storage_dict_round_trips_nested_shape():
    ch = Channel(
        name="Nested",
        api_key="sk-test",
        models=["claude-3"],
        endpoints=[Endpoint(api_type="anthropic", base_url="http://example.com")],
    )

    d = ch.to_storage_dict()

    for flat_key in (
        "api_type",
        "base_url",
        "endpoint_url",
        "models_url",
        "anthropic_version",
        "anthropic_version_policy",
        "anthropic_beta",
        "anthropic_beta_policy",
    ):
        assert flat_key not in d, f"to_storage_dict 不应包含扁平键 {flat_key}"
    assert d["api_key"] == "sk-test"

    rebuilt = Channel(**d)
    assert rebuilt.to_storage_dict() == d, "持久化字典应稳定往返"
    assert rebuilt.endpoints[0].base_url == "http://example.com"


def test_channel_model_overrides_default_empty_and_round_trip():
    ch = Channel(
        name="Models",
        api_key="sk-test",
        models=["vision-1"],
        model_overrides={"vision-1": {"capabilities": {"input_modalities": {"image": "supported"}}}},
        endpoints=[Endpoint(api_type="openai-chat-completions", base_url="http://example.com")],
    )

    assert Channel(name="Empty", api_key="k", endpoints=ch.endpoints).model_overrides == {}
    assert ch.to_storage_dict()["model_overrides"]["vision-1"]["capabilities"]["input_modalities"]["image"] == "supported"


def test_channel_and_endpoint_reject_old_modality_scopes():
    with pytest.raises(ValidationError, match="渠道不能覆盖"):
        ChannelCreate(
            name="Wrong channel scope",
            api_key="k",
            profile_overrides={"capabilities": {"input_modalities": {"image": "supported"}}},
            endpoints=[{"api_type": "openai-chat-completions", "base_url": "http://example.com"}],
        )
    with pytest.raises(ValidationError, match="接入点模型覆盖"):
        ChannelCreate(
            name="Wrong endpoint scope",
            api_key="k",
            endpoints=[
                {
                    "api_type": "openai-chat-completions",
                    "base_url": "http://example.com",
                    "model_overrides": {"m": {"capabilities": {"input_modalities": {"image": "supported"}}}},
                }
            ],
        )


def test_channel_create_rejects_flat_body():
    """票据05 契约收紧：管理端 Create 拒绝扁平 body（存储迁移的容忍仅保留在 Channel 本体）"""
    with pytest.raises(ValidationError, match="扁平"):
        ChannelCreate(
            name="New",
            api_type="openai-chat-completions",
            base_url="http://new.example.com",
            endpoint_url=None,
            api_key="sk-new",
            models=["gpt-4o"],
        )


def test_serialized_view_exposes_only_nested_shape():
    """GET /admin/channels 用 model_dump() 渲染——序列化输出只含嵌套形态，无顶层扁平视图键"""
    ch = Channel(
        name="Nested",
        api_key="sk-test",
        endpoints=[Endpoint(api_type="anthropic", base_url="http://example.com")],
    )

    view = ch.model_dump()

    for flat_key in ("api_type", "base_url", "endpoint_url"):
        assert flat_key not in view
    assert [ep["api_type"] for ep in view["endpoints"]] == ["anthropic"]


# ─── 两级结构终态契约（票据07-B：兼容视图删除） ───


def test_channel_hides_flat_protocol_attributes():
    """嵌套构造的 Channel 不再暴露任何扁平协议属性——兼容访问器已整体删除"""
    ch = Channel(
        name="Nested",
        api_key="sk-test",
        endpoints=[Endpoint(api_type="anthropic", base_url="http://example.com")],
    )

    for flat_attr in (
        "api_type",
        "base_url",
        "endpoint_url",
        "models_url",
        "anthropic_version",
        "anthropic_beta",
    ):
        assert not hasattr(ch, flat_attr), f"Channel 不应再暴露扁平协议属性 {flat_attr}"


# ─── 评审修复的回归 ───


def test_channel_rejects_zero_endpoints():
    """无接入点的渠道会让兼容视图返回 None 并污染下游缓存键——构造期拒绝"""
    with pytest.raises(ValidationError):
        Channel(name="ghost", api_key="k")


def test_endpoint_url_validation_covers_all_endpoints():
    """嵌套创建的非首个接入点同样必须过出站校验（评审发现的安全缺口）"""
    from fastapi import HTTPException

    from routers.admin.channels import _validate_endpoints_outbound_urls

    endpoints = [
        Endpoint(api_type="anthropic", base_url="https://ok.example.com"),
        Endpoint(api_type="openai-chat-completions", base_url="ftp://bad.example.com"),
    ]
    with pytest.raises(HTTPException):
        _validate_endpoints_outbound_urls(endpoints)
