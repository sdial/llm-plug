"""代理请求编排本体。

入口识别单模型或 Model Group；单渠道接入点回退留在本模块，Model Group 编排
留在 :mod:`proxy.model_group_dispatch`，渠道选择→尝试→429预算→排除的唯一循环
留在 :mod:`proxy.dispatcher`。
"""

from typing import Any

from channel_catalog import catalog
from models.api_types import APIType
from models.channel import Channel
from proxy import conversion as _conversion
from proxy.channel_attempt import ChannelAttemptInput, StreamAttemptResult, attempt_channel
from proxy.dispatcher import _to_upstream_http_status_error
from proxy.dispatcher import dispatch as _dispatch
from proxy.errors import AllChannelsExhausted
from response_state import get_responses_store

# Web 各模块共享同一 Responses State store；保留既有单例接口。
_responses_store = get_responses_store()


async def proxy_request(
    model: str,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool = False,
    query_string: str | None = None,
    client_headers: dict[str, str] | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    request_source: str = "client",
) -> tuple[Any, Channel]:
    """执行代理请求；Model Group 调用其真实编排模块，单模型保留本地调度接线。"""
    group = await catalog.get_model_group_by_name(model)
    if group:
        # 运行期导入避免 Model Group 模块调用真实 attempt 接口时形成循环导入。
        from proxy.model_group_dispatch import ModelGroupRequestContext, execute_model_group_request

        return await execute_model_group_request(
            group,
            ModelGroupRequestContext(request_data, target_api_type, is_stream, query_string, client_headers, api_key_id, client_ip, request_source),
        )
    return await _proxy_single_model_request(
        model,
        request_data,
        target_api_type,
        is_stream,
        query_string,
        client_headers,
        api_key_id,
        client_ip,
        request_source=request_source,
    )


async def _proxy_single_model_request(
    model: str,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None,
    client_headers: dict[str, str] | None,
    api_key_id: str | None,
    client_ip: str | None,
    request_source: str = "client",
) -> tuple[Any, Channel]:
    """单模型请求：候选池经 dispatcher 选路、尝试、429 预算和排除。"""
    channels = await catalog.channels_for_model(model)
    if not channels:
        raise ValueError(f"没有可用渠道支持模型: {model}")
    channels = _conversion.filter_channels_by_conversion(channels, target_api_type)
    if not channels:
        raise ValueError(f"模型 {model} 没有可用的同格式渠道（已禁止跨格式转换），客户端格式={target_api_type.value}")

    async def attempt_fn(channel: Channel, wait_budget: float) -> tuple[Any, Channel]:
        result = await attempt_channel(
            channel,
            ChannelAttemptInput(
                payload=request_data,
                inbound_api_type=target_api_type,
                requested_model=model,
                serving_model=model,
                is_stream=is_stream,
                query_string=query_string,
                client_headers=client_headers,
                api_key_id=api_key_id,
                client_ip=client_ip,
                request_source=request_source,
            ),
            wait_budget=wait_budget,
        )
        output = result.stream if isinstance(result, StreamAttemptResult) else result.response
        return output, result.channel

    try:
        return await _dispatch(channels, attempt_fn, model=model, client_ip=client_ip, api_key_id=api_key_id, client_headers=client_headers)
    except AllChannelsExhausted as exhausted:
        if exhausted.last_error is not None:
            raise _to_upstream_http_status_error(exhausted.last_error) from exhausted.last_error
        raise
