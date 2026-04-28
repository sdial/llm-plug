"""
通用代理逻辑，供三个代理路由共用
"""
import json
import os
from datetime import datetime
from typing import Any

from balancer.load_balancer import load_balancer
from client import create_client, create_stream_client, get_upstream_headers
from config import DEBUG, DEBUG_LOG_DIR
from converters.to_anthropic import ToAnthropicConverter
from converters.to_chat import ToChatCompletionsConverter
from converters.to_response import ToResponseConverter
from models.api_types import APIType
from models.channel import Channel
from storage import load_data

# 流式响应最大记录chunk数量，防止内存溢出
MAX_STREAM_CHUNKS = 10000


def _log_debug(
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


def _get_upstream_url(channel: Channel) -> str:
    base = channel.base_url.rstrip("/")
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
    query_string: str | None = None,
) -> tuple[Any, Channel]:
    """
    执行代理请求，返回 (response_data_or_stream, selected_channel)
    自动进行负载均衡和故障转移
    """
    channels = _get_channels_for_model(model)
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
            return await _do_request(selected, request_data, target_api_type, is_stream, query_string=query_string), selected
        except Exception as e:
            load_balancer.record_failure(selected.id)
            last_error = e
            all_tried.add(selected.id)


async def _do_request(
    channel: Channel,
    request_data: dict[str, Any],
    target_api_type: APIType,
    is_stream: bool,
    query_string: str | None = None,
):
    converter, source_type = _get_converter_and_upstream_type(channel, target_api_type)

    # 转换请求
    if converter:
        upstream_data = converter.convert_request(request_data, source_type)
    else:
        upstream_data = request_data

    url = _get_upstream_url(channel)
    if query_string:
        url = f"{url}?{query_string}"
    headers = get_upstream_headers(channel)
    headers["Content-Type"] = "application/json"

    if is_stream:
        return _do_stream_request(
            channel, url, headers, upstream_data, converter, source_type, target_api_type
        )

    # 非流式：使用缓存的 httpx 客户端（不可 async with，否则会关闭共享连接）
    try:
        client = create_client(channel)
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
    channel: Channel, url: str, headers: dict, upstream_data: dict, converter, source_type: str,
    target_api_type: APIType = APIType.OPENAI_CHAT,
):
    """流式请求，yield SSE 数据行。

    当 target_api_type 为 ANTHROPIC 时，输出 Anthropic SSE 格式
    （包含 event: 行）。否则输出 OpenAI SSE 格式（仅 data: 行）。
    """
    stream_chunks: list[Any] = []
    stream_chunk_count = 0

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
    is_upstream_anthropic = source_type == "anthropic"

    stream_success = False
    try:
        async with client.stream("POST", url, json=upstream_data, headers=headers) as resp:
            resp.raise_for_status()
            resp_status_code = resp.status_code
            resp_headers = dict(resp.headers)

            upstream_event_type = None
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                if is_upstream_anthropic and line.startswith("event: "):
                    upstream_event_type = line[7:].strip()
                    continue
                if not line.startswith("data: "):
                    # 非 data 行（如 SSE 注释），原样转发
                    _record_chunk(line)
                    continue

                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    _record_chunk("[DONE]")
                    if not output_anthropic_sse:
                        yield "data: [DONE]\n\n"
                    continue

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    _record_chunk(line)
                    yield line + "\n\n"
                    continue

                _record_chunk(chunk)

                if is_upstream_anthropic and upstream_event_type is not None:
                    chunk["_event_type"] = upstream_event_type

                if converter:
                    converted = converter.convert_stream_chunk(chunk, source_type)
                    if converted is not None:
                        if output_anthropic_sse:
                            evt_type = converter.get_stream_event_type(chunk, source_type)
                            if evt_type:
                                yield _yield_anthropic_event(evt_type, converted)
                            else:
                                yield f"data: {json.dumps(converted, ensure_ascii=False)}\n\n"
                            extra = converter.get_extra_events(converted)
                            if extra:
                                yield _yield_anthropic_events(extra)
                        else:
                            yield f"data: {json.dumps(converted, ensure_ascii=False)}\n\n"
                            extra = converter.get_extra_events(converted)
                            if extra:
                                for extra_evt in extra:
                                    yield f"data: {json.dumps(extra_evt, ensure_ascii=False)}\n\n"
                else:
                    if output_anthropic_sse and is_upstream_anthropic:
                        evt = upstream_event_type or "ping"
                        yield _yield_anthropic_event(evt, chunk)
                    else:
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                if is_upstream_anthropic:
                    upstream_event_type = None

        stream_success = True
        _log_debug(
            channel=channel, upstream_url=url, upstream_data=upstream_data,
            upstream_headers=headers, is_stream=True, stream_content=stream_chunks,
            response_headers=resp_headers, status_code=resp_status_code,
        )
    except Exception as e:
        load_balancer.record_failure(channel.id)
        _log_debug(
            channel=channel, upstream_url=url, upstream_data=upstream_data,
            upstream_headers=headers, is_stream=True, stream_content=stream_chunks,
            response_headers=resp_headers, status_code=resp_status_code, error=str(e),
        )
        # 流式传输中发生错误，向客户端发送错误事件后结束流
        if output_anthropic_sse:
            yield _yield_anthropic_event("error", {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            })
        else:
            error_data = {"error": {"message": f"流式传输错误: {e}", "type": "api_error"}}
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
    finally:
        if stream_success:
            load_balancer.record_success(channel.id)
        if not client.is_closed:
            await client.aclose()
