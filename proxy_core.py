"""
通用代理逻辑，供三个代理路由共用
"""
import json
import os
from datetime import datetime
from typing import Any, Optional

import httpx

from balancer.load_balancer import load_balancer
from client import create_client, get_upstream_headers
from config import DEBUG, DEBUG_LOG_DIR
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from storage import load_data


def _log_debug(
    channel: Channel,
    upstream_url: str,
    upstream_data: dict,
    upstream_headers: dict,
    response_data: Any = None,
    is_stream: bool = False,
    stream_content: Any = None,
    response_headers: dict = None,
    status_code: int = None,
    error: str = None,
):
    """记录 debug 日志（包含完整 request + response）"""
    if not DEBUG:
        return

    try:
        # 确保日志目录存在
        os.makedirs(DEBUG_LOG_DIR, exist_ok=True)

        # 生成文件名（按日期）
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(DEBUG_LOG_DIR, f"debug_{today}.jsonl")

        # 构建日志条目 - 完整记录请求和响应
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "channel": {
                "id": channel.id,
                "name": channel.name,
                "api_type": channel.api_type.value,
                "base_url": channel.base_url,
            },
            "request": {
                "url": upstream_url,
                "headers": {k: v for k, v in upstream_headers.items() if k.lower() != "authorization"},
                "body": upstream_data,
            },
            "response": {
                "is_stream": is_stream,
                "status_code": status_code,
                "headers": {k: v for k, v in (response_headers or {}).items() if k.lower() not in ("authorization", "set-cookie")},
            },
        }

        # 根据类型记录响应内容
        if is_stream:
            # 流式：记录所有 chunks（限制大小）
            if stream_content:
                # 只保留前 100 个 chunk 的摘要，避免日志过大
                if isinstance(stream_content, list) and len(stream_content) > 100:
                    log_entry["response"]["stream_chunks_count"] = len(stream_content)
                    log_entry["response"]["stream_content_sample"] = stream_content[:10] + stream_content[-10:]
                else:
                    log_entry["response"]["stream_content"] = stream_content
        else:
            # 非流式：记录完整响应
            log_entry["response"]["data"] = response_data

        if error:
            log_entry["error"] = error

        # 追加写入日志文件
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as log_err:
        # 日志记录失败不应影响主流程，仅打印警告
        print(f"[WARN] Failed to write debug log: {log_err}")


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
            # 返回异步生成器，不使用 await
            return _do_stream_request(client, url, headers, upstream_data, converter, source_type, channel)
        else:
            resp = await client.post(url, json=upstream_data, headers=headers)
            resp.raise_for_status()
            response_data = resp.json()

            # 记录 debug 日志（包含完整响应和响应头）
            _log_debug(
                channel=channel,
                upstream_url=url,
                upstream_data=upstream_data,
                upstream_headers=headers,
                response_data=response_data,
                is_stream=False,
                response_headers=dict(resp.headers),
                status_code=resp.status_code,
            )

            # 转换响应
            if converter:
                response_data = converter.convert_response(response_data, source_type)

            load_balancer.record_success(channel.id)
            return response_data
    except Exception as e:
        # 记录错误日志
        _log_debug(
            channel=channel,
            upstream_url=url,
            upstream_data=upstream_data,
            upstream_headers=headers,
            error=str(e),
        )
        raise


async def _do_stream_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    upstream_data: dict,
    converter,
    source_type: str,
    channel: Channel,
):
    """流式请求，yield SSE数据行"""
    stream_chunks = []  # 收集流式内容用于 debug 日志
    resp_status_code = None
    resp_headers = None

    async with client.stream("POST", url, json=upstream_data, headers=headers) as resp:
        resp.raise_for_status()
        resp_status_code = resp.status_code
        resp_headers = dict(resp.headers)

        # Anthropic 使用特殊的 SSE 格式，包含 event: 和 data: 行
        is_anthropic = "anthropic" in headers.get("anthropic-version", "")

        event_type = None
        async for line in resp.aiter_lines():
            if not line.strip():
                continue
            if is_anthropic and line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    stream_chunks.append("[DONE]")
                    yield "data: [DONE]\n\n"
                    continue
                try:
                    chunk = json.loads(data_str)
                    stream_chunks.append(chunk)  # 记录原始 chunk
                    if converter:
                        if is_anthropic and event_type is not None:
                            chunk["_event_type"] = event_type
                        converted = converter.convert_stream_chunk(chunk, source_type)
                        if converted is not None:
                            yield f"data: {json.dumps(converted, ensure_ascii=False)}\n\n"
                    else:
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                except json.JSONDecodeError:
                    stream_chunks.append(line)
                    yield line + "\n\n"
                if is_anthropic:
                    event_type = None
            else:
                suffix = "\n" if is_anthropic else "\n\n"
                stream_chunks.append(line)
                yield line + suffix

    # 流结束后记录 debug 日志（包含响应头和状态码）
    _log_debug(
        channel=channel,
        upstream_url=url,
        upstream_data=upstream_data,
        upstream_headers=headers,
        is_stream=True,
        stream_content=stream_chunks,
        response_headers=resp_headers,
        status_code=resp_status_code,
    )
