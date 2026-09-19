"""Endpoint Execution tests 的轻量调用构造器；生产代码不得导入。"""

from typing import Any

import config
from models.api_types import APIType
from models.channel import Channel
from proxy.channel_attempt import ChannelAttemptInput, StreamAttemptResult, attempt_channel
from proxy.endpoint_execution import EndpointExecutionInput, execute_endpoint


async def execute_single_endpoint(
    channel: Channel,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None = None,
    client_headers: dict[str, str] | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    requested_model: str | None = None,
    request_source: str = "client",
    rate_wait_budget: float | None = None,
):
    """用旧测试数据形状调用新的明确 Endpoint interface。"""
    serving_model = str(request_data.get("model", ""))
    return await execute_endpoint(
        channel,
        channel.selected_endpoint(),
        EndpointExecutionInput(
            payload=request_data,
            inbound_api_type=target_api_type,
            requested_model=requested_model or serving_model,
            serving_model=serving_model,
            is_stream=is_stream,
            query_string=query_string,
            client_headers=client_headers,
            api_key_id=api_key_id,
            client_ip=client_ip,
            request_source=request_source,
        ),
        settings=config.get_settings(),
        wait_budget=rate_wait_budget if rate_wait_budget is not None else float(config.get_setting("rate_limit_wait_seconds") or 0),
    )


def endpoint_execution_side_effect(handler):
    """把关注旧式 payload 参数的行为桩接到显式 Endpoint Execution seam。

    仅供大量既有调度行为测试复用；调用点 patch 的始终是新模块真实入口。
    """

    async def execute(channel, endpoint, input, *, settings, wait_budget):
        return await handler(
            channel,
            dict(input.payload),
            input.inbound_api_type,
            input.is_stream,
            requested_model=input.requested_model,
            model=input.serving_model,
            query_string=input.query_string,
            client_headers=dict(input.client_headers or {}),
            api_key_id=input.api_key_id,
            client_ip=input.client_ip,
            request_source=input.request_source,
            rate_wait_budget=wait_budget,
            endpoint=endpoint,
            settings=settings,
        )

    return execute


async def run_channel_attempt(
    channel: Channel,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    *,
    query_string: str | None = None,
    client_headers: dict[str, str] | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    requested_model: str | None = None,
    rate_wait_budget: float | None = None,
    model: str | None = None,
    request_source: str = "client",
):
    """用旧 fixture 形状调用新的 Channel Attempt interface。"""
    serving_model = model or str(request_data.get("model", ""))
    result = await attempt_channel(
        channel,
        ChannelAttemptInput(
            payload=request_data,
            inbound_api_type=target_api_type,
            requested_model=requested_model or serving_model,
            serving_model=serving_model,
            is_stream=is_stream,
            query_string=query_string,
            client_headers=client_headers,
            api_key_id=api_key_id,
            client_ip=client_ip,
            request_source=request_source,
        ),
        wait_budget=rate_wait_budget if rate_wait_budget is not None else float(config.get_setting("rate_limit_wait_seconds") or 0),
    )
    output = result.stream if isinstance(result, StreamAttemptResult) else result.response
    return output, result.channel
