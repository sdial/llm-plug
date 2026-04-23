from fastapi import APIRouter, HTTPException

from client import create_client, get_upstream_headers
from models.channel import Channel, ChannelCreate, ChannelUpdate
from storage import load_data, save_data

router = APIRouter(prefix="/admin", tags=["管理"])


def _get_channels() -> list[Channel]:
    data = load_data()
    return [Channel(**ch) for ch in data.get("channels", [])]


def _save_channels(channels: list[Channel]):
    save_data({"channels": [ch.model_dump() for ch in channels]})


@router.get("/channels", response_model=list[Channel])
def list_channels():
    """获取所有渠道"""
    return _get_channels()


@router.post("/channels", response_model=Channel)
def create_channel(body: ChannelCreate):
    """添加渠道"""
    channels = _get_channels()
    channel = Channel(**body.model_dump())
    channels.append(channel)
    _save_channels(channels)
    return channel


@router.put("/channels/{channel_id}", response_model=Channel)
def update_channel(channel_id: str, body: ChannelUpdate):
    """更新渠道"""
    channels = _get_channels()
    for i, ch in enumerate(channels):
        if ch.id == channel_id:
            update_data = body.model_dump(exclude_unset=True)
            updated = ch.model_copy(update=update_data)
            channels[i] = updated
            _save_channels(channels)
            return updated
    raise HTTPException(status_code=404, detail="渠道不存在")


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: str):
    """删除渠道"""
    channels = _get_channels()
    new_channels = [ch for ch in channels if ch.id != channel_id]
    if len(new_channels) == len(channels):
        raise HTTPException(status_code=404, detail="渠道不存在")
    _save_channels(new_channels)
    return {"message": "删除成功"}


@router.patch("/channels/{channel_id}/toggle", response_model=Channel)
def toggle_channel(channel_id: str):
    """启用/禁用渠道"""
    channels = _get_channels()
    for i, ch in enumerate(channels):
        if ch.id == channel_id:
            updated = ch.model_copy(update={"enabled": not ch.enabled})
            channels[i] = updated
            _save_channels(channels)
            return updated
    raise HTTPException(status_code=404, detail="渠道不存在")


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str):
    """测试渠道连通性：发送最简prompt，检查返回"""
    import time

    channels = _get_channels()
    channel = next((ch for ch in channels if ch.id == channel_id), None)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")

    if not channel.models:
        return {"success": False, "message": "渠道无可用模型", "latency_ms": None}

    model = channel.models[0]
    api_type = channel.api_type.value
    base = channel.base_url.rstrip("/")

    # 根据api_type构建请求
    if api_type == "openai-chat-completions":
        url = f"{base}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    elif api_type == "openai-response":
        url = f"{base}/v1/responses"
        payload = {
            "model": model,
            "input": "Hi",
            "max_output_tokens": 5,
        }
    elif api_type == "anthropic":
        url = f"{base}/v1/messages"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
    else:
        return {"success": False, "message": f"不支持的API类型: {api_type}", "latency_ms": None}

    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    client = create_client(channel, timeout=30.0)
    start = time.monotonic()
    try:
        resp = await client.post(url, json=payload, headers=headers)
        latency_ms = round((time.monotonic() - start) * 1000)
        resp.raise_for_status()
        data = resp.json()

        # 基本返回校验
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
            "model": model,
            "latency_ms": latency_ms,
            "reply": reply,
        }
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "success": False,
            "message": f"请求失败: {str(e)}",
            "model": model,
            "latency_ms": latency_ms,
            "reply": None,
        }
    finally:
        await client.aclose()
