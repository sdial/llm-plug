from fastapi import APIRouter, HTTPException

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
