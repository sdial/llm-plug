import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

import httpx

from client import get_upstream_headers, remove_channel_client
from models.channel import Channel, ChannelCreate, ChannelUpdate
from storage import load_data, save_data, get_lock

LOGS_DIR = Path(__file__).parent.parent / "logs"
STATIC_DIR = Path(__file__).parent.parent / "static"

router = APIRouter(prefix="/admin", tags=["管理"])


def _get_channels() -> list[Channel]:
    data = load_data()
    return [Channel(**ch) for ch in data.get("channels", [])]


def _save_channels(channels: list[Channel]):
    save_data({"channels": [ch.model_dump() for ch in channels]})


@router.get("/channels")
def list_channels():
    """获取所有渠道（API Key 脱敏）"""
    channels = _get_channels()
    result = []
    for ch in channels:
        d = ch.model_dump()
        if d.get("api_key"):
            key = d["api_key"]
            d["api_key"] = key[:4] + "***" if len(key) > 4 else "***"
        result.append(d)
    return result


@router.post("/channels", response_model=Channel)
def create_channel(body: ChannelCreate):
    """添加渠道"""
    with get_lock():
        channels = _get_channels()
        channel = Channel(**body.model_dump())
        channels.append(channel)
        _save_channels(channels)
    return channel


@router.put("/channels/{channel_id}", response_model=Channel)
def update_channel(channel_id: str, body: ChannelUpdate):
    """更新渠道"""
    with get_lock():
        channels = _get_channels()
        for i, ch in enumerate(channels):
            if ch.id == channel_id:
                update_data = body.model_dump(exclude_unset=True)
                updated = ch.model_copy(update=update_data)
                channels[i] = updated
                _save_channels(channels)
                # 渠道配置变更（base_url/socks5_proxy）时刷新客户端缓存
                remove_channel_client(ch)
                return updated
    raise HTTPException(status_code=404, detail="渠道不存在")


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: str):
    """删除渠道"""
    with get_lock():
        channels = _get_channels()
        new_channels = [ch for ch in channels if ch.id != channel_id]
        if len(new_channels) == len(channels):
            raise HTTPException(status_code=404, detail="渠道不存在")
        # 移除被删除渠道的客户端缓存
        for ch in channels:
            if ch.id == channel_id:
                remove_channel_client(ch)
                break
        _save_channels(new_channels)
    return {"message": "删除成功"}


@router.patch("/channels/{channel_id}/toggle", response_model=Channel)
def toggle_channel(channel_id: str):
    """启用/禁用渠道"""
    with get_lock():
        channels = _get_channels()
        for i, ch in enumerate(channels):
            if ch.id == channel_id:
                updated = ch.model_copy(update={"enabled": not ch.enabled})
                channels[i] = updated
                _save_channels(channels)
                # 渠道配置变更时刷新客户端缓存
                remove_channel_client(ch)
                return updated
    raise HTTPException(status_code=404, detail="渠道不存在")


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, model: str | None = Query(default=None)):
    """测试渠道连通性：发送最简prompt，检查返回"""
    channels = _get_channels()
    channel = next((ch for ch in channels if ch.id == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")

    if not channel.models:
        return {"success": False, "message": "渠道无可用模型", "latency_ms": None}

    if model:
        if model not in channel.models:
            return {"success": False, "message": f"模型 '{model}' 不在此渠道的模型列表中", "latency_ms": None}
        test_model = model
    else:
        test_model = channel.models[0]
    api_type = channel.api_type.value
    base = channel.base_url.rstrip("/")

    if api_type == "openai-chat-completions":
        url = f"{base}/v1/chat/completions"
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    elif api_type == "openai-response":
        url = f"{base}/v1/responses"
        payload = {
            "model": test_model,
            "input": "Hi",
            "max_output_tokens": 5,
        }
    elif api_type == "anthropic":
        url = f"{base}/v1/messages"
        payload = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    else:
        return {"success": False, "message": f"不支持的API类型: {api_type}", "latency_ms": None}

    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    # 测试使用独立客户端（非缓存），可安全关闭
    if channel.socks5_proxy:
        test_client = httpx.AsyncClient(
            proxy=channel.socks5_proxy,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    else:
        test_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    start = time.monotonic()
    try:
        resp = await test_client.post(url, json=payload, headers=headers)
        latency_ms = round((time.monotonic() - start) * 1000)
        resp.raise_for_status()
        data = resp.json()

        if api_type == "openai-chat-completions":
            choices = data.get("choices", [])
            ok = bool(choices) and choices[0].get("message", {}).get("content") is not None
            reply = choices[0]["message"]["content"][:100] if ok else str(data)[:200]
        elif api_type == "openai-response":
            output = data.get("output", [])
            ok = bool(output)
            reply = str(output[0])[:100] if ok else str(data)[:200]
        elif api_type == "anthropic":
            content = data.get("content", [])
            ok = bool(content)
            reply = content[0].get("text", "")[:100] if ok else str(data)[:200]
        else:
            ok = True
            reply = str(data)[:200]

        return {
            "success": ok,
            "message": "测试通过" if ok else "返回数据格式异常",
            "model": test_model,
            "latency_ms": latency_ms,
            "reply": reply,
        }
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "success": False,
            "message": f"请求失败: {str(e)}",
            "model": test_model,
            "latency_ms": latency_ms,
            "reply": None,
        }
    finally:
        await test_client.aclose()


# ============ Logs API ============


@router.get("/logs")
def list_logs():
    """列出所有日志文件"""
    if not LOGS_DIR.exists():
        return []
    files = sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size} for f in files]


@router.get("/logs/{filename}")
def get_log(filename: str):
    """获取日志文件内容"""
    file_path = (LOGS_DIR / filename).resolve()
    if not file_path.is_relative_to(LOGS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="禁止访问")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, media_type="application/jsonl")
