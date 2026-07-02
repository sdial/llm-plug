import asyncio
import threading

from loguru import logger
from pydantic import ValidationError

import storage
from balancer.load_balancer import load_balancer
from models.channel import Channel
from storage import register_save_callback


_model_channels_cache: dict[str, list[Channel]] | None = None
_model_channels_cache_version = 0
_model_channels_lock = asyncio.Lock()
_model_channels_sync_lock = threading.Lock()  # 保护同步回调对全局变量的写操作
_background_tasks: set[asyncio.Task] = set()


async def invalidate_model_channels_cache() -> None:
    global _model_channels_cache, _model_channels_cache_version
    async with _model_channels_lock:
        _model_channels_cache = None
        _model_channels_cache_version += 1
    data = await storage.load_data()
    active_ids = {ch.get("id") for ch in data.get("channels", [])}
    await load_balancer.cleanup_removed_channels(active_ids)


def schedule_invalidate_model_channels_cache() -> None:
    global _model_channels_cache, _model_channels_cache_version
    with _model_channels_sync_lock:
        _model_channels_cache = None
        _model_channels_cache_version += 1
    try:
        import sys

        core = sys.modules.get("proxy.core")
        sync = getattr(core, "_sync_model_channel_state_after_registry_callback", None)
        if sync is not None:
            sync()
    except Exception:
        pass
    try:
        task = asyncio.create_task(cleanup_removed_channels_after_save())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        pass


register_save_callback(schedule_invalidate_model_channels_cache)


async def cleanup_removed_channels_after_save() -> None:
    data = await storage.load_data()
    active_ids = {ch.get("id") for ch in data.get("channels", [])}
    await load_balancer.cleanup_removed_channels(active_ids)


async def get_channels_for_model(model: str) -> list[Channel]:
    global _model_channels_cache
    async with _model_channels_lock:
        while True:
            cache = _model_channels_cache
            if cache is not None:
                return cache.get(model, [])

            version = _model_channels_cache_version
            data = await storage.load_data()
            channels: list[Channel] = []
            for idx, raw_channel in enumerate(data.get("channels", [])):
                try:
                    channels.append(Channel(**raw_channel))
                except (TypeError, ValidationError) as exc:
                    channel_id = (
                        raw_channel.get("id") if isinstance(raw_channel, dict) else None
                    )
                    logger.warning(
                        f"skip invalid channel entry index={idx} id={channel_id}: {exc}"
                    )

            next_cache: dict[str, list[Channel]] = {}
            for ch in channels:
                if not ch.enabled:
                    continue
                for m in ch.models:
                    next_cache.setdefault(m, []).append(ch)

            if version != _model_channels_cache_version:
                continue

            _model_channels_cache = next_cache
            return next_cache.get(model, [])

_invalidate_model_channels_cache = invalidate_model_channels_cache
_schedule_invalidate_model_channels_cache = schedule_invalidate_model_channels_cache
_cleanup_removed_channels_after_save = cleanup_removed_channels_after_save
_get_channels_for_model = get_channels_for_model
