"""
通用代理逻辑，供三个代理路由共用
"""
import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any

from loguru import logger

import httpx

from balancer.load_balancer import load_balancer
from client import create_client, get_or_create_stream_client, get_upstream_headers
from config import DEBUG, DEBUG_LOG_DIR, LOG_LEVEL
import storage
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
import stats
from storage import register_save_callback

# 流式响应最大记录chunk数量，防止内存溢出
MAX_STREAM_CHUNKS = 2000


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
    content_text = ""
    stop_reason = None
    input_tokens = 0
    output_tokens = 0

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        chunk_type = chunk.get("type")

        if chunk_type == "message_start":
            msg = chunk.get("message", {})
            message_id = msg.get("id")
            role = msg.get("role", "assistant")
            usage = msg.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)

        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                content_text += delta.get("text", "")

        elif chunk_type == "message_delta":
            delta = chunk.get("delta", {})
            stop_reason = delta.get("stop_reason")
            usage = chunk.get("usage", {})
            output_tokens = usage.get("output_tokens", output_tokens)

    if not message_id:
        # 尝试从其他 chunk 中获取 id
        for chunk in chunks:
            if isinstance(chunk, dict) and chunk.get("id"):
                message_id = chunk["id"]
                break

    if not message_id:
        return None

    return {
        "id": message_id,
        "type": "message",
        "role": role,
        "content": [{"type": "text", "text": content_text}],
        "model": model,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _build_openai_stream_response(chunks: list[Any], model: str) -> dict | None:
    """构建 OpenAI 格式的流式响应体。"""
    response_id = None
    content_text = ""
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


def _write_log_line(log_file: str, line: str) -> None:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)


async def _log_debug(
    channel: Channel,
    upstream_url: str,
    upstream_data: dict,
    upstream_headers: dict,
    response_data: Any = None,
    is_stream: bool = False,
    stream_content: Any = None,
    response_headers: dict | None = None,
    status_code: int | None = None,
    error: str | None = None,
):
    """记录 debug 日志（包含完整 request + response）"""
    if not DEBUG:
        return
    try:
        os.makedirs(DEBUG_LOG_DIR, exist_ok=True)

        today = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(DEBUG_LOG_DIR, f"debug_{today}.jsonl")

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

        if is_stream:
            if stream_content:
                if isinstance(stream_content, list) and len(stream_content) > 100:
                    log_entry["response"]["stream_chunks_count"] = len(stream_content)
                    log_entry["response"]["stream_content_sample"] = stream_content[:10] + stream_content[-10:]
                else:
                    log_entry["response"]["stream_content"] = stream_content
        else:
            log_entry["response"]["data"] = response_data

        if error:
            log_entry["error"] = error

        line = json.dumps(log_entry, ensure_ascii=False) + "\n"
        await asyncio.to_thread(_write_log_line, log_file, line)
    except Exception as log_err:
        logger.warning(f"Failed to write debug log: {log_err}")


_model_channels_cache: dict[str, list[Channel]] | None = None
_model_channels_lock = asyncio.Lock()


async def _invalidate_model_channels_cache() -> None:
    global _model_channels_cache
    async with _model_channels_lock:
        _model_channels_cache = None
    data = await storage.load_data()
    active_ids = {ch.get("id") for ch in data.get("channels", [])}
    load_balancer.cleanup_removed_channels(active_ids)


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

    # 检查 base_url 是否已经包含完整的 API 路径
    # 如果已包含，则不再拼接
    if actual_type == "openai-chat-completions":
        # 检查是否已包含 chat/completions 或类似路径
        if base.endswith("/chat/completions") or base.endswith("/chat/completion"):
            return base
        return f"{base}/v1/chat/completions"
    elif actual_type == "openai-response":
        if base.endswith("/responses"):
            return base
        return f"{base}/v1/responses"
    elif actual_type == "anthropic":
        if base.endswith("/messages"):
            return base
        return f"{base}/v1/messages"
    return base


_RETRYABLE_EXCEPTIONS = (
    httpx.HTTPStatusError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
)


def _fire_and_forget(coro):
    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning(f"fire-and-forget task failed: {type(exc).__name__}: {exc}")

    task = asyncio.create_task(coro)
    task.add_done_callback(_on_done)
    return task


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
            return await _do_request(
                selected, request_data, target_api_type, is_stream,
                query_string=query_string, client_headers=client_headers,
                api_key_id=api_key_id,
            ), selected
        except _RETRYABLE_EXCEPTIONS as e:
            load_balancer.record_failure(selected.id)
            last_error = e
            all_tried.add(selected.id)


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
                return await _do_request(
                    selected, modified_request, target_api_type, is_stream,
                    query_string=query_string, client_headers=client_headers,
                    api_key_id=api_key_id,
                ), selected
            except _RETRYABLE_EXCEPTIONS as e:
                load_balancer.record_failure(selected.id)
                last_error = e
                tried_channels.add(selected.id)

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

    # 转换请求：客户端格式 → 上游格式
    if request_converter:
        upstream_data = request_converter.convert_request(request_data, target_api_type.value)
    else:
        upstream_data = request_data

    url = _get_upstream_url(channel)
    if query_string:
        url = f"{url}?{query_string}"
    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    # 透传客户端 header（排除 host 和认证相关）
    _SKIP_HEADERS = {"host", "authorization", "x-api-key", "content-type", "content-length"}
    if client_headers:
        for key, val in client_headers.items():
            if key.lower() not in _SKIP_HEADERS:
                headers[key] = val

    if is_stream:
        # OpenAI 流式请求需要注入 stream_options 以获取 usage 信息
        if source_type != "anthropic" and isinstance(upstream_data, dict):
            upstream_data = {
                **upstream_data,
                "stream_options": {"include_usage": True}
            }
        return _do_stream_request(
            channel, url, headers, upstream_data, response_converter, source_type, target_api_type,
            api_key_id=api_key_id,
        )

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

        # 记录 debug 日志（包含完整响应和响应头）
        await _log_debug(
            channel=channel,
            upstream_url=url,
            upstream_data=upstream_data,
            upstream_headers=headers,
            response_data=response_data,
            is_stream=False,
            response_headers=dict(resp.headers),
            status_code=resp.status_code,
        )

        # 转换响应：上游格式 → 客户端格式
        if response_converter:
            response_data = response_converter.convert_response(response_data, source_type)

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

        # 记录统计（后台执行，不阻塞响应）
        _fire_and_forget(stats.record_request(
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
        ))

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

        load_balancer.record_success(channel.id)
        return response_data
    except Exception as e:
        # upstream_start 为 None 表示 create_client 失败，此时用 request_start 兜底
        latency_ms = int((time.time() - (upstream_start or request_start)) * 1000)
        # 记录失败统计（后台执行，不阻塞响应）
        _fire_and_forget(stats.record_request(
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
        ))
        # 控制台输出详细错误
        err_body = ""
        if isinstance(e, httpx.HTTPStatusError):
            err_body = e.response.text[:500]
            logger.error(f"upstream {e.response.status_code} {url}")
            logger.error(f"body: {err_body}")
        else:
            logger.error(f"upstream {type(e).__name__}: {e}")

        await _log_debug(
            channel=channel,
            upstream_url=url,
            upstream_data=upstream_data,
            upstream_headers=headers,
            error=str(e),
        )
        raise


def _yield_anthropic_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _yield_anthropic_events(events: list[tuple[str, dict[str, Any]] | dict[str, Any]]) -> str:
    parts = []
    for evt in events:
        if isinstance(evt, tuple) and len(evt) == 2:
            et, d = evt
            parts.append(f"event: {et}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n")
        elif isinstance(evt, dict):
            parts.append(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n")
    return "".join(parts)


async def _do_stream_request(
    channel: Channel, url: str, headers: dict, upstream_data: dict, response_converter, source_type: str,
    target_api_type: APIType = APIType.OPENAI_CHAT, api_key_id: str | None = None,
):
    """流式请求，yield SSE 数据行。

    当 target_api_type 为 ANTHROPIC 时，输出 Anthropic SSE 格式
    （包含 event: 行）。否则输出 OpenAI SSE 格式（仅 data: 行）。
    response_converter: 用于把上游格式转换为客户端格式
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
    client = await get_or_create_stream_client(channel)
    output_anthropic_sse = target_api_type == APIType.ANTHROPIC
    output_responses_sse = target_api_type == APIType.OPENAI_RESPONSE
    output_sse_events = output_anthropic_sse or output_responses_sse
    is_upstream_anthropic = source_type == "anthropic"

    stream_success = False
    stream_error = None
    try:
        async with client.stream("POST", url, json=upstream_data, headers=headers) as resp:
            resp.raise_for_status()
            resp_status_code = resp.status_code
            resp_headers = dict(resp.headers)

            upstream_event_type = None

            def _mark_first_token():
                nonlocal first_token_time
                if first_token_time is None:
                    first_token_time = time.time()

            def _format_sse(data: dict, event_type: str | None = None) -> str:
                if event_type:
                    return _yield_anthropic_event(event_type, data)
                if output_responses_sse and isinstance(data, dict) and data.get("type"):
                    return _yield_anthropic_event(data["type"], data)
                return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            def _yield_extra_events(converted: dict):
                extra = response_converter.get_extra_events(converted)
                if not extra:
                    return []
                results = []
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
                return results

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                if is_upstream_anthropic and line.startswith("event:"):
                    raw_type = line[5:].strip()
                    if raw_type.startswith(":"):
                        raw_type = raw_type[1:].strip()
                    upstream_event_type = raw_type
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                elif line.startswith("data:"):
                    data_str = line[5:]
                else:
                    _record_chunk(line)
                    continue

                if data_str.strip() == "[DONE]":
                    _record_chunk("[DONE]")
                    if not output_sse_events:
                        yield "data: [DONE]\n\n"
                    continue

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    _record_chunk(line)
                    _mark_first_token()
                    yield line + "\n\n"
                    continue

                _record_chunk(chunk)
                _mark_first_token()

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

                if is_upstream_anthropic and upstream_event_type is not None:
                    chunk["_event_type"] = upstream_event_type

                if response_converter:
                    converted = response_converter.convert_stream_chunk(chunk, source_type)
                    if converted is not None:
                        evt_type = response_converter.get_stream_event_type(chunk, source_type) if output_anthropic_sse else None
                        sse = _format_sse(converted, evt_type)
                        _log_stream_event(sse)
                        yield sse
                        for extra_sse in _yield_extra_events(converted):
                            yield extra_sse
                else:
                    if output_anthropic_sse and is_upstream_anthropic:
                        sse = _format_sse(chunk, upstream_event_type or "ping")
                    else:
                        sse = _format_sse(chunk)
                    _log_stream_event(sse)
                    yield sse

                if is_upstream_anthropic:
                    upstream_event_type = None

        stream_success = True
        await _log_debug(
            channel=channel, upstream_url=url, upstream_data=upstream_data,
            upstream_headers=headers, is_stream=True, stream_content=stream_chunks,
            response_headers=resp_headers, status_code=resp_status_code,
        )
    except Exception as e:
        stream_error = str(e)
        load_balancer.record_failure(channel.id)
        err_body = ""
        if isinstance(e, httpx.HTTPStatusError):
            try:
                await e.response.aread()
                err_body = e.response.text[:500]
            except Exception:
                pass
            logger.error(f"upstream stream {e.response.status_code} {url}")
            logger.error(f"body: {err_body}")
        else:
            logger.error(f"upstream stream {type(e).__name__}: {e}")

        await _log_debug(
            channel=channel, upstream_url=url, upstream_data=upstream_data,
            upstream_headers=headers, is_stream=True, stream_content=stream_chunks,
            response_headers=resp_headers, status_code=resp_status_code, error=str(e),
        )
        if output_anthropic_sse:
            yield _yield_anthropic_event("error", {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            })
        elif output_responses_sse:
            error_data = {"type": "error", "error": {"message": f"流式传输错误: {e}", "type": "api_error"}}
            yield _yield_anthropic_event("error", error_data)
        else:
            error_data = {"error": {"message": f"流式传输错误: {e}", "type": "api_error"}}
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        if not output_sse_events:
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
            _fire_and_forget(stats.record_request(
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
            ))
            if stream_success:
                load_balancer.record_success(channel.id)
        except Exception as finally_err:
            logger.warning(f"stream finally error: {finally_err}")
