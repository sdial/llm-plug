"""Endpoint Execution：对一个明确 Endpoint 完成请求准备、发送与响应处理。"""

import copy
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

import stats
from capability_manager import merge_system_messages
from client import create_client, get_upstream_headers
from context_shaping import shape_request
from context_shaping.prompts import PromptIntegrityError
from conversion_plan import ConversionPlan, IncompatibleRequestError, IncompatibleResponseError, prepare_request, prepare_response
from converters.stream_usage import _normalize_full_response_usage
from converters.to_chat import ToChatCompletionsConverter
from models.api_types import APIType
from models.channel import Channel, Endpoint
from pii_errors import SensitiveBlockError
from pii_filter import apply_pii_filter
from proxy import outcomes
from proxy.media import _save_multimodal_files
from proxy.outcomes import OutcomeKind

# 请求日志落库组装单一住所（ADR-0014 D2）：实现迁入 proxy.request_record
# （kwarg 集中 / headers 脱敏一处 / stats 与 request_logs 双后端分发）。
# Endpoint Execution 内部统一通过这一引用完成请求记录。
from proxy.request_record import record_request as _record_request
from proxy.think_filter import _filter_think_in_response
from rate_limiting import (
    RateLimitExceeded,
    _parse_retry_after,
    acquire_send_budget,
)
from upstream_profile_resolver import resolve_upstream_profile
from url_builder import append_query, build_upstream_url


@dataclass(frozen=True, slots=True)
class EndpointExecutionInput:
    """一次明确 Endpoint 执行所需的不可变请求事实。"""

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


def _copy_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """建立本次 Endpoint Execution 的深隔离工作副本。"""
    return copy.deepcopy(dict(payload))


def _conversion_log_info(plan: ConversionPlan, resolved_profile) -> dict[str, Any]:
    dispositions = {item.disposition.value for item in plan.diagnostics}
    result = "exact" if dispositions <= {"exact"} else "compatible"
    return {
        "result": result,
        "inbound_api_type": plan.inbound_api_type.value,
        "upstream_api_type": plan.upstream_api_type.value,
        "upstream_profile_id": resolved_profile.upstream_profile_id,
        "catalog_revision": resolved_profile.catalog_revision,
        "diagnostics": [
            {"code": item.code, "path": item.path, "feature": item.feature, "disposition": item.disposition.value} for item in plan.diagnostics[:20]
        ],
    }


def _safe_response_text(resp: httpx.Response, max_len: int = 1000) -> str:
    """安全读取响应体用于错误日志，解码失败时回退到 raw bytes repr。"""
    try:
        return resp.text[:max_len]
    except Exception:
        return repr(resp.content[:max_len])


def _extract_response_body(resp: httpx.Response):
    """best-effort 解析响应体：优先 JSON，否则原文。"""
    try:
        return resp.json()
    except Exception:
        try:
            return resp.text or None
        except Exception:
            return None


def _format_error_msg(exc: BaseException, error_body) -> str:
    """请求日志 error_msg：有原始 body 存原文全文，否则至少保留异常类型。"""
    if isinstance(error_body, (dict, list)):
        try:
            return json.dumps(error_body, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(error_body)
    if isinstance(error_body, str) and error_body.strip():
        return error_body
    message = str(exc).strip()
    # httpx.ReadTimeout / ConnectTimeout 等常见传输异常的 __str__ 可能为空。
    # 若原样入库，失败行虽有 success=0 却没有可诊断原因；保留异常类名使 stats
    # 的轻量明细与 Raw Request Log 均至少能说明失败类别。
    return message or type(exc).__name__


def _build_upstream_headers(
    channel: Channel,
    endpoint: Endpoint,
    client_headers: dict[str, str] | None,
) -> dict:
    forwarded_headers = {}
    skip_headers = {
        "host",
        "authorization",
        "x-api-key",
        "content-type",
        "content-length",
        # 客户端会话 Cookie 与上游 LLM 无关，转发会造成跨域凭证泄漏。
        "cookie",
        # 流式响应会按 UTF-8 逐行解析；不转发客户端声明的压缩编码，
        # 避免不兼容上游返回压缩字节流导致 SSE/JSON 解析失败。
        "accept-encoding",
        # hop-by-hop headers (RFC 2616 Section 13.5.1)
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    for key, val in (client_headers or {}).items():
        if key.lower() not in skip_headers:
            forwarded_headers[key] = val

    headers = get_upstream_headers(channel, forwarded_headers, endpoint=endpoint)
    headers["Content-Type"] = "application/json"
    # Accept-Encoding 由 httpx 自动管理（gzip, deflate, br, zstd），
    # 不再强制 identity；brotli/zstandard 已装，上游无论返回哪种编码都能解压。
    return headers


async def execute_endpoint(
    channel: Channel,
    endpoint: Endpoint,
    input: EndpointExecutionInput,
    *,
    settings: Mapping[str, Any],
    wait_budget: float,
):
    from proxy.conversion import (
        _prepare_openai_response_request_for_upstream,
        get_converter_for_endpoint,
    )
    from proxy.errors import ConverterError
    from proxy.stream_executor import (
        _do_stream_request,
        _raise_preflight_stream_errors,
    )

    request_data = _copy_payload(input.payload)
    upstream_data = request_data
    target_api_type = input.inbound_api_type
    is_stream = input.is_stream
    requested_model = input.requested_model
    client_headers = dict(input.client_headers or {})
    api_key_id = input.api_key_id
    client_ip = input.client_ip
    request_source = input.request_source
    query_string = input.query_string
    request_converter, response_converter, source_type = get_converter_for_endpoint(endpoint, target_api_type)

    # 透传 OpenAI Chat 客户端的 stream_options.include_usage 到 response_converter
    if is_stream and isinstance(response_converter, ToChatCompletionsConverter):
        include_usage = bool((request_data.get("stream_options") or {}).get("include_usage", False))
        response_converter.set_stream_include_usage(include_usage)

    request_data = await _prepare_openai_response_request_for_upstream(
        request_data,
        source_type,
        target_api_type,
    )

    # Conversion Plan 是兼容判定与请求转换的共同入口。先按具体
    # (Channel, Endpoint, model) 解析生效档案，再执行 exact/compatible。
    model = input.serving_model
    resolved_profile = await resolve_upstream_profile(channel, endpoint, model)
    conversion_info: dict[str, Any]
    try:
        prepared = prepare_request(
            request_data,
            target_api_type,
            endpoint.api_type,
            resolved_profile,
            request_converter,
        )
        upstream_data = prepared.payload
        conversion_info = _conversion_log_info(prepared.plan, resolved_profile)
    except Exception as conv_err:
        if isinstance(conv_err, IncompatibleRequestError):
            conversion_info = _conversion_log_info(conv_err.plan, resolved_profile)
            conversion_info["result"] = "rejected"
            _record_request(
                channel_id=channel.id,
                channel_name=channel.name,
                model=model,
                api_type=source_type,
                requested_model=requested_model,
                is_stream=is_stream,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                success=False,
                error_msg=str(conv_err),
                api_key_id=api_key_id,
                client_ip=client_ip,
                request_source=request_source,
                request_headers=client_headers,
                conversion_info=conversion_info,
            )
            raise
        logger.warning(f"请求转换失败: {type(conv_err).__name__}: {conv_err}")
        raise ConverterError(f"Request conversion failed: {conv_err}") from conv_err

    # 保存请求中的多模态文件（在 capability 过滤前，保留原始内容）
    await _save_multimodal_files(upstream_data, model, channel)

    shaping_result = shape_request(
        upstream_data,
        resolved_profile=resolved_profile,
        settings=settings,
    )
    upstream_data = shaping_result.payload
    shaping_info = shaping_result.receipt

    # 部分上游的确定性动作由档案声明，不再根据 URL keyword 推断。
    if resolved_profile.requires_single_system_message and "messages" in upstream_data:
        original_count = len([m for m in upstream_data["messages"] if m.get("role") == "system"])
        upstream_data["messages"] = merge_system_messages(upstream_data["messages"])
        new_count = len([m for m in upstream_data["messages"] if m.get("role") == "system"])
        if original_count > 1:
            logger.debug(f"[CAPABILITY] MiniMax: 合并 {original_count} 条 system 消息为 {new_count} 条")

    need_think_filter = resolved_profile.filter_think_content

    url = build_upstream_url(endpoint)
    if query_string and source_type == target_api_type.value:
        url = append_query(url, query_string)
    headers = _build_upstream_headers(
        channel,
        endpoint,
        client_headers,
    )

    # === PII 敏感信息过滤 ===
    # 在 Context Shaping 与 capability 处理之后、真正发网前最后一道处理。
    # 流式与非流式共用 upstream_data，一次调用覆盖两条路径。
    sensitivity_info: dict[str, Any] | None = None
    try:
        upstream_data, pii_info = apply_pii_filter(
            upstream_data,
            target_api_type=source_type,
            settings=settings,
            channel_id=channel.id,
            return_info=True,
        )
    except SensitiveBlockError as block_err:
        sensitivity_info = {
            "enabled": True,
            "action": "block",
            "rules_triggered": block_err.triggered,
            "target_api_type": target_api_type.value,
        }
        # 拦截请求：记录 redacted body 后立即抛异常，不发上游。
        _record_request(
            channel_id=channel.id,
            channel_name=channel.name,
            model=model,
            api_type=source_type,
            requested_model=requested_model,
            is_stream=is_stream,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            lag_ms=None,
            success=False,
            error_msg=str(block_err),
            finish_reason=None,
            api_key_id=api_key_id,
            client_ip=client_ip,
            request_source=request_source,
            request_headers=headers,
            request_body={"_redacted": True, "reason": "pii_block"},
            response_body=None,
            sensitivity_info=sensitivity_info,
            conversion_info=conversion_info,
            shaping_info=shaping_info,
        )
        raise
    else:
        if pii_info.get("enabled"):
            sensitivity_info = pii_info

    if shaping_result.prompt_integrity is not None:
        try:
            shaping_result.prompt_integrity.verify(upstream_data)
        except PromptIntegrityError as integrity_err:
            sensitivity_info = {
                "enabled": True,
                "action": "block",
                "rules_triggered": ["PROMPT_EXTENSION_INTEGRITY"],
                "target_api_type": source_type,
            }
            _record_request(
                channel_id=channel.id,
                channel_name=channel.name,
                model=model,
                api_type=source_type,
                requested_model=requested_model,
                is_stream=is_stream,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                success=False,
                error_msg=str(integrity_err),
                api_key_id=api_key_id,
                client_ip=client_ip,
                request_source=request_source,
                request_headers=headers,
                request_body={"_redacted": True, "reason": "prompt_extension_integrity"},
                sensitivity_info=sensitivity_info,
                conversion_info=conversion_info,
                shaping_info=shaping_info,
            )
            raise SensitiveBlockError(str(integrity_err), triggered=["PROMPT_EXTENSION_INTEGRITY"]) from integrity_err

    # 上游限速适配：发送前按渠道 RPM 节流（滑动窗口排队），
    # 把突发流量在代理侧平滑掉，从源头避免直接撞 429。
    # 等待预算由外层循环传入（429 重试与发送排队共用同一预算）。
    await acquire_send_budget(
        channel,
        wait_timeout=wait_budget,
    )
    if is_stream:
        # Chat→Chat 同格式直通：自动注入 stream_options.include_usage=true，
        # 让上游在流式末尾返回真实 usage chunk（OpenAI 规范仅此方式提供流式 token 用量）。
        # 跨格式转换已由 ToChatCompletionsConverter 无条件注入（converters/to_chat.py），
        # 此处补齐同格式直通路径，否则流式请求日志/统计拿不到任何 token 数字。
        if source_type == APIType.OPENAI_CHAT.value and target_api_type == APIType.OPENAI_CHAT:
            stream_opts = upstream_data.get("stream_options")
            if not isinstance(stream_opts, dict):
                upstream_data["stream_options"] = {"include_usage": True}
        stream = _do_stream_request(
            channel,
            url,
            headers,
            upstream_data,
            response_converter,
            source_type,
            target_api_type,
            api_key_id=api_key_id,
            client_ip=client_ip,
            need_think_filter=need_think_filter,
            requested_model=requested_model,
            request_source=request_source,
            sensitivity_info=sensitivity_info,
            conversion_info=conversion_info,
            shaping_info=shaping_info,
        )
        return _raise_preflight_stream_errors(stream)

    # 非流式：使用缓存的 httpx 客户端（不可 async with，否则会关闭共享连接）
    request_start = time.time()  # 整体起点，create_client 失败时兜底
    upstream_start: float | None = None  # 上游请求起点（不含连接建立）
    try:
        client = await create_client(channel, endpoint=endpoint)
        upstream_start = time.time()
        resp = await client.post(url, json=upstream_data, headers=headers)
        try:
            stats.record_context_shaping_receipt(channel.id, model, shaping_info)
        except Exception as shaping_stats_err:
            logger.warning(f"Context Shaping stats record failed: {type(shaping_stats_err).__name__}")
        if resp.status_code == 429:
            # 上游限速：携带 Retry-After 抛出，外层等待后重试同一渠道。
            # 同时挂载原始错误 body 与 response，供窗口级限速检测与请求日志使用。
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            error_body = _extract_response_body(resp)
            logger.warning(f"[RATE LIMIT] 上游限速 429 url={url} retry_after={retry_after if retry_after is not None else '未指定'}")
            raise RateLimitExceeded(
                f"上游限速 (429): {url}",
                retry_after=retry_after,
                error_body=error_body,
                response=resp,
            )
        if resp.is_error:
            logger.error(f"[UPSTREAM ERROR] status={resp.status_code} url={url} body={_safe_response_text(resp)}")
        resp.raise_for_status()
        response_data = resp.json()

        # 转换响应：上游格式 → 客户端格式
        if response_converter:
            try:
                response_data = prepare_response(response_data, endpoint.api_type, target_api_type, response_converter)
            except IncompatibleResponseError as conv_err:
                conversion_info["result"] = "rejected_response"
                conversion_info["response_diagnostic"] = {
                    "code": conv_err.code,
                    "path": conv_err.path,
                    "feature": conv_err.diagnostic.feature,
                    "disposition": conv_err.diagnostic.disposition.value,
                }
                raise
            except Exception as conv_err:
                logger.warning(f"响应转换失败: {type(conv_err).__name__}: {conv_err}")
                raise ConverterError(f"Response conversion failed: {conv_err}") from conv_err

        latency_ms = int((time.time() - upstream_start) * 1000)

        # 过滤 💭 内容
        if need_think_filter:
            response_data = _filter_think_in_response(response_data)

        # 提取 token 使用量与 finish_reason（ADR-0015 D1：委托 stream_usage 一次性
        # 归一，与非流式 / non-SSE 兜底共用同一方言住所）
        # 注意：某些 API（如 Kimi）的 input_tokens 可能为 0（表示缓存后），实际值在 prompt_tokens
        # 注意：部分上游（如 NVIDIA z-ai/glm-5.2）可能显式返回 usage: null——归一函数容错
        usage_summary = _normalize_full_response_usage(response_data if isinstance(response_data, dict) else {})

        # 记录统计（入队，由后台 worker 写入，不阻塞响应）
        _record_request(
            channel_id=channel.id,
            channel_name=channel.name,
            model=model,
            api_type=source_type,
            requested_model=requested_model,
            is_stream=False,
            input_tokens=usage_summary.input_tokens,
            output_tokens=usage_summary.output_tokens,
            cache_read_input_tokens=usage_summary.cache_read_input_tokens,
            cache_creation_input_tokens=usage_summary.cache_creation_input_tokens,
            latency_ms=latency_ms,
            lag_ms=None,
            success=True,
            finish_reason=usage_summary.finish_reason,
            api_key_id=api_key_id,
            client_ip=client_ip,
            request_source=request_source,
            request_headers=headers,
            response_headers=dict(resp.headers),
            request_body=upstream_data,
            response_body=response_data,
            sensitivity_info=sensitivity_info,
            conversion_info=conversion_info,
            shaping_info=shaping_info,
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
                        summary.append(f"text({len(c.get('text', ''))}chars)")
                    elif c.get("type") == "tool_use":
                        summary.append(f"tool_use({c.get('name', '')})")
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
                                summary.append(f"tool_use({func.get('name', '')})")
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
                                        summary.append(f"text({len(part.get('text', ''))}chars)")
                                    else:
                                        summary.append(part.get("type", "?"))
                            elif item_type == "function_call":
                                summary.append(f"tool_use({item.get('name', '')})")
                            elif item_type:
                                summary.append(item_type)
                    stop_label = response_data.get("status") or response_data.get("stop_reason")

            if summary:
                logger.debug(f"content: [{', '.join(summary)}]")
            logger.debug(f"stop_reason: {stop_label or '?'}")

        # 健康记账直连 outcomes（ADR-0025 D1，LB 兼容外壳已退场）：健康键 model 段
        # 推导唯一住所 effective_model——上游 body model 为 "" 时不再静默丢账，
        # 以 (channel.id, channel_id) 渠道级虚拟键照记（判决备选 A）。
        outcomes.record(outcomes.effective_model(model=model, channel_id=channel.id), channel.id, OutcomeKind.success)
        return response_data
    except Exception as e:
        # upstream_start 为 None 表示 create_client 失败，此时用 request_start 兜底
        latency_ms = int((time.time() - (upstream_start or request_start)) * 1000)
        error_body = getattr(e, "error_body", None)
        if error_body is None and isinstance(e, httpx.HTTPStatusError):
            error_body = _extract_response_body(e.response)
        # 记录失败统计（入队，由后台 worker 写入，不阻塞响应）
        _record_request(
            channel_id=channel.id,
            channel_name=channel.name,
            model=model,
            api_type=source_type,
            requested_model=requested_model,
            is_stream=False,
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            lag_ms=None,
            success=False,
            error_msg=_format_error_msg(e, error_body),
            finish_reason=None,
            api_key_id=api_key_id,
            client_ip=client_ip,
            request_source=request_source,
            request_headers=headers,
            request_body=upstream_data,
            response_body=error_body,
            sensitivity_info=sensitivity_info,
            conversion_info=conversion_info,
            shaping_info=shaping_info,
        )
        # 控制台输出详细错误
        err_body = ""
        if isinstance(e, httpx.HTTPStatusError):
            err_body = _safe_response_text(e.response, 500)
            logger.error(f"upstream {e.response.status_code} {url}")
            logger.error(f"body: {err_body}")
        elif isinstance(e, RateLimitExceeded):
            # 限速属预期事件（外层负责等待重试），用 warning 而非 error
            logger.warning(f"upstream rate limited: {e}")
        else:
            logger.error(f"upstream {type(e).__name__}: {e}")

        raise


__all__ = [
    "EndpointExecutionInput",
    "execute_endpoint",
]
