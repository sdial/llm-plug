"""
通用代理逻辑，供三个代理路由共用
"""
import json
from typing import Any, Optional

import httpx

from balancer.load_balancer import load_balancer
from client import create_client, get_upstream_headers
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from storage import load_data


def _get_channels_for_model(model: str) -> list[Channel]:
    data = load_data()
    channels = [Channel(**ch) for ch in data.get("channels", [])]
    return [ch for ch in channels if model in ch.models and ch.enabled]


def _get_converter_and_upstream_type(
    channel: Channel, target_api_type: APIType
) -> tuple:
    """根据渠道类型和目标API类型，获取转换器和上游请求类型"""
    source = channel.api_type.value
    target = target_api_type.value

    if source == target:
        return None, source

    if target == "openai-chat-completions":
        return ToChatCompletionsConverter(), source
    elif target == "openai-response":
        return ToResponseConverter(), source
    elif target == "anthropic":
        return ToAnthropicConverter(), source

    return None, source


def _get_upstream_url(channel: Channel, target_api_type: APIType) -> str:
    base = channel.base_url.rstrip("/")
    # 根据渠道实际API类型决定上游URL
    actual_type = channel.api_type.value
    if actual_type == "openai-chat-completions":
        return f"{base}/v1/chat/completions"
    elif actual_type == "openai-response":
        return f"{base}/v1/responses"
    elif actual_type == "anthropic":
        return f"{base}/v1/messages"
    return base


async def proxy_request(
    model: str,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool = False,
) -> tuple[Any, Channel]:
    """
    执行代理请求，返回 (response_data_or_stream, selected_channel)
    自动进行负载均衡和故障转移
    """
    channels = _get_channels_for_model(model)
    if not channels:
        raise ValueError(f"没有可用渠道支持模型: {model}")

    selected = load_balancer.select_channel(channels)
    if not selected:
        raise ValueError(f"模型 {model} 的所有渠道均不可用")

    # 尝试请求，失败则故障转移
    all_tried = {selected.id}
    last_error = None

    while True:
        try:
            return await _do_request(selected, request_data, target_api_type, is_stream), selected
        except Exception as e:
            load_balancer.record_failure(selected.id)
            last_error = e
            # 故障转移
            fallback_channels = load_balancer.get_fallback_channels(channels, exclude_ids=all_tried)
            if not fallback_channels:
                break
            selected = fallback_channels[0]
            all_tried.add(selected.id)

    raise last_error or RuntimeError("所有渠道请求失败")


async def _do_request(
    channel: Channel,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
):
    converter, source_type = _get_converter_and_upstream_type(channel, target_api_type)

    # 转换请求
    if converter:
        upstream_data = converter.convert_request(request_data, source_type)
    else:
        upstream_data = request_data

    url = _get_upstream_url(channel, target_api_type)
    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    client = create_client(channel)

    try:
        if is_stream:
            return await _do_stream_request(client, url, headers, upstream_data, converter, source_type)
        else:
            resp = await client.post(url, json=upstream_data, headers=headers)
            resp.raise_for_status()
            response_data = resp.json()

            # 转换响应
            if converter:
                response_data = converter.convert_response(response_data, source_type)

            load_balancer.record_success(channel.id)
            return response_data
    except Exception:
        raise


async def _do_stream_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    upstream_data: dict,
    converter,
    source_type: str,
):
    """流式请求，yield SSE数据行"""
    async with client.stream("POST", url, json=upstream_data, headers=headers) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.strip():
                continue
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    yield "data: [DONE]\n\n"
                    continue
                try:
                    chunk = json.loads(data_str)
                    if converter:
                        converted = converter.convert_stream_chunk(chunk, source_type)
                        if converted is not None:
                            yield f"data: {json.dumps(converted, ensure_ascii=False)}\n\n"
                    else:
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                except json.JSONDecodeError:
                    yield line + "\n\n"
            else:
                yield line + "\n\n"
