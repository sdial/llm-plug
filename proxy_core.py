"""
通用代理逻辑，供三个代理路由共用
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

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
from config import LOG_LEVEL
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from response_state import get_responses_store
from storage import register_save_callback
from think_filter import ThinkFilter, filter_think_content_static

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
                    blocks[block_idx]["input"] = {}
                    blocks[block_idx]["_partial_json"] = buffer

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
            if delta and "tool_calls" in delta:
                for tc in delta["tool_calls"]:
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

    if not response_id:
        response_id = f"chatcmpl-{model[:8]}"

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

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }




_model_channels_cache: dict[str, list[Channel]] | None = None
_model_channels_lock = asyncio.Lock()


async def _invalidate_model_channels_cache() -> None:
    global _model_channels_cache
    async with _model_channels_lock:
        _model_channels_cache = None
    data = await storage.load_data()
    active_ids = {ch.get("id") for ch in data.get("channels", [])}
    await load_balancer.cleanup_removed_channels(active_ids)


def _schedule_invalidate_model_channels_cache() -> None:
    try:
        asyncio.create_task(_invalidate_model_channels_cache())
    except RuntimeError:
        pass


register_save_callback(_schedule_invalidate_model_channels_cache)


async def _get_channels_for_model(model: str) -> list[Channel]:
    global _model_channels_cache
    cache = _model_channels_cache
    if cache is not None:
        return cache.get(model, [])

    async with _model_channels_lock:
        cache = _model_channels_cache
        if cache is not None:
            return cache.get(model, [])
        data = await storage.load_data()
        channels = [Channel(**ch) for ch in data.get("channels", [])]
        _model_channels_cache = {}
        for ch in channels:
            if not ch.enabled:
                continue
            for m in ch.models:
                _model_channels_cache.setdefault(m, []).append(ch)
        return _model_channels_cache.get(model, [])


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
    base = channel.base_url.rstrip("/")
    actual_type = channel.api_type.value

    if actual_type == "openai-chat-completions":
        return _append_api_path(base, "/chat/completions")
    elif actual_type == "openai-response":
        return _append_api_path(base, "/responses")
    elif actual_type == "anthropic":
        return _append_api_path(base, "/messages")
    return base


def _append_api_path(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.endswith(path):
        return base
    # 检查单复数形式: /chat/completion vs /chat/completions
    if path.endswith("s") and base.endswith(path[:-1]):
        return base
    if base.endswith("/v1"):
        return f"{base}{path}"
    return f"{base}/v1{path}"


def _build_upstream_headers(
    channel: Channel,
    target_api_type: APIType,
    same_type_passthrough: bool,
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
    httpx.PoolTimeout,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
)


class ConverterError(Exception):
    """格式转换失败，允许外层故障转移到其他渠道。"""
    pass


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, _UpstreamStreamErrorEvent):
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


async def _prime_stream(gen):
    """消费首个 chunk，让连接和首包前错误进入故障转移循环。

    如果上游连接成功但没有任何 SSE 输出（StopAsyncIteration），
    视为上游异常，触发故障转移而非返回空流。
    """
    try:
        first_chunk = await anext(gen)
    except StopAsyncIteration:
        raise RuntimeError("上游流式响应为空，没有任何 SSE 输出") from None
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
            query_string, client_headers, api_key_id
        )
    else:
        # 单模型请求：现有逻辑
        return await _proxy_single_model_request(
            model, request_data, target_api_type, is_stream,
            query_string, client_headers, api_key_id
        )


async def _proxy_single_model_request(
    model: str,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None,
    client_headers: dict[str, str] | None,
    api_key_id: str | None,
) -> tuple[Any, Channel]:
    """单模型请求，现有逻辑"""
    channels = await _get_channels_for_model(model)
    if not channels:
        raise ValueError(f"没有可用渠道支持模型: {model}")

    all_tried: set[str] = set()
    last_error: Exception | None = None

    while True:
        selected = await load_balancer.select_channel(channels, exclude_ids=all_tried)
        if not selected:
            if last_error is not None:
                raise last_error
            raise ValueError(f"模型 {model} 的所有渠道均不可用")

        try:
            result = await _do_request(
                selected, request_data, target_api_type, is_stream,
                query_string=query_string, client_headers=client_headers,
                api_key_id=api_key_id,
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
) -> tuple[Any, Channel]:
    """模型组请求：按 Fallback 顺序尝试每个模型"""
    # 按组内模型的 Fallback 顺序尝试
    tried_channels: set[str] = set()  # 所有已尝试的渠道
    last_error: Exception | None = None

    for current_model in group.models:
        channels = await _get_channels_for_model(current_model)
        if not channels:
            continue  # 该模型无渠道，尝试下一个模型

        # 在该模型的渠道中尝试
        while True:
            selected = await load_balancer.select_channel(channels, exclude_ids=tried_channels)
            if not selected:
                break  # 该模型所有渠道都试过了，切换下一个模型

            try:
                # 修改请求中的模型名
                modified_request = {**request_data, "model": current_model}
                result = await _do_request(
                    selected, modified_request, target_api_type, is_stream,
                    query_string=query_string, client_headers=client_headers,
                    api_key_id=api_key_id,
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
    if last_error is not None:
        raise last_error
    raise ValueError(f"模型组 {group.name} 的所有渠道均不可用")


async def _do_request(
    channel: Channel,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None = None,
    client_headers: dict[str, str] | None = None,
    api_key_id: str | None = None,
):
    request_converter, response_converter, source_type = _get_converter_and_upstream_type(channel, target_api_type)
    same_type_passthrough = request_converter is None and response_converter is None
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
    apply_compatibility_filters = not same_type_passthrough
    caps = infer_capabilities(channel) if apply_compatibility_filters else None
    if caps is not None:
        upstream_data = apply_capability_filter(upstream_data, caps)

    # MiniMax 特殊处理：合并多条 system 消息
    if caps is not None and caps.requires_single_system_message:
        if "messages" in upstream_data:
            original_count = len([m for m in upstream_data["messages"] if m.get("role") == "system"])
            upstream_data["messages"] = merge_system_messages(upstream_data["messages"])
            new_count = len([m for m in upstream_data["messages"] if m.get("role") == "system"])
            if original_count > 1:
                logger.debug(f"[CAPABILITY] MiniMax: 合并 {original_count} 条 system 消息为 {new_count} 条")

    need_think_filter = bool(caps and caps.filter_think_content)

    url = _get_upstream_url(channel)
    if query_string:
        url = f"{url}?{query_string}"
    headers = _build_upstream_headers(
        channel,
        target_api_type,
        same_type_passthrough,
        client_headers,
    )

    if is_stream:
        stream = _do_stream_request(
            channel, url, headers, upstream_data, response_converter, source_type, target_api_type,
            api_key_id=api_key_id,
            need_think_filter=need_think_filter,
        )
        return _raise_preflight_stream_errors(stream)

    # 非流式：使用缓存的 httpx 客户端（不可 async with，否则会关闭共享连接）
    model = request_data.get("model", "")
    request_start = time.time()  # 整体起点，create_client 失败时兜底
    upstream_start: float | None = None  # 上游请求起点（不含连接建立）
    try:
        client = await create_client(channel)
        upstream_start = time.time()
        resp = await client.post(url, json=upstream_data, headers=headers)
        resp.raise_for_status()
        response_data = resp.json()
        latency_ms = int((time.time() - upstream_start) * 1000)

        # 转换响应：上游格式 → 客户端格式
        if response_converter:
            try:
                response_data = response_converter.convert_response(response_data, source_type)
            except Exception as conv_err:
                logger.warning(f"响应转换失败: {type(conv_err).__name__}: {conv_err}")
                raise ConverterError(f"响应转换失败: {conv_err}") from conv_err

        # 过滤 💭 内容
        if need_think_filter:
            response_data = _filter_think_in_response(response_data)

        # 提取 token 使用量
        # 注意：某些 API（如 Kimi）的 input_tokens 可能为 0（表示缓存后），实际值在 prompt_tokens
        usage = response_data.get("usage", {}) if isinstance(response_data, dict) else {}
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))

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
            latency_ms=latency_ms,
            lag_ms=None,
            success=True,
            finish_reason=finish_reason,
            api_key_id=api_key_id,
            request_headers={k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")},
            response_headers=dict(resp.headers),
            request_body=upstream_data,
            response_body=response_data,
        )

        # 非流式响应摘要日志
        content = response_data.get("content", []) if isinstance(response_data, dict) else []
        if content:
            summary = []
            for c in content:
                if c.get("type") == "text":
                    txt = c.get("text", "")
                    summary.append(f'text({len(txt)}chars)')
                elif c.get("type") == "tool_use":
                    summary.append(f'tool_use({c.get("name", "")})')
                else:
                    summary.append(c.get("type", "?"))
            logger.debug(f"content: [{', '.join(summary)}]")
        logger.debug(f"stop_reason: {response_data.get('stop_reason', '?') if isinstance(response_data, dict) else '?'}")

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
    if output_responses_sse:
        converted = response_converter.convert_response(full_response, source_type)
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
    return []


def _format_sse_for_list(data: dict[str, Any]) -> str:
    if data.get("type"):
        return _yield_anthropic_event(data["type"], data)
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
    logger.debug(f"[STREAM START] model={model} url={url} target={target_api_type.value}")
    try:
        async with client.stream("POST", url, json=upstream_data, headers=headers) as resp:
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
                    import traceback
                    logger.error(traceback.format_exc())
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
                    import traceback
                    logger.error(traceback.format_exc())
                    raise
                return results

            nonlocal_stream_body = None
            _first_line_checked = False
            _line_count = 0
            _done_received = False

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
                        # 非 SSE 内容（如原始 JSON），设置为 nonlocal_stream_body 用于后续处理
                        nonlocal_stream_body = "\n".join(passthrough_lines)
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
                        import traceback
                        logger.error(traceback.format_exc())
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

                if isinstance(chunk, dict) and is_upstream_anthropic and (
                    upstream_event_type == "error" or chunk.get("type") == "error"
                ):
                    upstream_error = _UpstreamStreamErrorEvent(chunk)
                    if not emitted_output:
                        raise _StreamPreflightError(upstream_error)
                    stream_error = str(upstream_error)

                # 增量提取 token 用量和 finish_reason，避免 finally 中二次遍历
                if isinstance(chunk, dict):
                    if is_upstream_anthropic:
                        if chunk.get("type") == "message_start":
                            input_tokens = chunk.get("message", {}).get("usage", {}).get("input_tokens", 0)
                        elif chunk.get("type") == "message_delta":
                            output_tokens = chunk.get("usage", {}).get("output_tokens", output_tokens)
                            fr = chunk.get("delta", {}).get("stop_reason")
                            if fr:
                                finish_reason = fr
                    else:
                        usage = chunk.get("usage")
                        if usage:
                            logger.info(f"[STREAM USAGE] upstream returned usage: {usage}")
                            input_tokens = usage.get("prompt_tokens", input_tokens)
                            output_tokens = usage.get("completion_tokens", output_tokens)
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
                    if output_sse_events and is_upstream_event_sse and upstream_event_type:
                        sse = _format_sse(chunk, upstream_event_type or "ping")
                    else:
                        sse = _format_sse(chunk)
                    if passthrough_lines:
                        sse = "\n".join(passthrough_lines) + "\n" + sse
                    _log_stream_event(sse)
                    _mark_output()
                    yield sse

        logger.debug(f"[STREAM LOOP END] model={model} lines={_line_count} done={_done_received}")
        logger.debug(f"[STREAM ASYNC WITH EXIT] model={model}")
        if nonlocal_stream_body is not None:
            logger.debug(f"[STREAM NON-SSE] model={model} body_length={len(nonlocal_stream_body)}")
            try:
                full_response = json.loads(nonlocal_stream_body)
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
                    if response_converter:
                        stream_events = _convert_non_stream_to_stream_events(
                            full_response, response_converter, source_type, output_responses_sse,
                        )
                        for sse in stream_events:
                            _log_stream_event(sse)
                            _mark_output()
                            yield sse
                    else:
                        sse = _format_sse(full_response)
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
                input_tokens = full_response.get("usage", {}).get("prompt_tokens", full_response.get("usage", {}).get("input_tokens", input_tokens))
                output_tokens = full_response.get("usage", {}).get("completion_tokens", full_response.get("usage", {}).get("output_tokens", output_tokens))
                choices = full_response.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                stop_reason = full_response.get("stop_reason")
                if stop_reason:
                    finish_reason = stop_reason

        logger.debug(f"[STREAM FINISH] model={model} done_received={_done_received} nonlocal_body={'yes' if nonlocal_stream_body else 'no'} chunks={len(stream_chunks)}")
        if stream_error is None:
            stream_success = True
        logger.debug(f"[STREAM COMPLETE] model={model} chunks={len(stream_chunks)} input_tokens={input_tokens} output_tokens={output_tokens} finish_reason={finish_reason}")
    except Exception as e:
        stream_error = str(e)
        err_body = ""
        import traceback
        logger.error(f"[STREAM ERROR TRACEBACK] model={model} url={url}")
        logger.error(traceback.format_exc())
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
            logger.debug(f"[STREAM STATS] model={model} success={stream_success} error={stream_error} latency={latency_ms}ms lag={lag_ms}ms chunks={len(stream_chunks)} input={input_tokens} output={output_tokens} finish={finish_reason}")
            _record_request(
                channel_id=channel.id,
                channel_name=channel.name,
                model=model,
                is_stream=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                lag_ms=lag_ms,
                success=stream_success,
                error_msg=stream_error,
                finish_reason=finish_reason,
                api_key_id=api_key_id,
                request_headers={k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")},
                response_headers=resp_headers,
                request_body=upstream_data,
                response_body=response_body,
            )
            if stream_success:
                await load_balancer.record_success(channel.id)
                logger.debug(f"[STREAM RECORDED SUCCESS] channel={channel.name}")
            else:
                if stream_error and emitted_output and not failure_recorded:
                    await load_balancer.record_failure(channel.id)
                logger.warning(f"[STREAM RECORDED FAILURE] channel={channel.name} error={stream_error}")
        except Exception as finally_err:
            logger.warning(f"stream finally error: {finally_err}")
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
