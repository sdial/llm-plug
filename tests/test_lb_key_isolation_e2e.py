"""ADR-0008 D0 (model, channel) 键隔离 e2e：同渠道 A 模型挂死不污染 B 模型。

场景：单一渠道同时服务 deepseek-chat 与 deepseek-chat-done 两个模型，
上游对 deepseek-chat 始终 500、对 deepseek-chat-done 正常返回。
A 模型（deepseek-chat）累计 3 次失败熔断后，B 模型（deepseek-chat-done）
在同一渠道上仍可选可用（A 挂不死 B）。
"""

import json
import os

_BASE = "http://127.0.0.1:19999"
_E2E_CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "_test_data", "channels.json")


def _ep(api_type: str, base_url: str) -> dict:
    return {"api_type": api_type, "base_url": base_url}


def _reset_storage_caches() -> None:
    """绕过 TTL：直改文件后同步清 storage 与渠道注册缓存（AGENTS.md 测试场景豁免）"""
    import storage
    from channel_catalog import catalog

    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None


def _write_single_dual_model_channel() -> None:
    """单一渠道，同一接入点同时服务两个模型。"""
    channels_data = {
        "channels": [
            {
                "id": "ch_key_iso",
                "name": "Key Isolation",
                "api_key": "test-key",
                "models": ["deepseek-chat", "deepseek-chat-done"],
                "enabled": True,
                "weight": 1,
                "priority": 1,
                "endpoints": [_ep("openai-chat-completions", f"{_BASE}/fail-deepseek")],
            }
        ]
    }
    with open(_E2E_CHANNELS_FILE, "w") as f:
        json.dump(channels_data, f)
    _reset_storage_caches()


def _chat_body(model: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }


def test_model_specific_failure_does_not_block_other_model(e2e_client):
    """同渠道 A 模型 3 次失败熔断后，B 模型仍可选可用（A 挂不死 B）。"""
    from proxy import outcomes

    # 隔离既有状态并把熔断阈值钉在 spec 默认（3 次 / 120s）：
    # 注意仓库 data/settings.json 显式保存了 5/60，e2e 需覆盖为 3/120。
    outcomes.reset()
    outcomes.configure(max_fail_count=3, cooldown_seconds=120)
    _write_single_dual_model_channel()

    # A 模型连续 3 次失败 → (deepseek-chat, ch) 熔断
    for _ in range(3):
        resp = e2e_client.post("/v1/chat/completions", json=_chat_body("deepseek-chat"))
        assert resp.status_code == 500

    # A 模型在此渠道上已不可用
    assert outcomes.is_healthy("deepseek-chat", "ch_key_iso") is False
    # B 模型在同一渠道上仍健康
    assert outcomes.is_healthy("deepseek-chat-done", "ch_key_iso") is True

    # A 模型继续请求仍失败（无健康渠道 → AllChannelsExhausted → 502）
    resp = e2e_client.post("/v1/chat/completions", json=_chat_body("deepseek-chat"))
    assert resp.status_code == 502

    # B 模型在同一渠道上正常服务
    resp = e2e_client.post("/v1/chat/completions", json=_chat_body("deepseek-chat-done"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello world"
