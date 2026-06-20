"""
通用代理逻辑，供三个代理路由共用
"""
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import ValidationError

import request_logs
import stats
import storage
from balancer.load_balancer import load_balancer
from capability_manager import (
    apply_capability_filter,
    infer_capabilities,
    merge_system_messages,
)
from client import create_client, create_stream_client, get_upstream_headers
from config import LOG_LEVEL, get_setting
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from converters.usage import cache_token_details
from models.api_types import APIType
from models.channel import Channel
from response_state import get_responses_store
from storage import register_save_callback
from think_filter import ThinkFilter, filter_think_content_static
from url_builder import append_query, build_upstream_url

# Responses 状态存储
_responses_store = get_responses_store()

# 流式响应最大记录chunk数量，防止内存溢出
MAX_STREAM_CHUNKS = 2000


def _record_request(**kwargs) -> None:
    """Write lightweight stats and optional debug request log without blocking responses."""
    stats.record_request(**kwargs)
    request_logs.record_request(**kwargs)


def _filter_think_in_response(response_data: dict[str, Any]) -> dict[str, Any]:
    """过滤响应中的 💭 内容。

    支持两种响应格式：
    - Chat Completions: choices[].message.content
    - Responses: output[].content[].text

    Args:
        response_data: 原始响应数据

    Returns:
        过滤后的响应数据
    """
    if not isinstance(response_data, dict):
        return response_data

    result = dict(response_data)

    # Chat Completions 格式
    choices = result.get("choices", [])
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message", {})
        if "content" in msg and isinstance(msg["content"], str):
            msg = dict(msg)
            msg["content"] = filter_think_content_static(msg["content"])
            result["choices"] = [dict(choices[0], message=msg)]
        return result

    # Responses 格式
    output = result.get("output", [])
    if output and isinstance(output, list):
        new_output = []
        for item in output:
            if not isinstance(item, dict):
                new_output.append(item)
                continue
            if item.get("type") == "message":
                content = item.get("content", [])
                if isinstance(content, list):
                    new_content = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in ("output_text", "input_text"):
                            text = part.get("text", "")
                            part = dict(part, text=filter_think_content_static(text))
                        new_content.append(part)
                    item = dict(item, content=new_content)
            new_output.append(item)
        result["output"] = new_output

    # 更新 output_text
    if "output_text" in result:
        result["output_text"] = filter_think_content_static(result["output_text"])

    return result


def _filter_think_in_stream_chunk(chunk: dict[str, Any], think_filter: ThinkFilter) -> dict[str, Any] | None:
    """过滤流式 chunk 中的 💭 内容。

    处理两种格式：
    - Chat Completions: choices[].delta.content
    - Responses: response.output_text.delta 事件

    Args:
        chunk: 流式 chunk 数据
        think_filter: ThinkFilter 实例

    Returns:
        过滤后的 chunk，如果整个 chunk 都被过滤则返回 None
    """
    if not isinstance(chunk, dict):
        return chunk

    event_type = chunk.get("type", "")

    # Responses 格式：response.output_text.delta
    if event_type == "response.output_text.delta":
        delta = chunk.get("delta", "")
        if delta:
            filtered = think_filter.feed(delta)
            if not filtered:
                return None
            return dict(chunk, delta=filtered)

    # Chat Completions 格式：choices[].delta.content
    choices = chunk.get("choices", [])
    if choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta", {})
        content = delta.get("content")
        if content and isinstance(content, str):
            filtered = think_filter.feed(content)
            if not filtered:
                return None
            new_choice = dict(choices[0])
            new_choice["delta"] = dict(delta, content=filtered)
            return dict(chunk, choices=[new_choice])

    return chunk


def _build_stream_response_body(
    chunks: list[Any],
    is_upstream_anthropic: bool,
    model: str,
) -> dict | None:
    """从流式 chunks 构建完整的响应体用于存储。

    Args:
        chunks: 流式响应的 chunk 列表
        is_upstream_anthropic: 上游是否为 Anthropic API
        model: 模型名称

    Returns:
        拼装后的响应体字典，如果无法构建则返回 None
    """
    if not chunks:
        return None

    if is_upstream_anthropic:
        return _build_anthropic_stream_response(chunks, model)
    else:
        return _build_openai_stream_response(chunks, model)


def _build_anthropic_stream_response(chunks: list[Any], model: str) -> dict | None:
    """构建 Anthropic 格式的流式响应体。"""
    message_id = None
    role = "assistant"
    stop_reason = None
    stop_sequence = None
    usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
    blocks: dict[int, dict[str, Any]] = {}
    tool_json_buffers: dict[int, str] = {}

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        chunk_type = chunk.get("type")

        if chunk_type == "message_start":
            msg = chunk.get("message", {})
            message_id = msg.get("id")
            role = msg.get("role", "assistant")
            if isinstance(msg.get("usage"), dict):
                usage.update(msg["usage"])

        elif chunk_type == "content_block_start":
            content_block = chunk.get("content_block", {})
            block_idx = chunk.get("index", 0)
            if isinstance(block_idx, int) and isinstance(content_block, dict):
                blocks[block_idx] = dict(content_block)
                if content_block.get("type") == "tool_use":
                    tool_json_buffers[block_idx] = ""

        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            block_idx = chunk.get("index", 0)
            delta_type = delta.get("type")
            block = blocks.setdefault(block_idx, {"type": "text", "text": ""})
            if delta_type == "text_delta":
                block["type"] = block.get("type") or "text"
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta_type == "thinking_delta":
                block["type"] = block.get("type") or "thinking"
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif delta_type == "signature_delta":
                block["signature"] = delta.get("signature", "")
            elif delta_type == "input_json_delta":
                tool_json_buffers[block_idx] = tool_json_buffers.get(block_idx, "") + delta.get("partial_json", "")

        elif chunk_type == "content_block_stop":
            block_idx = chunk.get("index", 0)
            if block_idx in tool_json_buffers and block_idx in blocks:
                buffer = tool_json_buffers[block_idx]
                try:
                    blocks[block_idx]["input"] = json.loads(buffer) if buffer else {}
                except json.JSONDecodeError:
                    # 上游 partial_json 拼装异常：保留空 input，避免把内部 buffer 写入日志/响应记录。
                    blocks[block_idx]["input"] = {}

        elif chunk_type == "message_delta":
            delta = chunk.get("delta", {})
            stop_reason = delta.get("stop_reason")
            stop_sequence = delta.get("stop_sequence")
            if isinstance(chunk.get("usage"), dict):
                usage.update(chunk["usage"])

    if not message_id:
        # 尝试从其他 chunk 中获取 id
        for chunk in chunks:
            if isinstance(chunk, dict) and chunk.get("id"):
                message_id = chunk["id"]
                break

    if not message_id:
        return None

    content_blocks = [blocks[idx] for idx in sorted(blocks)]

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    return {
        "id": message_id,
        "type": "message",
        "role": role,
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": usage,
    }


def _build_openai_stream_response(chunks: list[Any], model: str) -> dict | None:
    """构建 OpenAI 格式的流式响应体。"""
    response_id = None
    content_text = ""
    reasoning_text = ""
    finish_reason = None
    input_tokens = 0
    output_tokens = 0
    total_tokens: int | None = None
    prompt_details: dict | None = None
    completion_details: dict | None = None
    role = "assistant"
    # tool_calls 拼接：按 index 分组
    # 每个 tool call 结构: {id, type: "function", function: {name, arguments}}
    tool_calls_map: dict[int, dict] = {}

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        # 获取 id（通常在第一个 chunk）
        if not response_id and chunk.get("id"):
            response_id = chunk["id"]

        # 获取 role（某些实现可能在第一个 chunk 包含）
        if chunk.get("choices"):
            choice = chunk["choices"][0]
            if isinstance(choice, dict):
                delta = choice.get("delta", {})
                if delta.get("role"):
                    role = delta["role"]

        # 拼接内容
        choices = chunk.get("choices", [])
        if choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta", {})
            if delta and "content" in delta and delta["content"]:
                content_text += delta["content"]
            # 拼接 reasoning_content（如 DeepSeek 的思考内容）
            if delta and "reasoning_content" in delta and delta["reasoning_content"]:
                reasoning_text += delta["reasoning_content"]

            # 拼接 tool_calls
            tool_calls = delta.get("tool_calls") if delta else None
            if tool_calls:
                for tc in tool_calls:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }
                    tool_call = tool_calls_map[idx]
                    # 更新 id（第一个 chunk 可能有）
                    if tc.get("id"):
                        tool_call["id"] = tc["id"]
                    # 更新 type
                    if tc.get("type"):
                        tool_call["type"] = tc["type"]
                    # 拼接 function 字段
                    func = tc.get("function", {})
                    if func.get("name"):
                        tool_call["function"]["name"] = func["name"]
                    if func.get("arguments"):
                        tool_call["function"]["arguments"] += func["arguments"]

            # 获取 finish_reason
            fr = choices[0].get("finish_reason")
            if fr:
                finish_reason = fr

        # 获取 usage（可能在最后一个 chunk）
        usage = chunk.get("usage")
        if usage:
            input_tokens = usage.get("prompt_tokens", input_tokens)
            output_tokens = usage.get("completion_tokens", output_tokens)
            # 优先使用上游的 total_tokens
            if usage.get("total_tokens") is not None:
                total_tokens = usage["total_tokens"]
            # 透传 prompt_tokens_details
            pd = usage.get("prompt_tokens_details")
            if isinstance(pd, dict):
                prompt_details = pd
            # 透传 completion_tokens_details
            cd = usage.get("completion_tokens_details")
            if isinstance(cd, dict):
                completion_details = cd

    if not response_id:
        response_id = f"chatcmpl-{secrets.token_hex(12)}"

    message: dict = {
        "role": role,
        "content": content_text if content_text else None,
    }
    # 添加 reasoning_content（如有）
    if reasoning_text:
        message["reasoning_content"] = reasoning_text

    # 如果有 tool_calls，按 index 顺序添加到 message
    if tool_calls_map:
        tool_calls_list = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())]
        message["tool_calls"] = tool_calls_list

    # 构建 usage 字段
    final_usage: dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens if total_tokens is not None else input_tokens + output_tokens,
    }
    if prompt_details is not None:
        final_usage["prompt_tokens_details"] = prompt_details
    if completion_details is not None:
        final_usage["completion_tokens_details"] = completion_details

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": final_usage,
    }




_model_channels_cache: dict[str, list[Channel]] | None = None
_model_channels_cache_version = 0
_model_channels_lock = asyncio.Lock()
_model_channels_sync_lock = threading.Lock()  # 保护同步回调对全局变量的写操作
_background_tasks: set[asyncio.Task] = set()


async def _invalidate_model_channels_cache() -> None:
    global _model_channels_cache, _model_channels_cache_version
    async with _model_channels_lock:
        _model_channels_cache = None
        _model_channels_cache_version += 1
    data = await storage.load_data()
    active_ids = {ch.get("id") for ch in data.get("channels", [])}
    await load_balancer.cleanup_removed_channels(active_ids)


def _schedule_invalidate_model_channels_cache() -> None:
    global _model_channels_cache, _model_channels_cache_version
    with _model_channels_sync_lock:
        _model_channels_cache = None
        _model_channels_cache_version += 1
    try:
        task = asyncio.create_task(_cleanup_removed_channels_after_save())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        pass


register_save_callback(_schedule_invalidate_model_channels_cache)


async def _cleanup_removed_channels_after_save() -> None:
    data = await storage.load_data()
    active_ids = {ch.get("id") for ch in data.get("channels", [])}
    await load_balancer.cleanup_removed_channels(active_ids)


async def _get_channels_for_model(model: str) -> list[Channel]:
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
                    channel_id = raw_channel.get("id") if isinstance(raw_channel, dict) else None
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


CONVERTER_MAP: dict[tuple[str, str], tuple[type, type]] = {
    # key: (source=上游渠道格式, target=客户端入口格式)
    # value: (RequestConverter, ResponseConverter)
    # RequestConverter: 把客户端格式(target)转换为上游格式(source)
    # ResponseConverter: 把上游格式(source)转换为客户端格式(target)
    ("openai-chat-completions", "anthropic"): (ToChatCompletionsConverter, ToAnthropicConverter),
    ("openai-response", "anthropic"): (ToResponseConverter, ToAnthropicConverter),
    ("openai-response", "openai-chat-completions"): (ToResponseConverter, ToChatCompletionsConverter),
    ("anthropic", "openai-chat-completions"): (ToAnthropicConverter, ToChatCompletionsConverter),
    ("anthropic", "openai-response"): (ToAnthropicConverter, ToResponseConverter),
    ("openai-chat-completions", "openai-response"): (ToChatCompletionsConverter, ToResponseConverter),
}


def _filter_channels_by_conversion(
    channels: list[Channel], target_api_type: APIType
) -> list[Channel]:
    """按"是否允许跨格式转换"过滤渠道。

    同格式渠道（透传）始终通过。跨格式渠道按 channel.allow_format_conversion 决定，
    若该字段为 None 则回落到全局 settings.allow_format_conversion（默认 True）。
    """
    target = target_api_type.value
    global_allowed = get_setting("allow_format_conversion")
    if global_allowed is None:
        global_allowed = True
    result: list[Channel] = []
    for ch in channels:
        if ch.api_type.value == target:
            result.append(ch)
            continue
        allowed = ch.allow_format_conversion
        if allowed is None:
            allowed = global_allowed
        if allowed:
            result.append(ch)
    return result


def _get_converter_and_upstream_type(
    channel: Channel, target_api_type: APIType
) -> tuple:
    """根据渠道类型和目标API类型，获取转换器和上游请求类型

    返回 (request_converter, response_converter, source_type)
    - request_converter: 用于把客户端格式转换为上游格式
    - response_converter: 用于把上游格式转换为客户端格式
    """
    source = channel.api_type.value
    target = target_api_type.value

    if source == target:
        return None, None, source

    converters = CONVERTER_MAP.get((source, target))
    if converters is None:
        raise ValueError(f"不支持的转换方向: {source} -> {target}")
    req_cls, resp_cls = converters
    return req_cls(), resp_cls(), source


def _get_upstream_url(channel: Channel) -> str:
    return build_upstream_url(channel)


def _build_upstream_headers(
    channel: Channel,
    client_headers: dict[str, str] | None,
) -> dict:
    forwarded_headers = {}
    skip_headers = {
        "host", "authorization", "x-api-key", "content-type", "content-length",
        # hop-by-hop headers (RFC 2616 Section 13.5.1)
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade",
    }
    for key, val in (client_headers or {}).items():
        if key.lower() not in skip_headers:
            forwarded_headers[key] = val

    headers = get_upstream_headers(channel, forwarded_headers)
    headers["Content-Type"] = "application/json"
    return headers


_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
)


class ConverterError(Exception):
    """格式转换失败，允许外层故障转移到其他渠道。"""
    pass


class AllChannelsExhausted(Exception):
    """有可用渠道但全部因上游错误（429/5xx）不可用。

    携带 last_error 以便外层根据原始错误返回正确的 HTTP 状态码。
    """

    def __init__(self, message: str, last_error: BaseException | None = None):
        super().__init__(message)
        self.last_error = last_error


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, _UpstreamStreamErrorEvent):
        return True
    if isinstance(exc, _EmptyStreamError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code == 429 or 500 <= status_code < 600
    if isinstance(exc, ConverterError):
        return True
    return isinstance(exc, _RETRYABLE_EXCEPTIONS)


def _is_channel_config_error(exc: BaseException) -> bool:
    """检查是否为渠道配置错误（如认证失败、路径错误等）"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403, 404)
    return False


def _is_stream_terminal_event_missing(
    target_api_type: APIType,
    source_type: str,
    response_converter,
    stream_chunks: list[Any],
    done_received: bool,
) -> bool:
    if target_api_type == APIType.ANTHROPIC:
        if response_converter is not None:
            return True
        if source_type == "anthropic":
            return not any(
                isinstance(chunk, dict) and chunk.get("type") == "message_stop"
                for chunk in stream_chunks
            )
        return True
    if target_api_type != APIType.OPENAI_RESPONSE:
        return not done_received
    return False


def _response_input_to_items(input_data: Any) -> list[dict[str, Any]]:
    if input_data is None:
        return []
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if isinstance(input_data, list):
        items: list[dict[str, Any]] = []
        for item in input_data:
            if isinstance(item, str):
                items.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                items.append(dict(item))
        return items
    return [{"role": "user", "content": str(input_data)}]


async def _prepare_openai_response_request_for_upstream(
    request_data: dict[str, Any],
    source_type: str,
    target_api_type: APIType,
) -> dict[str, Any]:
    """展开 Responses 本地历史，仅用于不支持 Responses 状态的上游。"""
    previous_response_id = request_data.get("previous_response_id")
    if (
        target_api_type != APIType.OPENAI_RESPONSE
        or source_type == APIType.OPENAI_RESPONSE.value
        or not previous_response_id
    ):
        return request_data

    conversation = await _responses_store.get_conversation(previous_response_id)
    if conversation is None:
        raise ValueError(f"Response {previous_response_id} not found")

    prepared = dict(request_data)
    prepared["input"] = list(conversation.get("messages", [])) + _response_input_to_items(request_data.get("input"))
    if not prepared.get("instructions") and conversation.get("instructions"):
        prepared["instructions"] = conversation["instructions"]
    prepared.pop("previous_response_id", None)
    return prepared


class _StreamPreflightError(Exception):
    """流式响应首个输出前失败，允许外层故障转移。"""

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


class _UpstreamStreamErrorEvent(Exception):
    def __init__(self, event: dict[str, Any]):
        self.event = event
        error = event.get("error", {})
        super().__init__(error.get("message") or "upstream stream error")


class _EmptyStreamError(Exception):
    pass


async def _prime_stream(gen):
    """消费首个 chunk，让连接和首包前错误进入故障转移循环。

    如果上游连接成功但没有任何 SSE 输出（StopAsyncIteration），
    视为上游异常，触发故障转移而非返回空流。
    """
    try:
        first_chunk = await anext(gen)
    except StopAsyncIteration:
        raise _EmptyStreamError("上游流式响应为空，没有任何 SSE 输出") from None
    except _StreamPreflightError as exc:
        raise exc.original from exc

    async def _replay():
        try:
            yield first_chunk
            async for chunk in gen:
                yield chunk
        finally:
            await gen.aclose()

    return _replay()


async def proxy_request(
    model: str,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool = False,
    query_string: str | None = None,
    client_headers: dict[str, str] | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
) -> tuple[Any, Channel]:
    """
    执行代理请求，返回 (response_data_or_stream, selected_channel)
    自动进行负载均衡和故障转移

    支持模型组：如果 model 是模型组名，按 Fallback 顺序尝试组内模型
    """
    # 检查是否为模型组
    group = await storage.get_model_group_by_name(model)

    if group:
        # 模型组请求：按 Fallback 顺序尝试每个模型
        return await _proxy_model_group_request(
            group, request_data, target_api_type, is_stream,
            query_string, client_headers, api_key_id, client_ip
        )
    else:
        # 单模型请求：现有逻辑
        return await _proxy_single_model_request(
            model, request_data, target_api_type, is_stream,
            query_string, client_headers, api_key_id, client_ip
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
) -> tuple[Any, Channel]:
    """单模型请求，现有逻辑"""
    channels = await _get_channels_for_model(model)
    if not channels:
        raise ValueError(f"没有可用渠道支持模型: {model}")
    channels = _filter_channels_by_conversion(channels, target_api_type)
    if not channels:
        raise ValueError(
            f"模型 {model} 没有可用的同格式渠道（已禁止跨格式转换），"
            f"客户端格式={target_api_type.value}"
        )

    all_tried: set[str] = set()
    last_error: Exception | None = None

    while True:
        selected = await load_balancer.select_channel(
            channels,
            exclude_ids=all_tried,
            client_ip=client_ip,
            api_key_id=api_key_id,
            client_headers=client_headers,
        )
        if not selected:
            if last_error is not None:
                raise last_error
            raise AllChannelsExhausted(f"模型 {model} 的所有渠道均不可用")

        try:
            result = await _do_request(
                selected, request_data, target_api_type, is_stream,
                query_string=query_string, client_headers=client_headers,
                api_key_id=api_key_id,
                client_ip=client_ip,
            )
            if is_stream:
                result = await _prime_stream(result)
            return result, selected
        except Exception as e:
            if _is_retryable_exception(e):
                await load_balancer.record_failure(selected.id)
                last_error = e
                all_tried.add(selected.id)
            elif _is_channel_config_error(e):
                # 渠道配置错误（401/403/404）：记录失败并继续尝试其他渠道
                await load_balancer.record_failure(selected.id)
                last_error = e
                all_tried.add(selected.id)
            else:
                raise


async def _proxy_model_group_request(
    group,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None,
    client_headers: dict[str, str] | None,
    api_key_id: str | None,
    client_ip: str | None,
) -> tuple[Any, Channel]:
    """模型组请求：按 Fallback 顺序尝试每个模型"""
    # 按组内模型的 Fallback 顺序尝试
    tried_channels: set[str] = set()  # 所有已尝试的渠道
    last_error: Exception | None = None
    attempted_models: list[str] = []

    for current_model in group.models:
        if current_model not in attempted_models:
            attempted_models.append(current_model)
        channels = await _get_channels_for_model(current_model)
        if not channels:
            continue  # 该模型无渠道，尝试下一个模型
        channels = _filter_channels_by_conversion(channels, target_api_type)
        if not channels:
            continue  # 该模型的所有渠道都因禁止跨格式转换被排除，尝试下一个模型

        # 在该模型的渠道中尝试
        while True:
            selected = await load_balancer.select_channel(
                channels,
                exclude_ids=tried_channels,
                client_ip=client_ip,
                api_key_id=api_key_id,
                client_headers=client_headers,
            )
            if not selected:
                break  # 该模型所有渠道都试过了，切换下一个模型

            try:
                # 修改请求中的模型名
                modified_request = {**request_data, "model": current_model}
                result = await _do_request(
                    selected, modified_request, target_api_type, is_stream,
                    query_string=query_string, client_headers=client_headers,
                    api_key_id=api_key_id,
                    client_ip=client_ip,
                )
                if is_stream:
                    result = await _prime_stream(result)
                return result, selected
            except Exception as e:
                if _is_retryable_exception(e):
                    await load_balancer.record_failure(selected.id)
                    last_error = e
                    tried_channels.add(selected.id)
                elif _is_channel_config_error(e):
                    # 渠道配置错误（401/403/404）：记录失败并继续尝试其他渠道
                    await load_balancer.record_failure(selected.id)
                    last_error = e
                    tried_channels.add(selected.id)
                else:
                    raise

    # 所有模型的所有渠道都失败了
    attempted = ", ".join(attempted_models) or "none"
    if last_error is not None:
        raise AllChannelsExhausted(
            f"模型组 Fallback 已穷尽所有模型: group={group.name}, "
            f"attempted_models=[{attempted}], last_error={last_error}"
        ) from last_error
    raise AllChannelsExhausted(
        f"模型组 Fallback 已穷尽所有模型: group={group.name}, "
        f"attempted_models=[{attempted}], no available channels"
    )


def _ext_for_mime(mime_type: str) -> str:
    """根据 MIME 类型推断扩展名，去掉前导点。"""
    clean_mime = mime_type.split(";")[0].strip()
    ext = mimetypes.guess_extension(clean_mime) or ""
    return ext.lstrip(".")


def _extract_base64_data(part: dict) -> tuple[bytes, str] | None:
    """
    从多模态 content 块中提取 base64 数据与扩展名。

    支持：
    - OpenAI Chat image_url (data URL)
    - Anthropic image (source.base64)
    - OpenAI input_audio
    - OpenAI file (file.file_data)
    """
    part_type = part.get("type", "")

    # OpenAI Chat / Responses image_url
    if part_type == "image_url":
        image_url = part.get("image_url", {})
        if isinstance(image_url, dict):
            url = image_url.get("url", "")
            if isinstance(url, str) and url.startswith("data:"):
                header, _, b64 = url.partition(",")
                mime = "image/png"
                if ";" in header and ":" in header:
                    mime = header.split(";")[0].split(":", 1)[1]
                try:
                    return base64.b64decode(b64), _ext_for_mime(mime) or "png"
                except Exception:
                    return None

    # Anthropic image
    if part_type == "image":
        source = part.get("source", {})
        if isinstance(source, dict) and source.get("type") == "base64":
            mime = source.get("media_type", "image/png")
            data = source.get("data", "")
            try:
                return base64.b64decode(data), _ext_for_mime(mime) or "png"
            except Exception:
                return None

    # OpenAI input_audio
    if part_type == "input_audio":
        audio = part.get("input_audio", {})
        if isinstance(audio, dict):
            fmt = audio.get("format", "wav")
            data = audio.get("data", "")
            try:
                return base64.b64decode(data), fmt
            except Exception:
                return None

    # OpenAI file (file_data base64)
    if part_type == "file":
        file_info = part.get("file", {})
        if isinstance(file_info, dict):
            file_data = file_info.get("file_data")
            filename = file_info.get("filename", "file")
            if file_data:
                ext = ""
                if isinstance(filename, str):
                    ext = os.path.splitext(filename)[1].lstrip(".")
                try:
                    return base64.b64decode(file_data), ext or "bin"
                except Exception:
                    return None

    return None


async def _save_multimodal_files(request_data: dict, model_name: str, channel: Channel) -> None:
    """
    将请求中的多模态文件保存到 logs/{images,audios,files}/ 目录。

    保存行为由 settings 中的 save_images / save_audios / save_files 控制。
    写入失败会记录 warning 日志，不会阻断请求。
    """
    save_files = bool(get_setting("save_files"))
    save_images = bool(get_setting("save_images"))
    save_audios = bool(get_setting("save_audios"))
    if not (save_files or save_images or save_audios):
        return

    messages = request_data.get("messages")
    if not isinstance(messages, list):
        return

    logs_dir = Path("logs")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved_count = {"image": 0, "audio": 0, "file": 0}

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            category: str | None = None
            if part_type in ("image_url", "image"):
                if not save_images:
                    continue
                category = "images"
            elif part_type == "input_audio":
                if not save_audios:
                    continue
                category = "audios"
            elif part_type == "file":
                if not save_files:
                    continue
                category = "files"
            else:
                continue

            extracted = _extract_base64_data(part)
            if not extracted:
                continue
            data, ext = extracted
            if not ext:
                ext = {"images": "png", "audios": "wav", "files": "bin"}[category]

            file_hash = hashlib.sha256(data).hexdigest()[:8]
            safe_model = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in (model_name or "unknown")
            )
            filename = f"{timestamp}_{safe_model}_{file_hash}.{ext}"
            file_dir = logs_dir / category
            file_path = file_dir / filename

            try:
                await asyncio.to_thread(_write_media_file, file_dir, file_path, data)
                saved_count[{"images": "image", "audios": "audio", "files": "file"}[category]] += 1
            except Exception as e:
                logger.warning(f"[SAVE_MEDIA] 保存多模态文件失败: {file_path}: {e}")

    total = sum(saved_count.values())
    if total:
        logger.info(
            f"[SAVE_MEDIA] 已保存 {total} 个多模态文件: {saved_count} "
            f"渠道={channel.name} 模型={model_name}"
        )


def _write_media_file(file_dir: Path, file_path: Path, data: bytes) -> None:
    """同步写入文件，供 asyncio.to_thread 调用。"""
    file_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, file_path)


async def _do_request(
    channel: Channel,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None = None,
    client_headers: dict[str, str] | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
):
    upstream_data = request_data  # 兜底：若后续转换步骤抛异常，except 仍可安全引用
    request_converter, response_converter, source_type = _get_converter_and_upstream_type(channel, target_api_type)

    # 透传 OpenAI Chat 客户端的 stream_options.include_usage 到 response_converter
    if is_stream and isinstance(response_converter, ToChatCompletionsConverter):
        include_usage = bool((request_data.get("stream_options") or {}).get("include_usage", False))
        response_converter.set_stream_include_usage(include_usage)

    request_data = await _prepare_openai_response_request_for_upstream(
        request_data,
        source_type,
        target_api_type,
    )

    # 转换请求：客户端格式 → 上游格式
    if request_converter:
        try:
            upstream_data = request_converter.convert_request(request_data, target_api_type.value)
        except Exception as conv_err:
            logger.warning(f"请求转换失败: {type(conv_err).__name__}: {conv_err}")
            raise ConverterError(f"请求转换失败: {conv_err}") from conv_err
    else:
        upstream_data = request_data

    # === Capability 管理 ===
    # 必须在格式转换之后处理，因为能力过滤作用于实际发给上游的格式。
    # 能力描述的是上游真实约束（MiniMax 单 system、DeepSeek 不支持 parallel_tool_calls 等），
    # 与是否做格式转换无关 —— 同格式透传时也必须应用。
    model = upstream_data.get("model", "")

    # 保存请求中的多模态文件（在 capability 过滤前，保留原始内容）
    await _save_multimodal_files(upstream_data, model, channel)

    caps = infer_capabilities(channel, model)
    upstream_data = apply_capability_filter(upstream_data, caps, channel.name, model)

    # MiniMax 特殊处理：合并多条 system 消息
    if caps.requires_single_system_message and "messages" in upstream_data:
        original_count = len([m for m in upstream_data["messages"] if m.get("role") == "system"])
        upstream_data["messages"] = merge_system_messages(upstream_data["messages"])
        new_count = len([m for m in upstream_data["messages"] if m.get("role") == "system"])
        if original_count > 1:
            logger.debug(f"[CAPABILITY] MiniMax: 合并 {original_count} 条 system 消息为 {new_count} 条")

    need_think_filter = bool(caps.filter_think_content)

    url = _get_upstream_url(channel)
    if query_string:
        url = append_query(url, query_string)
    headers = _build_upstream_headers(
        channel,
        client_headers,
    )

    if is_stream:
        stream = _do_stream_request(
            channel, url, headers, upstream_data, response_converter, source_type, target_api_type,
            api_key_id=api_key_id,
            client_ip=client_ip,
            need_think_filter=need_think_filter,
        )
        return _raise_preflight_stream_errors(stream)

    # 非流式：使用缓存的 httpx 客户端（不可 async with，否则会关闭共享连接）
    request_start = time.time()  # 整体起点，create_client 失败时兜底
    upstream_start: float | None = None  # 上游请求起点（不含连接建立）
    try:
        client = await create_client(channel)
        upstream_start = time.time()
        resp = await client.post(url, json=upstream_data, headers=headers)
        resp.raise_for_status()
        response_data = resp.json()

        # 转换响应：上游格式 → 客户端格式
        if response_converter:
            try:
                response_data = response_converter.convert_response(response_data, source_type)
            except Exception as conv_err:
                logger.warning(f"响应转换失败: {type(conv_err).__name__}: {conv_err}")
                raise ConverterError(f"响应转换失败: {conv_err}") from conv_err

        latency_ms = int((time.time() - upstream_start) * 1000)

        # 过滤 💭 内容
        if need_think_filter:
            response_data = _filter_think_in_response(response_data)

        # 提取 token 使用量
        # 注意：某些 API（如 Kimi）的 input_tokens 可能为 0（表示缓存后），实际值在 prompt_tokens
        usage = response_data.get("usage", {}) if isinstance(response_data, dict) else {}
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
        token_details = cache_token_details(usage)
        # Anthropic 语义：input_tokens 不含 cache_creation/cache_read（三者互斥）
        # OpenAI Chat：prompt_tokens 含全部输入（cache 是子集）
        # OpenAI Response：input_tokens 含全部输入（cache 是子集）
        # 仅当纯 Anthropic 语义时归一化：加上缓存 token 使 input_tokens 表示总输入
        if "prompt_tokens" not in usage and "input_tokens_details" not in usage:
            input_tokens += token_details["cache_creation_input_tokens"] + token_details["cache_read_input_tokens"]

        # 提取 finish_reason
        finish_reason = None
        if isinstance(response_data, dict):
            choices = response_data.get("choices", [])
            if choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get("finish_reason")
            if finish_reason is None:
                finish_reason = response_data.get("stop_reason")

        # 记录统计（入队，由后台 worker 写入，不阻塞响应）
        _record_request(
            channel_id=channel.id,
            channel_name=channel.name,
            model=model,
            is_stream=False,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=token_details["cache_read_input_tokens"],
            cache_creation_input_tokens=token_details["cache_creation_input_tokens"],
            latency_ms=latency_ms,
            lag_ms=None,
            success=True,
            finish_reason=finish_reason,
            api_key_id=api_key_id,
            client_ip=client_ip,
            request_headers={k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")},
            response_headers=dict(resp.headers),
            request_body=upstream_data,
            response_body=response_data,
        )

        # 非流式响应摘要日志（兼容 Anthropic / Chat / Response 三种形状）
        if isinstance(response_data, dict):
            summary: list[str] = []
            stop_label: str | None = None

            # Anthropic: 顶层 content 数组
            content = response_data.get("content")
            if isinstance(content, list) and content:
                for c in content:
                    if not isinstance(c, dict):
                        summary.append("?")
                        continue
                    if c.get("type") == "text":
                        summary.append(f'text({len(c.get("text", ""))}chars)')
                    elif c.get("type") == "tool_use":
                        summary.append(f'tool_use({c.get("name", "")})')
                    else:
                        summary.append(c.get("type", "?"))
                stop_label = response_data.get("stop_reason")

            # OpenAI Chat: choices[].message
            else:
                choices = response_data.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
                    msg_content = msg.get("content") if isinstance(msg, dict) else None
                    if isinstance(msg_content, str) and msg_content:
                        summary.append(f"text({len(msg_content)}chars)")
                    tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if isinstance(tc, dict):
                                func = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                                summary.append(f'tool_use({func.get("name", "")})')
                    stop_label = choices[0].get("finish_reason")
                else:
                    # OpenAI Response: output[].content[]
                    output = response_data.get("output")
                    if isinstance(output, list):
                        for item in output:
                            if not isinstance(item, dict):
                                continue
                            item_type = item.get("type")
                            if item_type == "message":
                                for part in item.get("content", []) or []:
                                    if not isinstance(part, dict):
                                        continue
                                    if part.get("type") == "output_text":
                                        summary.append(f'text({len(part.get("text", ""))}chars)')
                                    else:
                                        summary.append(part.get("type", "?"))
                            elif item_type == "function_call":
                                summary.append(f'tool_use({item.get("name", "")})')
                            elif item_type:
                                summary.append(item_type)
                    stop_label = response_data.get("status") or response_data.get("stop_reason")

            if summary:
                logger.debug(f"content: [{', '.join(summary)}]")
            logger.debug(f"stop_reason: {stop_label or '?'}")

        await load_balancer.record_success(channel.id)
        return response_data
    except Exception as e:
        # upstream_start 为 None 表示 create_client 失败，此时用 request_start 兜底
        latency_ms = int((time.time() - (upstream_start or request_start)) * 1000)
        # 记录失败统计（入队，由后台 worker 写入，不阻塞响应）
        _record_request(
            channel_id=channel.id,
            channel_name=channel.name,
            model=model,
            is_stream=False,
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            lag_ms=None,
            success=False,
            error_msg=str(e),
            finish_reason=None,
            api_key_id=api_key_id,
            client_ip=client_ip,
            request_headers={k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")},
            request_body=upstream_data,
        )
        # 控制台输出详细错误
        err_body = ""
        if isinstance(e, httpx.HTTPStatusError):
            err_body = e.response.text[:500]
            logger.error(f"upstream {e.response.status_code} {url}")
            logger.error(f"body: {err_body}")
        else:
            logger.error(f"upstream {type(e).__name__}: {e}")

        raise


def _yield_anthropic_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _convert_anthropic_response_to_events(converted: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    message_for_start = {k: v for k, v in converted.items() if k not in ("stop_reason", "stop_sequence")}
    message_for_start["usage"] = {"input_tokens": 0, "output_tokens": 0}
    events.append(("message_start", {"message": message_for_start}))

    for i, block in enumerate(converted.get("content", [])):
        block_type = block.get("type", "text")
        if block_type == "thinking":
            events.append(("content_block_start", {"index": i, "content_block": {"type": "thinking", "thinking": ""}}))
            events.append(("content_block_delta", {"index": i, "delta": {"type": "thinking_delta", "thinking": block.get("thinking", "")}}))
        elif block_type == "tool_use":
            events.append(("content_block_start", {"index": i, "content_block": {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""), "input": ""}}))
            events.append(("content_block_delta", {"index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False)}}))
        else:
            events.append(("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": block.get("text", "")}}))
        events.append(("content_block_stop", {"index": i}))

    usage = converted.get("usage", {})
    events.append(("message_delta", {"delta": {"stop_reason": converted.get("stop_reason", "end_turn")}, "usage": {"output_tokens": usage.get("output_tokens", 0)}}))
    events.append(("message_stop", {}))
    return events


def _convert_non_stream_to_stream_events(
    full_response: dict[str, Any], response_converter, source_type: str, output_responses_sse: bool,
) -> list[str]:
    if not output_responses_sse:
        return []
    if response_converter is not None:
        converted = response_converter.convert_response(full_response, source_type)
    else:
        converted = full_response
    return _build_responses_stream_events_from_object(converted)


def _build_responses_stream_events_from_object(converted: dict[str, Any]) -> list[str]:
    """把一个 Response 形态的完整对象拆成 Responses SSE 事件序列。"""
    events: list[str] = []
    events.append(_format_sse_for_list({"type": "response.created", "response": converted}))
    for idx, item in enumerate(converted.get("output", [])):
        events.append(_format_sse_for_list({"type": "response.output_item.added", "output_index": idx, "item": item}))
        if item.get("type") == "message":
            for part_idx, part in enumerate(item.get("content", [])):
                if part.get("type") == "output_text":
                    events.append(_format_sse_for_list({"type": "response.content_part.added", "output_index": idx, "content_index": part_idx, "part": {"type": "output_text", "text": ""}}))
                    text = part.get("text", "")
                    if text:
                        events.append(_format_sse_for_list({"type": "response.output_text.delta", "output_index": idx, "content_index": part_idx, "delta": text}))
                    events.append(_format_sse_for_list({"type": "response.content_part.done", "output_index": idx, "content_index": part_idx, "part": part}))
        events.append(_format_sse_for_list({"type": "response.output_item.done", "output_index": idx, "item": item}))
    status = converted.get("status", "completed")
    events.append(_format_sse_for_list({"type": "response.completed", "response": {**converted, "status": status}}))
    return events


def _build_chat_stream_chunks_from_object(full_response: dict[str, Any], model: str) -> list[dict[str, Any]]:
    """把一个 Chat Completion 完整对象拆成 chat.completion.chunk 列表（不含 [DONE]）。

    用于上游对 stream=true 仍返回整块 JSON 的兜底场景，避免直接吐整块对象破坏流式协议。
    """
    response_id = full_response.get("id") or f"chatcmpl-{secrets.token_hex(12)}"
    created = full_response.get("created") or int(time.time())
    resp_model = full_response.get("model") or model
    chunks: list[dict[str, Any]] = []
    choices = full_response.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return chunks

    for ch_idx, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
        role = message.get("role", "assistant")
        content = message.get("content")
        reasoning_content = message.get("reasoning_content")
        tool_calls = message.get("tool_calls")
        finish_reason = choice.get("finish_reason")

        # 首帧：role 头
        chunks.append({
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": resp_model,
            "choices": [{"index": ch_idx, "delta": {"role": role}, "finish_reason": None}],
        })

        if isinstance(reasoning_content, str) and reasoning_content:
            chunks.append({
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": resp_model,
                "choices": [{"index": ch_idx, "delta": {"reasoning_content": reasoning_content}, "finish_reason": None}],
            })

        if isinstance(content, str) and content:
            chunks.append({
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": resp_model,
                "choices": [{"index": ch_idx, "delta": {"content": content}, "finish_reason": None}],
            })

        if isinstance(tool_calls, list) and tool_calls:
            tc_delta = []
            for tc_idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                tc_delta.append({
                    "index": tc_idx,
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                    },
                })
            if tc_delta:
                chunks.append({
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resp_model,
                    "choices": [{"index": ch_idx, "delta": {"tool_calls": tc_delta}, "finish_reason": None}],
                })

        # 末帧：finish_reason
        chunks.append({
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": resp_model,
            "choices": [{"index": ch_idx, "delta": {}, "finish_reason": finish_reason}],
        })

    usage = full_response.get("usage")
    if isinstance(usage, dict):
        chunks.append({
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": resp_model,
            "choices": [],
            "usage": usage,
        })

    return chunks


def _format_sse_for_list(data: dict[str, Any]) -> str:
    event_type = data.get("type") if isinstance(data, dict) else None
    if event_type:
        return _yield_anthropic_event(event_type, data)
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _iter_sse_blocks(lines, coalesce_data_lines: bool = True):
    event_type = None
    data_lines = []
    passthrough_lines = []

    async for line in lines:
        if not line.strip():
            if event_type or data_lines or passthrough_lines:
                yield event_type, data_lines, passthrough_lines
            event_type = None
            data_lines = []
            passthrough_lines = []
            continue

        if line.startswith("event:") and (event_type or data_lines or passthrough_lines):
            yield event_type, data_lines, passthrough_lines
            event_type = None
            data_lines = []
            passthrough_lines = []

        if not coalesce_data_lines and line.startswith("data:") and data_lines:
            yield event_type, data_lines, passthrough_lines
            event_type = None
            data_lines = []
            passthrough_lines = []

        if line.startswith(":"):
            passthrough_lines.append(line)
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        else:
            passthrough_lines.append(line)

    if event_type or data_lines or passthrough_lines:
        yield event_type, data_lines, passthrough_lines


def _format_raw_sse(event_type: str | None, data: str) -> str:
    lines = []
    if event_type:
        lines.append(f"event: {event_type}")
    for data_line in data.splitlines() or [""]:
        lines.append(f"data: {data_line}")
    return "\n".join(lines) + "\n\n"


async def _do_stream_request(
    channel: Channel, url: str, headers: dict, upstream_data: dict, response_converter, source_type: str,
    target_api_type: APIType = APIType.OPENAI_CHAT, api_key_id: str | None = None,
    client_ip: str | None = None,
    need_think_filter: bool = False,
):
    """流式请求，yield SSE 数据行。

    当 target_api_type 为 ANTHROPIC 时，输出 Anthropic SSE 格式
    （包含 event: 行）。否则输出 OpenAI SSE 格式（仅 data: 行）。
    response_converter: 用于把上游格式转换为客户端格式
    need_think_filter: 是否过滤 💭 内容
    """
    start_time = time.time()
    first_token_time = None
    model = upstream_data.get("model", "")
    input_tokens = 0
    output_tokens = 0
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    finish_reason = None
    stream_chunks: list[Any] = []
    stream_chunk_count = 0
    _stream_log_enabled = LOG_LEVEL == "debug"
    _stream_log_count = 0  # 流式事件日志计数器
    _STREAM_LOG_MAX = 20   # 最多记录前 20 个事件

    # 创建 ThinkFilter 实例用于流式过滤
    think_filter = ThinkFilter() if need_think_filter else None

    def _log_stream_event(sse_data: str):
        """记录流式 SSE 事件（仅 debug 级别，限流：最多 _STREAM_LOG_MAX 条）"""
        if not _stream_log_enabled:
            return
        nonlocal _stream_log_count
        if _stream_log_count >= _STREAM_LOG_MAX:
            return
        _stream_log_count += 1
        # 提取 event type 和关键信息
        lines = sse_data.strip().split("\n")
        evt_type = ""
        data_summary = ""
        for ln in lines:
            if ln.startswith("event: "):
                evt_type = ln[7:]
            elif ln.startswith("data: "):
                try:
                    d = json.loads(ln[6:])
                    # 关键字段摘要
                    if d.get("type") == "content_block_start":
                        cb = d.get("content_block", {})
                        data_summary = f'cb_start({cb.get("type","")}{"," + cb.get("name","") if cb.get("name") else ""})'
                    elif d.get("type") == "content_block_delta":
                        delta = d.get("delta", {})
                        dtype = delta.get("type", "")
                        if dtype == "text_delta":
                            data_summary = f'text({len(delta.get("text",""))}chars)'
                        elif dtype == "input_json_delta":
                            data_summary = f'json({delta.get("partial_json","")})'
                        elif dtype == "thinking_delta":
                            data_summary = f'thinking({len(delta.get("thinking",""))}chars)'
                        else:
                            data_summary = dtype
                    elif d.get("type") == "content_block_stop":
                        data_summary = f'cb_stop(idx={d.get("index","")})'
                    elif d.get("type") == "message_start":
                        data_summary = f'id={d.get("message",{}).get("id","")}'
                    elif d.get("type") == "message_delta":
                        data_summary = f'stop={d.get("delta",{}).get("stop_reason","")}'
                    else:
                        data_summary = d.get("type", str(d)[:80])
                except json.JSONDecodeError:
                    data_summary = ln[6:][:80]
        if evt_type:
            logger.debug(f"{evt_type}: {data_summary}")
        elif data_summary:
            logger.debug(f"data: {data_summary}")

    def _record_chunk(item: Any):
        """记录stream chunk，超过限制后停止记录"""
        nonlocal stream_chunk_count
        if stream_chunk_count < MAX_STREAM_CHUNKS:
            stream_chunks.append(item)
            stream_chunk_count += 1
    resp_status_code = None
    resp_headers = None
    client = create_stream_client(channel)
    output_anthropic_sse = target_api_type == APIType.ANTHROPIC
    output_responses_sse = target_api_type == APIType.OPENAI_RESPONSE
    output_sse_events = output_anthropic_sse or output_responses_sse
    is_upstream_anthropic = source_type == "anthropic"
    is_upstream_event_sse = is_upstream_anthropic or source_type == "openai-response"

    stream_success = False
    stream_error = None
    emitted_output = False
    failure_recorded = False
    cancelled = False
    logger.debug(f"[STREAM START] model={model} url={url} target={target_api_type.value}")
    try:
        async with client.stream("POST", url, json=upstream_data, headers=headers) as resp:
            if resp.is_error:
                await resp.aread()
            resp.raise_for_status()
            resp_status_code = resp.status_code
            resp_headers = dict(resp.headers)
            logger.debug(f"[STREAM CONNECTED] status={resp_status_code} headers={resp_headers}")

            upstream_event_type = None

            def _mark_first_token():
                nonlocal first_token_time
                if first_token_time is None:
                    first_token_time = time.time()

            def _mark_output():
                nonlocal emitted_output
                emitted_output = True

            def _format_sse(data: dict, event_type: str | None = None) -> str:
                try:
                    if event_type:
                        return _yield_anthropic_event(event_type, data)
                    if output_responses_sse and isinstance(data, dict) and data.get("type"):
                        return _yield_anthropic_event(data["type"], data)
                    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except Exception as sse_err:
                    logger.error(f"[FORMAT SSE ERROR] {type(sse_err).__name__}: {sse_err} data={data}")
                    logger.exception("[FORMAT SSE ERROR TRACEBACK]")
                    raise

            def _yield_extra_events(converted: dict):
                extra = response_converter.get_extra_events(converted)
                logger.debug(f"[_yield_extra_events] got {len(extra)} extra events")
                return _format_extra_events(extra)

            def _format_extra_events(extra):
                if not extra:
                    return []
                results = []
                try:
                    if output_anthropic_sse:
                        for extra_evt in extra:
                            if isinstance(extra_evt, tuple) and len(extra_evt) == 2:
                                evt_type, evt_data = extra_evt
                                sse = _yield_anthropic_event(evt_type, evt_data)
                            else:
                                sse = _format_sse(extra_evt)
                            _log_stream_event(sse)
                            results.append(sse)
                    else:
                        for extra_evt in extra:
                            sse = _format_sse(extra_evt)
                            _log_stream_event(sse)
                            results.append(sse)
                except Exception as format_err:
                    logger.error(f"[FORMAT EXTRA ERROR] {type(format_err).__name__}: {format_err}")
                    logger.exception("[FORMAT EXTRA ERROR TRACEBACK]")
                    raise
                return results

            non_sse_stream_body = None
            _first_line_checked = False
            _line_count = 0
            _done_received = False

            def _terminal_events_for_error() -> list[str]:
                if not _is_stream_terminal_event_missing(
                    target_api_type,
                    source_type,
                    response_converter,
                    stream_chunks,
                    _done_received,
                ):
                    return []
                if output_anthropic_sse:
                    return [_yield_anthropic_event("message_stop", {"type": "message_stop"})]
                if not output_sse_events:
                    return ["data: [DONE]\n\n"]
                return []

            async for upstream_event_type, data_lines, passthrough_lines in _iter_sse_blocks(
                resp.aiter_lines(),
                coalesce_data_lines=is_upstream_event_sse,
            ):
                _line_count += len(data_lines) + len(passthrough_lines) + (1 if upstream_event_type else 0)
                if data_lines:
                    _first_line_checked = True
                    data_str = "\n".join(data_lines)
                else:
                    if not _first_line_checked:
                        # 检查是否为 SSE 注释/心跳行（以 : 开头）
                        has_sse_comments = any(line.strip().startswith(":") for line in passthrough_lines)
                        if has_sse_comments:
                            # SSE 注释/心跳行，直接透传并继续读取
                            passthrough = "\n".join(passthrough_lines) + "\n\n"
                            _mark_output()
                            yield passthrough
                            continue
                        # 非 SSE 内容（如原始 JSON），设置为 non_sse_stream_body 用于后续处理
                        non_sse_stream_body = "\n".join(passthrough_lines)
                        break
                    if response_converter:
                        continue
                    passthrough = "\n".join(passthrough_lines) + "\n\n"
                    _mark_output()
                    yield passthrough
                    continue

                if data_str.strip() == "[DONE]":
                    _record_chunk("[DONE]")
                    _done_received = True
                    stream_success = True
                    logger.debug(f"[STREAM DONE] model={model} lines={_line_count} chunks={len(stream_chunks)}")
                    try:
                        # 输出 ThinkFilter 残余内容
                        if think_filter:
                            remaining = think_filter.flush()
                            if remaining:
                                # 构造一个 delta 事件输出残余内容
                                if output_responses_sse:
                                    remaining_evt = {"type": "response.output_text.delta", "delta": remaining}
                                    sse = _format_sse(remaining_evt)
                                    _log_stream_event(sse)
                                    _mark_output()
                                    yield sse
                                else:
                                    # Chat Completions 格式
                                    remaining_chunk = {
                                        "id": "chatcmpl-stream",
                                        "object": "chat.completion.chunk",
                                        "created": 0,
                                        "model": model,
                                        "choices": [{"index": 0, "delta": {"content": remaining}, "finish_reason": None}]
                                    }
                                    sse = _format_sse(remaining_chunk)
                                    _log_stream_event(sse)
                                    _mark_output()
                                    yield sse

                        if response_converter:
                            final_events = response_converter.finalize_stream(source_type)
                            logger.debug(f"[STREAM FINALIZE] model={model} events={len(final_events)}")
                            for final_event in _format_extra_events(final_events):
                                _mark_output()
                                yield final_event
                            extra_events = _yield_extra_events({})
                            logger.debug(f"[STREAM EXTRA] model={model} events={len(extra_events)}")
                            for extra_sse in extra_events:
                                _mark_output()
                                yield extra_sse
                        if not output_sse_events:
                            _mark_output()
                            yield "data: [DONE]\n\n"
                    except Exception as done_err:
                        logger.error(f"[STREAM DONE ERROR] model={model} error={type(done_err).__name__}: {done_err}")
                        logger.exception("[STREAM DONE ERROR TRACEBACK]")
                        raise
                    continue

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    _record_chunk(data_str)
                    _mark_first_token()
                    if response_converter:
                        raise ConverterError(f"流式 chunk 不是有效 JSON: {data_str[:120]}") from None
                    sse = _format_raw_sse(upstream_event_type, data_str)
                    _log_stream_event(sse)
                    _mark_output()
                    yield sse
                    continue

                _record_chunk(chunk)
                _mark_first_token()

                upstream_error_detected = False
                if isinstance(chunk, dict):
                    if is_upstream_anthropic and (
                        upstream_event_type == "error" or chunk.get("type") == "error"
                    ):
                        upstream_error_detected = True
                    elif source_type == "openai-response" and (
                        upstream_event_type == "error"
                        or chunk.get("type") == "error"
                        or chunk.get("type") == "response.failed"
                    ):
                        upstream_error_detected = True
                    elif source_type == "openai-chat-completions" and isinstance(chunk.get("error"), dict):
                        upstream_error_detected = True

                if upstream_error_detected:
                    upstream_error = _UpstreamStreamErrorEvent(chunk)
                    if not emitted_output:
                        raise _StreamPreflightError(upstream_error)
                    stream_error = str(upstream_error)

                # 增量提取 token 用量和 finish_reason，避免 finally 中二次遍历
                if isinstance(chunk, dict):
                    if is_upstream_anthropic:
                        if chunk.get("type") == "message_start":
                            start_usage = chunk.get("message", {}).get("usage", {})
                            # 某些第三方代理用 prompt_tokens 代替 input_tokens
                            input_tokens = start_usage.get("input_tokens", 0)
                            if input_tokens == 0:
                                # prompt_tokens 回退：值已含 cache，不额外加
                                input_tokens = start_usage.get("prompt_tokens", 0)
                            else:
                                # Anthropic 语义：input_tokens 不含 cache，归一化为总输入
                                input_tokens += start_usage.get("cache_creation_input_tokens", 0) + start_usage.get("cache_read_input_tokens", 0)
                            token_details = cache_token_details(start_usage)
                            cache_read_input_tokens = token_details["cache_read_input_tokens"]
                            cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
                        elif chunk.get("type") == "message_delta":
                            delta_usage = chunk.get("usage", {})
                            output_tokens = delta_usage.get("output_tokens", output_tokens)
                            # 标准 API 的 message_delta 只有 output_tokens，但第三方代理可能附带 input_tokens
                            if input_tokens == 0:
                                input_tokens = delta_usage.get("input_tokens", 0)
                                if input_tokens == 0:
                                    # prompt_tokens 回退：值已含 cache，不额外加
                                    input_tokens = delta_usage.get("prompt_tokens", 0)
                                else:
                                    # Anthropic 语义：input_tokens 不含 cache，归一化为总输入
                                    input_tokens += cache_read_input_tokens + cache_creation_input_tokens
                            token_details = cache_token_details(delta_usage)
                            if token_details["cache_read_input_tokens"] or token_details["cache_creation_input_tokens"]:
                                cache_read_input_tokens = token_details["cache_read_input_tokens"]
                                cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
                            fr = chunk.get("delta", {}).get("stop_reason")
                            if fr:
                                finish_reason = fr
                    else:
                        usage = chunk.get("usage")
                        if usage:
                            logger.info(f"[STREAM USAGE] upstream returned usage: {usage}")
                            input_tokens = usage.get("prompt_tokens", input_tokens)
                            output_tokens = usage.get("completion_tokens", output_tokens)
                            token_details = cache_token_details(usage)
                            cache_read_input_tokens = token_details["cache_read_input_tokens"]
                            cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
                        choices = chunk.get("choices", [])
                        if choices and isinstance(choices[0], dict):
                            fr = choices[0].get("finish_reason")
                            if fr:
                                finish_reason = fr

                if response_converter:
                    if is_upstream_anthropic and upstream_event_type is not None:
                        chunk = {**chunk, "_event_type": upstream_event_type}
                    try:
                        converted = response_converter.convert_stream_chunk(chunk, source_type)
                    except Exception as conv_err:
                        logger.warning(f"流式 chunk 转换失败: {type(conv_err).__name__}: {conv_err}")
                        raise ConverterError(f"流式 chunk 转换失败: {conv_err}") from conv_err
                    logger.debug(f"[CONVERT_CHUNK] converted={converted is not None} type={converted.get('type') if converted else None}")
                    if converted is not None:
                        # 应用 ThinkFilter 过滤思考内容
                        if think_filter and isinstance(converted, dict):
                            converted = _filter_think_in_stream_chunk(converted, think_filter)
                        if converted is not None:
                            evt_type = response_converter.get_stream_event_type(chunk, source_type) if output_anthropic_sse else None
                            sse = _format_sse(converted, evt_type)
                            _log_stream_event(sse)
                            _mark_output()
                            yield sse
                    for extra_sse in _yield_extra_events(chunk):
                        _mark_output()
                        yield extra_sse
                else:
                    # 同格式透传：think 过滤仍需生效。
                    # Anthropic 协议的思考走 type: thinking 块，不存在 💭...💭 标记，跳过过滤。
                    if think_filter and not is_upstream_anthropic and isinstance(chunk, dict):
                        chunk = _filter_think_in_stream_chunk(chunk, think_filter)
                        if chunk is None:
                            continue
                    if output_sse_events and is_upstream_event_sse and upstream_event_type:
                        sse = _format_sse(chunk, upstream_event_type)
                    else:
                        sse = _format_sse(chunk)
                    if passthrough_lines:
                        _sse_lines = []
                        for _ln in sse.split("\n"):
                            if _ln.startswith("event: "):
                                _sse_lines.append(_ln)
                        _sse_lines.extend(passthrough_lines)
                        for _ln in sse.split("\n"):
                            if _ln.startswith("data:"):
                                _sse_lines.append(_ln)
                        sse = "\n".join(_sse_lines) + "\n\n"
                    _log_stream_event(sse)
                    _mark_output()
                    yield sse
                    if stream_error:
                        for terminal_sse in _terminal_events_for_error():
                            _log_stream_event(terminal_sse)
                            _mark_output()
                            yield terminal_sse
                        break

        logger.debug(f"[STREAM LOOP END] model={model} lines={_line_count} done={_done_received}")
        logger.debug(f"[STREAM ASYNC WITH EXIT] model={model}")
        # 尽早标记成功：流循环已正常结束，所有 chunks 已处理。
        # 放在后续 yield 点之前，避免 GeneratorExit 导致 success 未设置。
        if stream_error is None:
            stream_success = True
        # 上游关闭连接但未发送 [DONE] 时，补发终止事件避免客户端挂起。
        # 正常流结束路径：仅在非错误、非非SSE-body 场景下执行。
        if stream_error is None and not _done_received and non_sse_stream_body is None:
            if emitted_output:
                logger.warning(
                    f"[STREAM EOF WITHOUT DONE] model={model} "
                    f"chunks={len(stream_chunks)} target={target_api_type.value}"
                )
                # 1. 刷新 ThinkFilter 残余内容
                if think_filter:
                    remaining = think_filter.flush()
                    if remaining:
                        if output_responses_sse:
                            remaining_evt = {"type": "response.output_text.delta", "delta": remaining}
                            sse = _format_sse(remaining_evt)
                            _log_stream_event(sse)
                            _mark_output()
                            yield sse
                        else:
                            remaining_chunk = {
                                "id": "chatcmpl-stream",
                                "object": "chat.completion.chunk",
                                "created": 0,
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": remaining}, "finish_reason": None}]
                            }
                            sse = _format_sse(remaining_chunk)
                            _log_stream_event(sse)
                            _mark_output()
                            yield sse
                # 2. 调用 converter finalize 补发协议终止事件
                if response_converter:
                    final_events = response_converter.finalize_stream(source_type)
                    logger.debug(f"[STREAM EOF FINALIZE] model={model} events={len(final_events)}")
                    for final_event in _format_extra_events(final_events):
                        _mark_output()
                        yield final_event
                    extra_events = _yield_extra_events({})
                    for extra_sse in extra_events:
                        _mark_output()
                        yield extra_sse
                # 3. Chat Completions 补发 data: [DONE]
                if not output_sse_events:
                    _mark_output()
                    yield "data: [DONE]\n\n"
        if non_sse_stream_body is not None:
            logger.debug(f"[STREAM NON-SSE] model={model} body_length={len(non_sse_stream_body)}")
            try:
                full_response = json.loads(non_sse_stream_body)
            except json.JSONDecodeError:
                full_response = None
                logger.warning(f"[STREAM NON-SSE JSON ERROR] model={model}")
            if isinstance(full_response, dict):
                logger.warning(f"[STREAM NON-SSE] upstream returned non-SSE JSON for streaming request (object={full_response.get('object')}), converting to stream events")
                _record_chunk(full_response)
                _mark_first_token()
                if output_anthropic_sse:
                    if response_converter:
                        converted = response_converter.convert_response(full_response, source_type)
                        for evt_type, evt_data in _convert_anthropic_response_to_events(converted):
                            sse = _yield_anthropic_event(evt_type, evt_data)
                            _log_stream_event(sse)
                            _mark_output()
                            yield sse
                    else:
                        # 同类型 Anthropic 直通：非 SSE JSON 也需拆分为
                        # message_start / content_block_start / ... / message_stop 事件序列
                        for evt_type, evt_data in _convert_anthropic_response_to_events(full_response):
                            sse = _yield_anthropic_event(evt_type, evt_data)
                            _log_stream_event(sse)
                            _mark_output()
                            yield sse
                elif output_responses_sse:
                    # Response→Response 透传或跨格式都走拆事件，避免裸吐整块 JSON。
                    stream_events = _convert_non_stream_to_stream_events(
                        full_response, response_converter, source_type, output_responses_sse,
                    )
                    for sse in stream_events:
                        _log_stream_event(sse)
                        _mark_output()
                        yield sse
                else:
                    # Chat→Chat 透传：把整块 chat.completion 拆成 chat.completion.chunk 序列，
                    # 避免客户端拿到非流式形态破坏 SSE 协议。
                    chat_chunks = _build_chat_stream_chunks_from_object(full_response, model)
                    if chat_chunks:
                        for chunk_obj in chat_chunks:
                            # 同格式透传：think 过滤仍需生效（对齐 1611-1616 流式分支）。
                            # Anthropic 上游用 type: thinking 块，不存在 💭 标记，跳过过滤。
                            if think_filter and not is_upstream_anthropic:
                                chunk_obj = _filter_think_in_stream_chunk(chunk_obj, think_filter)
                                if chunk_obj is None:
                                    continue
                            sse = _format_sse(chunk_obj)
                            _log_stream_event(sse)
                            _mark_output()
                            yield sse
                    else:
                        sse = _format_sse(full_response)
                        _log_stream_event(sse)
                        _mark_output()
                        yield sse
                if not output_sse_events:
                    _mark_output()
                    yield "data: [DONE]\n\n"
                full_usage = full_response.get("usage", {})
                input_tokens = full_usage.get("prompt_tokens", full_usage.get("input_tokens", input_tokens))
                output_tokens = full_usage.get("completion_tokens", full_usage.get("output_tokens", output_tokens))
                token_details = cache_token_details(full_usage)
                # 非 SSE JSON 的 Anthropic 响应：input_tokens 不含 cache，归一化为总输入
                if "prompt_tokens" not in full_usage and "input_tokens_details" not in full_usage:
                    input_tokens += token_details["cache_creation_input_tokens"] + token_details["cache_read_input_tokens"]
                cache_read_input_tokens = token_details["cache_read_input_tokens"]
                cache_creation_input_tokens = token_details["cache_creation_input_tokens"]
                choices = full_response.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                stop_reason = full_response.get("stop_reason")
                if stop_reason:
                    finish_reason = stop_reason
            else:
                stream_error = "non_sse_json_parse_error"
                logger.error(f"[STREAM NON-SSE PARSE FAILED] model={model} body={non_sse_stream_body[:200]}")
                if output_anthropic_sse:
                    yield _yield_anthropic_event("error", {
                        "type": "error",
                        "error": {"type": "api_error", "message": "Upstream returned unparseable response"},
                    })
                    yield _yield_anthropic_event("message_stop", {"type": "message_stop"})
                elif output_responses_sse:
                    yield _yield_anthropic_event("error", {
                        "type": "error",
                        "error": {"message": "Upstream returned unparseable response", "type": "api_error"},
                    })
                    yield _yield_anthropic_event("response.failed", {
                        "type": "response.failed",
                        "response": {"id": "", "object": "response", "status": "failed", "model": model, "output": [], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}},
                    })
                else:
                    yield f'data: {json.dumps({"error": {"message": "Upstream returned unparseable response", "type": "api_error"}}, ensure_ascii=False)}\n\n'
                if not output_sse_events:
                    yield "data: [DONE]\n\n"

        if stream_error is None:
            stream_success = True
        logger.debug(f"[STREAM FINISH] model={model} done_received={_done_received} non_sse_body={'yes' if non_sse_stream_body else 'no'} chunks={len(stream_chunks)}")
        logger.debug(f"[STREAM COMPLETE] model={model} chunks={len(stream_chunks)} input_tokens={input_tokens} output_tokens={output_tokens} finish_reason={finish_reason}")
    except asyncio.CancelledError:
        cancelled = True
        if not emitted_output:
            stream_error = "client_disconnected_before_first_chunk"
        else:
            stream_error = "client_disconnected_mid_stream"
        logger.warning(
            f"[STREAM CANCELLED] model={model} emitted={emitted_output} "
            f"chunks={len(stream_chunks)} first_token={first_token_time is not None} "
            f"error={stream_error}"
        )
        raise
    except Exception as e:
        stream_error = str(e)
        err_body = ""
        logger.error(f"[STREAM ERROR TRACEBACK] model={model} url={url}")
        logger.exception("[STREAM ERROR]")
        if isinstance(e, httpx.HTTPStatusError):
            try:
                await e.response.aread()
                err_body = e.response.text[:500]
            except Exception:
                pass
            logger.error(f"[STREAM ERROR] upstream {e.response.status_code} {url} body={err_body}")
        else:
            logger.error(f"[STREAM ERROR] {type(e).__name__}: {e} model={model} url={url}")

        if not emitted_output:
            if _is_retryable_exception(e) or _is_channel_config_error(e):
                raise _StreamPreflightError(e) from e
            raise
        await load_balancer.record_failure(channel.id)
        failure_recorded = True
        if output_anthropic_sse:
            emitted_output = True
            yield _yield_anthropic_event("error", {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            })
            yield _yield_anthropic_event("message_stop", {"type": "message_stop"})
        elif output_responses_sse:
            error_data = {"type": "error", "error": {"message": f"流式传输错误: {e}", "type": "api_error"}}
            emitted_output = True
            yield _yield_anthropic_event("error", error_data)
            # 发送 response.failed 事件以便客户端正确识别流结束
            failed_data = {
                "type": "response.failed",
                "response": {
                    "id": "",
                    "object": "response",
                    "status": "failed",
                    "model": model,
                    "output": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            }
            emitted_output = True
            yield _yield_anthropic_event("response.failed", failed_data)
        else:
            error_data = {"error": {"message": f"流式传输错误: {e}", "type": "api_error"}}
            emitted_output = True
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        if not output_sse_events:
            emitted_output = True
            yield "data: [DONE]\n\n"
    finally:
        # 1. 构建响应体用于记录
        response_body = None
        try:
            latency_ms = int((time.time() - start_time) * 1000)
            lag_ms = None
            if first_token_time is not None:
                lag_ms = int((first_token_time - start_time) * 1000)
            response_body = _build_stream_response_body(
                chunks=stream_chunks,
                is_upstream_anthropic=is_upstream_anthropic,
                model=model,
            )
        except Exception as build_err:
            logger.warning(f"stream build response body error: {build_err}")

        # 2. 记录请求日志
        try:
            logger.debug(f"[STREAM STATS] model={model} success={stream_success} error={stream_error} latency={latency_ms}ms lag={lag_ms}ms chunks={len(stream_chunks)} input={input_tokens} output={output_tokens} finish={finish_reason}")
            _record_request(
                channel_id=channel.id,
                channel_name=channel.name,
                model=model,
                is_stream=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                latency_ms=latency_ms,
                lag_ms=lag_ms,
                success=stream_success,
                error_msg=stream_error,
                finish_reason=finish_reason,
                api_key_id=api_key_id,
                client_ip=client_ip,
                request_headers={k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")},
                response_headers=resp_headers,
                request_body=upstream_data,
                response_body=response_body,
            )
        except Exception as record_err:
            logger.warning(f"stream record request error: {record_err}")

        # 3. 记录负载均衡状态
        try:
            if stream_success:
                await load_balancer.record_success(channel.id)
                logger.debug(f"[STREAM RECORDED SUCCESS] channel={channel.name}")
            else:
                if stream_error and emitted_output and not failure_recorded and not cancelled:
                    await load_balancer.record_failure(channel.id)
                if cancelled:
                    logger.warning(f"[STREAM RECORDED CANCELLED] channel={channel.name} error={stream_error}")
                else:
                    logger.warning(f"[STREAM RECORDED FAILURE] channel={channel.name} error={stream_error}")
        except Exception as lb_err:
            logger.warning(f"stream load balancer record error: {lb_err}")

        # 4. 关闭流式客户端
        try:
            await client.aclose()
        except Exception as e:
            logger.warning(f"close stream client error: {e}")

async def _raise_preflight_stream_errors(gen):
    has_yielded = False
    try:
        async for chunk in gen:
            has_yielded = True
            yield chunk
    except Exception as exc:
        if not has_yielded and (_is_retryable_exception(exc) or _is_channel_config_error(exc)):
            raise _StreamPreflightError(exc) from exc
        raise
    finally:
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            await aclose()
