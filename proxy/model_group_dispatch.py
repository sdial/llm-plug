"""Model Group 调度：条目回退、Hard Binding、Sticky Session 与 Time Window 的唯一住所。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

import config
from channel_catalog import catalog
from models.api_types import APIType
from models.channel import Channel
from models.model_group import ModelGroup
from proxy import conversion, outcomes
from proxy.channel_attempt import ChannelAttemptInput, StreamAttemptResult, attempt_channel
from proxy.dispatcher import DispatchContext, _to_upstream_http_status_error, dispatch, dispatch_pinned
from proxy.errors import AllChannelsExhausted


@dataclass(frozen=True, slots=True)
class ModelGroupRequestContext:
    """一次 Model Group 请求的不可变元数据；尝试时仍复制为普通 dict。"""

    request_data: Mapping[str, Any]
    target_api_type: APIType
    is_stream: bool
    query_string: str | None
    client_headers: dict[str, str] | None
    api_key_id: str | None
    client_ip: str | None
    request_source: str = "client"


def _get_schedule_tz() -> tzinfo:
    name = (config.get_setting("aggregation_timezone") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or UTC


def _schedule_window_value(window: Any, key: str, default: Any = None) -> Any:
    if isinstance(window, dict):
        return window.get(key, default)
    return getattr(window, key, default)


def is_model_blocked_by_schedule(model: str, model_schedules: dict | list) -> bool:
    """检查 Model Group 条目或设置中的模型当前是否处于 Time Window。"""
    if isinstance(model_schedules, list):
        windows = model_schedules
    elif isinstance(model_schedules, dict):
        windows = model_schedules.get(model)
    else:
        windows = None
    if not windows:
        return False
    now = datetime.now(_get_schedule_tz())
    current_minutes = now.hour * 60 + now.minute
    for window in windows:
        if not _schedule_window_value(window, "enabled", True):
            continue
        start_str = _schedule_window_value(window, "start", "")
        end_str = _schedule_window_value(window, "end", "")
        if not start_str or not end_str:
            continue
        try:
            sh, sm = int(start_str[:2]), int(start_str[3:5])
            eh, em = int(end_str[:2]), int(end_str[3:5])
        except (ValueError, IndexError):
            continue
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if start_min <= end_min:
            if start_min <= current_minutes < end_min:
                return True
        elif current_minutes >= start_min or current_minutes < end_min:
            return True
    return False


async def execute_model_group_request(group: ModelGroup, context: ModelGroupRequestContext) -> tuple[Any, Channel]:
    """按 Item 顺序完成一次 Model Group 调度，严格复用既有渠道调度语义。"""
    attempted_models: list[str] = []
    dispatch_context = DispatchContext()

    async def attempt(channel: Channel, wait_budget: float, model: str) -> tuple[Any, Channel]:
        request_data = {**context.request_data, "model": model}
        result = await attempt_channel(
            channel,
            ChannelAttemptInput(
                payload=request_data,
                inbound_api_type=context.target_api_type,
                requested_model=group.name,
                serving_model=model,
                is_stream=context.is_stream,
                query_string=context.query_string,
                client_headers=context.client_headers,
                api_key_id=context.api_key_id,
                client_ip=context.client_ip,
                request_source=context.request_source,
            ),
            wait_budget=wait_budget,
        )
        output = result.stream if isinstance(result, StreamAttemptResult) else result.response
        return output, result.channel

    def make_attempt_fn(model: str):
        async def attempt_fn(channel: Channel, wait_budget: float) -> tuple[Any, Channel]:
            return await attempt(channel, wait_budget, model)

        return attempt_fn

    def make_not_degraded(model: str):
        def not_degraded(channel: Channel) -> bool:
            return not outcomes.is_degraded(model, channel.id)

        return not_degraded

    for entry in group.items:
        current_model = entry.model
        if is_model_blocked_by_schedule(current_model, entry.schedules):
            logger.debug(f"模型 {current_model} 在组 {group.name} 中被定时屏蔽，跳过")
            continue
        if current_model not in attempted_models:
            attempted_models.append(current_model)

        if entry.channel_id:
            channels = await catalog.channels_for_model(current_model)
            bound = next((channel for channel in channels if channel.id == entry.channel_id and channel.enabled), None)
            if bound is None:
                logger.warning(f"模型组 {group.name} 条目绑定渠道 {entry.channel_id} 不存在或未启用，跳过")
                continue
            try:
                return await dispatch_pinned(
                    bound,
                    make_attempt_fn(current_model),
                    model=current_model,
                    group=group,
                    context=dispatch_context,
                    admission=False,
                )
            except AllChannelsExhausted:
                continue

        channels = await catalog.channels_for_model(current_model)
        if not channels:
            continue
        channels = conversion.filter_channels_by_conversion(channels, context.target_api_type)
        if not channels:
            continue

        attempt_fn = make_attempt_fn(current_model)
        use_lazy = bool(getattr(group, "lazy_sticky", False))
        not_degraded = make_not_degraded(current_model)

        if use_lazy:
            pref_id = outcomes.sticky_preferred(group.id, current_model)
            if pref_id and pref_id not in dispatch_context.tried:
                pref_ch = next((channel for channel in channels if channel.id == pref_id), None)
                if pref_ch is not None:
                    try:
                        result, served_channel = await dispatch_pinned(
                            pref_ch,
                            attempt_fn,
                            model=current_model,
                            group=group,
                            context=dispatch_context,
                            yield_predicate=not_degraded,
                            admission=True,
                            client_ip=context.client_ip,
                            api_key_id=context.api_key_id,
                            client_headers=context.client_headers,
                        )
                        outcomes.remember_preferred(group.id, current_model, served_channel.id)
                        return result, served_channel
                    except AllChannelsExhausted:
                        pass

        try:
            result, served_channel = await dispatch(
                channels,
                attempt_fn,
                model=current_model,
                group=group,
                context=dispatch_context,
                yield_predicate=not_degraded if use_lazy else None,
                client_ip=context.client_ip,
                api_key_id=context.api_key_id,
                client_headers=context.client_headers,
            )
            if use_lazy:
                outcomes.remember_preferred(group.id, current_model, served_channel.id)
            return result, served_channel
        except AllChannelsExhausted:
            continue

    attempted = ", ".join(attempted_models) or "none"
    if dispatch_context.last_error is not None:
        raise AllChannelsExhausted(
            f"模型组 Fallback 已穷尽所有模型: group={group.name}, attempted_models=[{attempted}], last_error={dispatch_context.last_error}",
            last_error=_to_upstream_http_status_error(dispatch_context.last_error),
        ) from dispatch_context.last_error
    raise AllChannelsExhausted(f"模型组 Fallback 已穷尽所有模型: group={group.name}, attempted_models=[{attempted}], no available channels")
