"""Channel Attempt：对一个已选 Channel 完成 Endpoint Fallback。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from loguru import logger

import config
from conversion_plan import IncompatibleRequestError, IncompatibleResponseError
from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy import conversion, outcomes
from proxy.endpoint_execution import EndpointExecutionInput, execute_endpoint
from proxy.errors import _EmptyStreamError, _is_channel_config_error, _is_retryable_exception, _StreamPreflightError, classify_failure
from rate_limiting import _is_rate_limit_exception


@dataclass(frozen=True, slots=True)
class ChannelAttemptInput:
    """一次 Channel Attempt 的不可变源事实。"""

    payload: Mapping[str, Any]
    inbound_api_type: APIType
    requested_model: str
    serving_model: str
    is_stream: bool
    query_string: str | None = None
    client_headers: Mapping[str, str] | None = None
    api_key_id: str | None = None
    client_ip: str | None = None
    request_source: str = "client"


@dataclass(frozen=True, slots=True)
class NonStreamAttemptResult:
    response: Any
    channel: Channel
    endpoint: Endpoint


@dataclass(frozen=True, slots=True)
class StreamAttemptResult:
    stream: AsyncIterator[str]
    channel: Channel
    endpoint: Endpoint


ChannelAttemptResult: TypeAlias = NonStreamAttemptResult | StreamAttemptResult


class ChannelAttemptExhausted(Exception):
    """一个 Channel 的全部 Endpoint Execution 已穷尽。"""

    def __init__(self, channel: Channel, cause: Exception):
        self.channel = channel
        self.cause = cause
        super().__init__(f"渠道 {channel.name} 全部接入点失败: {cause}")


@dataclass(slots=True)
class _AttemptAccounting:
    """Channel Attempt 的首包前 Health 记账状态。"""

    failure_recorded: bool = False

    def record_failure_once(self, input: ChannelAttemptInput, channel: Channel, exc: BaseException) -> None:
        if self.failure_recorded:
            return
        effective_model = outcomes.effective_model(
            model=input.serving_model,
            body_model=input.payload.get("model"),
            requested_model=input.requested_model,
            channel_id=channel.id,
        )
        outcomes.record(effective_model, channel.id, classify_failure(exc))
        self.failure_recorded = True


def _is_heartbeat_chunk(chunk: Any) -> bool:
    if not isinstance(chunk, str):
        return False
    lines = [line for line in chunk.split("\n") if line.strip()]
    return bool(lines) and all(line.strip().startswith(":") for line in lines)


async def _prime_stream(stream):
    """取得首个有效事件并返回可重放流；首包前错误仍属于 Channel Attempt。"""
    first_chunk = None
    try:
        while first_chunk is None or _is_heartbeat_chunk(first_chunk):
            first_chunk = await anext(stream)
    except StopAsyncIteration:
        raise _EmptyStreamError("上游流式响应为空，没有任何 SSE 输出") from None
    except _StreamPreflightError as exc:
        raise exc.original from exc

    async def replay():
        try:
            yield first_chunk
            async for chunk in stream:
                yield chunk
        finally:
            try:
                await asyncio.wait_for(stream.aclose(), timeout=2.0)
            except TimeoutError:
                logger.warning("[STREAM PRIME ACLOSE TIMEOUT] stream.aclose() timeout 2.0s")
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as exc:
                logger.warning(f"close prime stream error: {exc}")

    return replay()


async def attempt_channel(
    channel: Channel,
    input: ChannelAttemptInput,
    *,
    wait_budget: float,
) -> ChannelAttemptResult:
    """在一致设置快照下尝试一个 Channel 的可用 Endpoint。"""
    attempts = conversion.resolve_endpoint_attempts(channel, input.inbound_api_type)
    if not attempts:
        cause = ValueError(f"渠道 {channel.name} 无可用接入点（全停用或格式门控排除）")
        raise ChannelAttemptExhausted(channel, cause)

    settings = copy.deepcopy(config.get_settings())
    accounting = _AttemptAccounting()
    has_native = attempts[0].api_type == input.inbound_api_type
    last_error: Exception | None = None

    endpoint_input = EndpointExecutionInput(
        payload=input.payload,
        inbound_api_type=input.inbound_api_type,
        requested_model=input.requested_model,
        serving_model=input.serving_model,
        is_stream=input.is_stream,
        query_string=input.query_string,
        client_headers=input.client_headers,
        api_key_id=input.api_key_id,
        client_ip=input.client_ip,
        request_source=input.request_source,
    )

    for endpoint in attempts:
        if has_native and endpoint.api_type != input.inbound_api_type:
            logger.warning(
                f"[ENDPOINT FALLBACK] 渠道={channel.name} 原生入口 {input.inbound_api_type.value} 失败，回退至 {endpoint.api_type.value} 转格式重试"
            )
        try:
            output = await execute_endpoint(
                channel,
                endpoint,
                endpoint_input,
                settings=settings,
                wait_budget=wait_budget,
            )
            if input.is_stream:
                stream = await _prime_stream(output)
                return StreamAttemptResult(stream=stream, channel=channel, endpoint=endpoint)
            return NonStreamAttemptResult(response=output, channel=channel, endpoint=endpoint)
        except Exception as exc:
            if isinstance(exc, (IncompatibleRequestError, IncompatibleResponseError)):
                last_error = exc
                continue
            if _is_rate_limit_exception(exc):
                raise
            if _is_retryable_exception(exc) or _is_channel_config_error(exc):
                accounting.record_failure_once(input, channel, exc)
                last_error = exc
                continue
            raise

    assert last_error is not None
    raise ChannelAttemptExhausted(channel, last_error) from last_error


__all__ = [
    "ChannelAttemptExhausted",
    "ChannelAttemptInput",
    "ChannelAttemptResult",
    "NonStreamAttemptResult",
    "StreamAttemptResult",
    "attempt_channel",
]
