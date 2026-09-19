"""票据 03 — 协议属性随选定接入点生效（URL / header / Capability，ADR-0006）。

E2E 缝：TestClient + mock 上游；协议属性（url_override / anthropic 头 /
api_key_override / vendor 推断）必须取自每次请求实际服务的接入点，
而非渠道扁平字段或固定首接入点。约定同 test_endpoint_fallback_e2e.py：
直写 _test_data/channels.json + 清 Channel Catalog / Access Key 缓存；
mock server 是独立进程，计数经 HTTP 端点读写。
"""

import json
import os

import httpx

_E2E_CHANNELS_FILE = os.path.join(os.path.dirname(__file__), "_test_data", "channels.json")
_BASE = "http://127.0.0.1:19999"


def _ep(api_type: str, base_url: str, **kw) -> dict:
    return {"api_type": api_type, "base_url": base_url, **kw}


def _write_e2e_channels(channels: list[dict]) -> None:
    with open(_E2E_CHANNELS_FILE, "w") as f:
        json.dump({"channels": channels}, f)
    _reset_storage_caches()


def _reset_storage_caches() -> None:
    """绕过 TTL：直改文件后同步清 storage 与渠道注册缓存（AGENTS.md 测试场景豁免）"""
    import storage
    from channel_catalog import catalog

    catalog.reset()
    storage._keys_cache = None
    storage._keys_cache_ts = 0
    storage._keys_lock = None


def _reset_counts() -> None:
    # mock server 是独立进程：必须用真实 HTTP 客户端访问（TestClient 会拦截到自身 app）
    with httpx.Client(timeout=5) as hc:
        hc.post(f"{_BASE}/_test/reset-counts")


def _upstream_counts() -> dict:
    with httpx.Client(timeout=5) as hc:
        return hc.get(f"{_BASE}/_test/request-counts").json()


_ANTHROPIC_BODY = {
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}

_OPENAI_BODY = {
    "model": "gpt-4o",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}


# ─── 1. url_override 分入口生效 ───


def test_url_override_applies_per_selected_endpoint(e2e_client):
    """双接入点各配不同 url_override：两种入口格式各自命中所选接入点的覆写路径"""
    _write_e2e_channels(
        [
            {
                "id": "ch_proto_ovr",
                "name": "Proto Override Dual",
                # base_url 故意指向不存在的路径：若覆写未生效会 404 而非静默错配
                "api_key": "test-key",
                "models": ["claude-sonnet-4-20250514", "gpt-4o"],
                "endpoints": [
                    _ep(
                        "anthropic",
                        f"{_BASE}/anthropic-unused",
                        url_override=f"{_BASE}/alt-anthropic/v1/messages",
                    ),
                    _ep(
                        "openai-chat-completions",
                        f"{_BASE}/openai-unused",
                        url_override=f"{_BASE}/openai/v1/chat/completions",
                    ),
                ],
            }
        ]
    )
    _reset_counts()

    resp_a = e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY)
    resp_c = e2e_client.post("/v1/chat/completions", json=_OPENAI_BODY)

    assert resp_a.status_code == 200
    assert resp_c.status_code == 200
    counts = _upstream_counts()
    # Anthropic 入口 → anthropic 接入点的覆写路径；Chat 入口 → chat 接入点的覆写路径
    assert counts.get("/alt-anthropic/v1/messages") == 1
    assert counts.get("/openai/v1/chat/completions") == 1
    # base_url 回退拼接路径均未被命中
    assert counts.get("/anthropic/v1/messages") is None
    assert "/anthropic-unused/v1/messages" not in counts
    assert "/openai-unused/v1/chat/completions" not in counts


# ─── 2. per-endpoint anthropic 头 ───


def _hdr_channels() -> list[dict]:
    """两个渠道各一个 anthropic 接入点：不同 base_url、不同版本/beta/密钥。

    渠道内 api_type 不允许重复，因此“双 anthropic 接入点”以两渠道形态落地；
    各自指向不同 echo 路径，响应按 served_by 归因，与 LB 选择顺序无关。
    """
    return [
        {
            "id": "ch_hdr_a",
            "name": "Hdr A",
            "api_key": "key-a",
            "models": ["claude-sonnet-4-20250514"],
            "endpoints": [
                _ep(
                    "anthropic",
                    f"{_BASE}/echo-anthropic",
                    anthropic_version="2023-06-01",
                    anthropic_beta="feature-a",
                )
            ],
        },
        {
            "id": "ch_hdr_b",
            "name": "Hdr B",
            "api_key": "key-b",
            "models": ["claude-sonnet-4-20250514"],
            "endpoints": [
                _ep(
                    "anthropic",
                    f"{_BASE}/alt-anthropic",
                    url_override=f"{_BASE}/echo-anthropic-alt/v1/messages",
                    anthropic_version="2024-10-22",
                    anthropic_beta="feature-b",
                )
            ],
        },
    ]


def test_anthropic_headers_follow_actually_serving_endpoint(e2e_client):
    """上游收到的 anthropic-version / beta 必须等于实际服务接入点的配置"""
    _write_e2e_channels(_hdr_channels())
    _reset_counts()

    bodies = [
        e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY).json(),
        e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY).json(),
    ]

    counts = _upstream_counts()
    assert counts.get("/echo-anthropic/v1/messages") == 1
    assert counts.get("/echo-anthropic-alt/v1/messages") == 1
    # url_override 生效：B 的 base_url 回退拼接路径不应被命中
    assert counts.get("/alt-anthropic/v1/messages") is None

    expected = {
        "/echo-anthropic/v1/messages": ("key-a", "2023-06-01", "feature-a"),
        "/echo-anthropic-alt/v1/messages": ("key-b", "2024-10-22", "feature-b"),
    }
    served_paths = {body["served_by"] for body in bodies}
    assert served_paths == set(expected), f"两个 echo 路径应各服务一次: {bodies}"
    for body in bodies:
        captured = body["captured_headers"]
        key, version, beta = expected[body["served_by"]]
        assert captured["x-api-key"] == key
        assert captured["anthropic-version"] == version
        assert captured["anthropic-beta"] == beta


# ─── 3. api_key_override 生效 ───


def test_endpoint_api_key_override_reaches_upstream(e2e_client):
    """接入点配 api_key_override 时，上游收到覆写密钥而非渠道密钥"""
    _write_e2e_channels(
        [
            {
                "id": "ch_proto_keyovr",
                "name": "Key Override",
                "api_key": "channel-level-key",
                "models": ["claude-sonnet-4-20250514"],
                "endpoints": [
                    _ep(
                        "anthropic",
                        f"{_BASE}/echo-anthropic",
                        api_key_override="endpoint-level-key",
                    )
                ],
            }
        ]
    )
    _reset_counts()

    resp = e2e_client.post("/v1/messages", json=_ANTHROPIC_BODY)

    assert resp.status_code == 200
    assert resp.json()["captured_headers"]["x-api-key"] == "endpoint-level-key"


# ─── 4. Capability 按选定接入点推断（经转换回退路径） ───


def _parse_anthropic_events(lines):
    events = []
    current_event = None
    for line in lines:
        line = line.strip()
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: "):
            try:
                events.append((current_event, json.loads(line[6:])))
            except json.JSONDecodeError:
                events.append((current_event, line[6:]))
    return events


def test_deepseek_capability_applies_via_conversion_fallback_endpoint(e2e_client):
    """Anthropic 入口 → 原生接入点 500 → 渠道内转格式回退命中 deepseek 接入点。

    💭 过滤规则必须来自回退后实际服务接入点的显式档案覆盖，不能依赖 URL 猜测。
    """
    _write_e2e_channels(
        [
            {
                "id": "ch_conv_fb_ds",
                "name": "Conv FB DeepSeek",
                "api_key": "test-key",
                "models": ["claude-sonnet-4-20250514"],
                "endpoints": [
                    _ep("anthropic", f"{_BASE}/fail-anthropic"),
                    {
                        **_ep("openai-chat-completions", f"{_BASE}/deepseek"),
                        "profile_overrides": {"filter_think_content": True},
                    },
                ],
            }
        ]
    )
    _reset_counts()

    with e2e_client.stream("POST", "/v1/messages", json={**_ANTHROPIC_BODY, "stream": True}) as resp:
        assert resp.status_code == 200, resp.read()
        lines = [line for line in resp.iter_lines() if line.strip()]

    events = _parse_anthropic_events(lines)
    text_payloads = [
        delta.get("text", "")
        for evt_type, data in events
        if evt_type == "content_block_delta" and isinstance(data, dict) and (delta := data.get("delta") or {}).get("type") == "text_delta"
    ]
    joined = "".join(text_payloads)
    assert "hidden" not in joined, f"💭 思考块泄漏进 Claude 上下文：{joined!r}"
    assert "visible" in joined, f"正文必须保留：{joined!r}"

    counts = _upstream_counts()
    assert counts.get("/fail-anthropic/v1/messages") == 1
    assert counts.get("/deepseek/v1/chat/completions") == 1
