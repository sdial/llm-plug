"""
流式上游请求执行：首包预热、SSE 事件解析/格式化、空流/流内错误事件、流式 think 块过滤、
生成器客户端生命周期与请求日志。

真实实现（从 proxy.core 迁入）；proxy.routing 对本模块符号做门面聚合。
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

import config
import stats
from client import create_stream_client
from conversion_plan import IncompatibleResponseError, prepare_response, validate_stream_response_chunk
from converters.stream_events import (
    _build_anthropic_error_events,
    _build_anthropic_message_stop_event,
    _build_chat_done_event,
    _build_chat_error_chunk,
    _build_chat_stream_chunks_from_object,
    _build_responses_completed_event,
    _build_responses_error_events,
    _convert_anthropic_response_to_events,
    _convert_non_stream_to_stream_events,
    _format_passthrough_sse_block,
    _format_raw_sse,
    _format_sse_for_list,
    _iter_sse_blocks,
    _yield_anthropic_event,
)
from converters.stream_usage import (
    _normalize_full_response_usage,
    _StreamUsageAccumulator,
    _StreamUsageSummary,
)
from models.api_types import APIType
from models.channel import Channel
from proxy import outcomes
from proxy.endpoint_execution import (
    _extract_response_body,
    _format_error_msg,
    _safe_response_text,
)
from proxy.errors import (
    ConverterError,  # re-export：定义体在错误域，保留旧导入面
    _EmptyStreamError,  # re-export：定义体已迁入错误域（ADR-0014 D0），保留旧导入面
    _is_channel_config_error,
    _is_retryable_exception,
    _StreamPreflightError,  # re-export：定义体已迁入错误域（ADR-0015 D2），保留旧导入面
    _UpstreamStreamErrorEvent,
    classify_failure,
)
from proxy.outcomes import OutcomeKind

# 落库组装单一住所（ADR-0014 D2）：从 proxy.request_record 直接导入，
# 共享辅助函数直接取自 Endpoint Execution 的真实住所。
from proxy.request_record import record_request as _record_request
from proxy.stream_reconstruct import _build_stream_response_body
from proxy.think_filter import _filter_think_in_stream_chunk
from think_filter import ThinkFilter


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
            return not any(isinstance(chunk, dict) and chunk.get("type") == "message_stop" for chunk in stream_chunks)
        return True
    if target_api_type == APIType.OPENAI_RESPONSE:
        # 检查 stream_chunks 中是否已有 response.completed 或 response.failed
        return not any(isinstance(chunk, dict) and chunk.get("type") in ("response.completed", "response.failed") for chunk in stream_chunks)
    # Chat Completions target
    return not done_received


# _StreamPreflightError / _UpstreamStreamErrorEvent / _EmptyStreamError 定义体已全部
# 迁入 proxy.errors（ADR-0014 D0 + ADR-0015 D2，错误域对流执行器零反向依赖）；
# 本模块经顶部 import 保留同名 re-export（__all__ 不变），旧导入面 / monkeypatch
# 路径零修改。


def _is_heartbeat_chunk(chunk: Any) -> bool:
    """判断 SSE chunk 是否仅为注释/心跳行（: 开头），不算真实输出。"""
    if not isinstance(chunk, str):
        return False
    lines = [ln for ln in chunk.split("\n") if ln.strip()]
    return bool(lines) and all(ln.strip().startswith(":") for ln in lines)


def _summarize_stream_event(sse_data: str) -> tuple[str, str]:
    """提取 SSE 事件的 event type 与关键信息摘要（debug 流式事件日志用，纯函数）。"""
    evt_type = ""
    data_summary = ""
    for ln in sse_data.strip().split("\n"):
        if ln.startswith("event: "):
            evt_type = ln[7:]
        elif ln.startswith("data: "):
            try:
                d = json.loads(ln[6:])
                # 关键字段摘要
                if d.get("type") == "content_block_start":
                    cb = d.get("content_block", {})
                    data_summary = f"cb_start({cb.get('type', '')}{',' + cb.get('name', '') if cb.get('name') else ''})"
                elif d.get("type") == "content_block_delta":
                    delta = d.get("delta", {})
                    dtype = delta.get("type", "")
                    if dtype == "text_delta":
                        data_summary = f"text({len(delta.get('text', ''))}chars)"
                    elif dtype == "input_json_delta":
                        data_summary = f"json({delta.get('partial_json', '')})"
                    elif dtype == "thinking_delta":
                        data_summary = f"thinking({len(delta.get('thinking', ''))}chars)"
                    else:
                        data_summary = dtype
                elif d.get("type") == "content_block_stop":
                    data_summary = f"cb_stop(idx={d.get('index', '')})"
                elif d.get("type") == "message_start":
                    data_summary = f"id={d.get('message', {}).get('id', '')}"
                elif d.get("type") == "message_delta":
                    data_summary = f"stop={d.get('delta', {}).get('stop_reason', '')}"
                else:
                    data_summary = d.get("type", str(d)[:80])
            except json.JSONDecodeError:
                data_summary = ln[6:][:80]
    return evt_type, data_summary


def _reassemble_passthrough_sse(sse: str, passthrough_lines: list[str], source_type: str) -> str:
    """Passthrough SSE 行重组装（ADR-0015 D3 第 3 对，原为两处逐字 9 行复制块）。

    把格式化结果 `sse` 中的 event: / data: 行提取出来，追加到上游 passthrough
    行之后重排——保证透传行（含注释/心跳行）先于协议字段行输出。仅非 Responses
    上游且有 passthrough 行时重排，其余原样返回（Responses 上游例外分支语义保持）。
    """
    if not (passthrough_lines and source_type != APIType.OPENAI_RESPONSE.value):
        return sse
    _sse_lines = list(passthrough_lines)
    for _ln in sse.split("\n"):
        if _ln.startswith("event: "):
            _sse_lines.append(_ln)
    for _ln in sse.split("\n"):
        if _ln.startswith("data:"):
            _sse_lines.append(_ln)
    return "\n".join(_sse_lines) + "\n\n"


# ─── 💭 think 过滤时序接缝（ADR-0015 D0 接缝 4；ADR-0016 D0 元组废除后仅 dict 事件）───
# 过滤应用 / flush 残余合成 / finalize 过滤编排的唯一住所；SSE 骨架按调用点
# 分派给接缝，骨架内不再内联过滤逻辑。私有接缝：不进 __all__，测试可直接 import。


def _apply_think_filter_to_event(evt: Any, think_filter: ThinkFilter | None) -> Any:
    """对单个流式事件应用 💭 过滤（过滤应用点单一住所）。

    事件统一 dict 形态（ADR-0016 D0）：dict 事件直接过滤；其余（无过滤器、
    非 dict 的透传事件）原样返回。
    返回 None 表示整个事件被吞（💭 块内容被全部过滤时无害，调用方跳过即可）。
    """
    if not think_filter:
        return evt
    return _filter_think_in_stream_chunk(evt, think_filter)


def _think_flush_residual_events(
    think_filter: ThinkFilter | None,
    output_anthropic_sse: bool,
    output_responses_sse: bool,
    model: str,
    last_text_delta_index: int,
) -> list[str]:
    """ThinkFilter flush 残余合成（承接 D3 阶段参数化收尾函数的 step 1，升至模块级）。

    按目标格式把残余内容合成合法事件形状，[DONE] 与 EOF 收尾路径共用（字节一致）：
    Responses 目标 response.output_text.delta；Anthropic 目标必须输出合法
    content_block_delta（text_delta），复用最近 text 块 index 的 M2 语义；
    Chat 目标输出 chat.completion.chunk。无过滤器或无残余返回空列表。
    """
    if not think_filter:
        return []
    remaining = think_filter.flush()
    if not remaining:
        return []
    if output_responses_sse:
        return [_format_sse_for_list({"type": "response.output_text.delta", "delta": remaining}, infer_event_type=output_responses_sse)]
    if output_anthropic_sse:
        return [
            _format_sse_for_list(
                {
                    "type": "content_block_delta",
                    "index": last_text_delta_index,
                    "delta": {"type": "text_delta", "text": remaining},
                },
                "content_block_delta",
                infer_event_type=output_responses_sse,
            )
        ]
    # Chat Completions 格式
    return [
        _format_sse_for_list(
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": remaining},
                        "finish_reason": None,
                    }
                ],
            },
            infer_event_type=output_responses_sse,
        )
    ]


def _format_think_filtered_finalize_events(
    events: list[Any],
    think_filter: ThinkFilter | None,
    infer_event_type: bool,
    log_event=None,
) -> list[str]:
    """converter finalize 事件（两拍协议拍 2）的 💭 过滤与格式化编排。

    M2 语义：finalize 事件此前零过滤，现统一经 _apply_think_filter_to_event 过滤，
    被吞事件跳过。事件统一 dict 形态（ADR-0016 D0），SSE event: 行由
    _format_sse_for_list 从 dict 的 type 字段推断（infer_event_type）。格式化
    异常记日志后原样上抛（调用方异常边界语义不变）。
    """
    if not events:
        return []
    results = []
    try:
        for evt in events:
            filtered_evt = _apply_think_filter_to_event(evt, think_filter)
            if filtered_evt is None:
                continue
            sse = _format_sse_for_list(filtered_evt, infer_event_type=infer_event_type)
            if log_event is not None:
                log_event(sse)
            results.append(sse)
    except Exception as format_err:
        logger.error(f"[FORMAT EXTRA ERROR] {type(format_err).__name__}: {format_err}")
        logger.exception("[FORMAT EXTRA ERROR TRACEBACK]")
        raise
    return results


# ─── non-SSE JSON 兜底接缝（ADR-0015 D0 接缝 3）───
# "上游对流式请求回了非 SSE JSON 整块"的兜底处理唯一住所：解析整块 → 按目标格式拆为
# 事件序列（Anthropic 拆 message 事件 / Responses 拆事件 / Chat 拆 chunk 序列，含
# think 过滤）→ 捕获 usage 与 finish_reason → 解析失败走错误事件接缝。usage 归一
# 委托 stream_usage 模块的一次性归一函数（ADR-0015 D1）。私有接缝：不进 __all__，
# 测试可直接 import。


@dataclass
class _NonSseUsageResult(_StreamUsageSummary):
    """non-SSE 兜底接缝的 usage/finish_reason 捕获结果（经 usage_out 持有人回传调用方）。"""

    parse_failed: bool = False


async def _emit_non_sse_fallback_events(
    non_sse_stream_body: str,
    *,
    model: str,
    source_type: str,
    response_converter,
    output_anthropic_sse: bool,
    output_responses_sse: bool,
    output_sse_events: bool,
    is_upstream_anthropic: bool,
    think_filter: ThinkFilter | None,
    cur_input_tokens: int,
    cur_output_tokens: int,
    cur_finish_reason: str | None,
    usage_out: _NonSseUsageResult,
    record_chunk,
    mark_first_token,
    mark_output,
    log_event,
    emit_error_events,
):
    """非 SSE JSON 整块转流兜底（ADR-0015 D0 接缝 3，原为主循环深处内联块）。

    解析整块 body：成功时按目标格式拆为事件序列，并把归一后的 usage/finish_reason
    写入 usage_out（缺省回填调用方现值）；解析失败（JSONDecodeError 或非 dict 整块）
    时记日志、置 usage_out.parse_failed=True 并改走错误事件接缝（emit_error_events）
    产出三格式错误事件，由调用方复位记账状态（stream_error/stream_success）。
    record_chunk / mark_first_token / mark_output / log_event 为骨架侧回调，调用点
    与内联时期逐点一致；输出与拆出前逐字节一致。
    """
    try:
        full_response = json.loads(non_sse_stream_body)
    except json.JSONDecodeError:
        full_response = None
        logger.warning(f"[STREAM NON-SSE JSON ERROR] model={model}")
    if not isinstance(full_response, dict):
        logger.error(f"[STREAM NON-SSE PARSE FAILED] model={model} body={non_sse_stream_body[:200]}")
        usage_out.parse_failed = True
        async for sse in emit_error_events("Upstream returned unparseable response"):
            yield sse
        return
    logger.warning(
        f"[STREAM NON-SSE] upstream returned non-SSE JSON for streaming request (object={full_response.get('object')}), converting to stream events"
    )
    record_chunk(full_response)
    mark_first_token()
    # non-SSE 兜底仍属于响应转换边界；先验证完整对象，避免拆事件 helper
    # 在未知/不可表达输出上静默丢失内容。
    fallback_target_type = APIType.ANTHROPIC if output_anthropic_sse else APIType.OPENAI_RESPONSE if output_responses_sse else APIType.OPENAI_CHAT
    prepare_response(full_response, APIType(source_type), fallback_target_type, None)
    if output_anthropic_sse:
        if response_converter:
            converted = prepare_response(full_response, APIType(source_type), APIType.ANTHROPIC, response_converter)
            for evt_type, evt_data in _convert_anthropic_response_to_events(converted):
                sse = _yield_anthropic_event(evt_type, evt_data)
                log_event(sse)
                mark_output()
                yield sse
        else:
            # 同类型 Anthropic 直通：非 SSE JSON 也需拆分为
            # message_start / content_block_start / ... / message_stop 事件序列
            for evt_type, evt_data in _convert_anthropic_response_to_events(full_response):
                sse = _yield_anthropic_event(evt_type, evt_data)
                log_event(sse)
                mark_output()
                yield sse
    elif output_responses_sse:
        # Response→Response 透传或跨格式都走拆事件，避免裸吐整块 JSON。
        stream_events = _convert_non_stream_to_stream_events(
            full_response,
            response_converter,
            source_type,
            output_responses_sse,
        )
        for sse in stream_events:
            log_event(sse)
            mark_output()
            yield sse
    else:
        # Chat→Chat 透传：把整块 chat.completion 拆成 chat.completion.chunk 序列，
        # 避免客户端拿到非流式形态破坏 SSE 协议。
        chat_chunks = _build_chat_stream_chunks_from_object(full_response, model)
        if chat_chunks:
            for chunk_obj in chat_chunks:
                # 同格式透传：think 过滤仍需生效（对齐流式分支）。
                # Anthropic 上游用 type: thinking 块，不存在 💭 标记，跳过过滤。
                if not is_upstream_anthropic:
                    chunk_obj = _apply_think_filter_to_event(chunk_obj, think_filter)
                    if chunk_obj is None:
                        continue
                sse = _format_sse_for_list(chunk_obj, infer_event_type=output_responses_sse)
                log_event(sse)
                mark_output()
                yield sse
        else:
            sse = _format_sse_for_list(full_response, infer_event_type=output_responses_sse)
            log_event(sse)
            mark_output()
            yield sse
    if not output_sse_events:
        mark_output()
        yield "data: [DONE]\n\n"
    # usage/finish_reason 归一委托 stream_usage 一次性归一函数（ADR-0015 D1）
    summary = _normalize_full_response_usage(
        full_response,
        cur_input_tokens=cur_input_tokens,
        cur_output_tokens=cur_output_tokens,
        cur_finish_reason=cur_finish_reason,
    )
    usage_out.input_tokens = summary.input_tokens
    usage_out.output_tokens = summary.output_tokens
    usage_out.cache_read_input_tokens = summary.cache_read_input_tokens
    usage_out.cache_creation_input_tokens = summary.cache_creation_input_tokens
    usage_out.finish_reason = summary.finish_reason


async def _do_stream_request(
    channel: Channel,
    url: str,
    headers: dict,
    upstream_data: dict,
    response_converter,
    source_type: str,
    target_api_type: APIType = APIType.OPENAI_CHAT,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    need_think_filter: bool = False,
    requested_model: str | None = None,
    request_source: str = "client",
    sensitivity_info: dict[str, Any] | None = None,
    conversion_info: dict[str, Any] | None = None,
    shaping_info: dict[str, Any] | None = None,
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
    # usage 方言累积器（ADR-0015 D1）：流内增量提取的单一住所，逐块 feed，
    # 替代原内联三协议提取块；摘要字段供终端事件 / non-SSE 兜底 / 记账收尾读取
    usage_acc = _StreamUsageAccumulator(source_type)
    stream_chunks: list[Any] = []
    stream_chunk_count = 0
    _stream_log_enabled = config.LOG_LEVEL == "debug"
    _stream_log_count = 0  # 流式事件日志计数器
    _STREAM_LOG_MAX = 20  # 最多记录前 20 个事件

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
        evt_type, data_summary = _summarize_stream_event(sse_data)
        if evt_type:
            logger.debug(f"{evt_type}: {data_summary}")
        elif data_summary:
            logger.debug(f"data: {data_summary}")

    _chunk_truncate_warned = False
    _max_stream_chunks = int(config.get_setting("max_stream_chunks") or 50000)

    def _record_chunk(item: Any):
        """记录stream chunk，超过限制后停止记录并警告一次"""
        nonlocal stream_chunk_count, _chunk_truncate_warned
        max_stream_chunks = _max_stream_chunks
        if stream_chunk_count < max_stream_chunks:
            stream_chunks.append(item)
            stream_chunk_count += 1
        elif not _chunk_truncate_warned:
            _chunk_truncate_warned = True
            logger.warning(
                f"[STREAM CHUNK TRUNCATED] model={model} "
                f"exceeded max_stream_chunks={max_stream_chunks}, "
                f"subsequent chunks will not be recorded; "
                f"reconstructed response body in request logs will be incomplete"
            )

    resp_status_code = None
    resp_headers = None
    client = create_stream_client(channel)
    output_anthropic_sse = target_api_type == APIType.ANTHROPIC
    output_responses_sse = target_api_type == APIType.OPENAI_RESPONSE
    output_sse_events = output_anthropic_sse or output_responses_sse
    is_upstream_anthropic = source_type == "anthropic"
    is_upstream_event_sse = is_upstream_anthropic or source_type == "openai-response"

    async def _emit_error_events(message: str):
        """按目标格式构造错误事件三格式输出（ADR-0015 D3 第 4 对：两处复制块合并）。

        non-SSE 解析失败与流式异常处理器两个调用点共用，message 由调用方给定。
        Anthropic 目标输出 error 事件 + message_stop，Responses 目标输出 error 事件 +
        response.failed（协议终止事件由各 builder 自带），Chat 目标输出 error chunk 并补
        [DONE] 终止行。输出与合并前逐字节一致。
        """
        if output_anthropic_sse:
            for evt in _build_anthropic_error_events(message):
                yield evt
        elif output_responses_sse:
            # 发送 response.failed 事件以便客户端正确识别流结束
            for evt in _build_responses_error_events(message, model):
                yield evt
        else:
            for evt in _build_chat_error_chunk(message):
                yield evt
        if not output_sse_events:
            yield _build_chat_done_event()[0]

    stream_success = False
    stream_error = None
    stream_error_body = None
    emitted_output = False
    emitted_real_output = False  # M10: 排除心跳注释行的真实输出标记
    # 失败证据（ADR-0014 D1）：异常对象或上游错误事件。失败分支只保留证据不定 kind，
    # finally 内调 classify_failure 一次判定——流执行器内 kind 判定的单一住所。
    stream_failure_evidence: BaseException | None = None
    # 失败已随预检异常移交接入点回退层记账（首包前失败）：finally 不再记账，同一失败不双记。
    stream_failure_handed_off = False
    local_response_incompatible = False
    cancelled = False
    last_text_delta_index = 0  # M2: Anthropic 目标 EOF flush 输出 content_block_delta 时复用最近的块 index
    logger.debug(f"[STREAM START] model={model} url={url} target={target_api_type.value}")
    try:
        async with client.stream("POST", url, json=upstream_data, headers=headers) as resp:
            try:
                stats.record_context_shaping_receipt(channel.id, model, shaping_info)
            except Exception as shaping_stats_err:
                logger.warning(f"Context Shaping stats record failed: {type(shaping_stats_err).__name__}")
            if resp.is_error:
                await resp.aread()
                logger.error(f"[STREAM UPSTREAM ERROR] status={resp.status_code} url={url} body={_safe_response_text(resp)}")
            resp.raise_for_status()
            resp_status_code = resp.status_code
            resp_headers = dict(resp.headers)
            logger.debug(f"[STREAM CONNECTED] status={resp_status_code} headers={resp_headers}")

            # 空闲超时（从最后一次收到实际数据开始计时）。SSE 心跳行不算数据，
            # 否则上游只发 keep-alive 心跳时，httpx 的 read 超时每次读到字节都会
            # 重置，流永远不会超时。这里在应用层独立计时兜底。
            _idle_timeout = float(config.REQUEST_TIMEOUT)
            _last_payload_time = time.monotonic()

            upstream_event_type = None

            def _mark_first_token():
                nonlocal first_token_time
                if first_token_time is None:
                    first_token_time = time.time()

            def _mark_output():
                nonlocal emitted_output, emitted_real_output
                emitted_output = True
                emitted_real_output = True

            def _mark_heartbeat():
                # M10: 心跳注释行只标记 emitted_output（供空闲超时/收尾判断），
                # 不标记 emitted_real_output——若随后紧跟上游 error 事件仍可故障转移
                nonlocal emitted_output
                emitted_output = True

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
                    return [*_build_anthropic_message_stop_event()]
                if output_responses_sse:
                    # Responses 目标：补发 response.completed 避免客户端挂起
                    # （ADR-0015 D0 接缝 2：改调 stream_events 工厂，usage 取当前累计值）
                    return [*_build_responses_completed_event(model, usage_acc.input_tokens, usage_acc.output_tokens)]
                # Chat Completions
                return [*_build_chat_done_event()]

            def _format_upstream_chunk_sse(chunk: Any) -> str:
                """上游 chunk / 错误事件的 SSE 格式化分发（ADR-0015 D3：原为主循环内
                上游错误分支与直通分支两处逐字复制块，合并为单一函数）。

                Responses 上游透传保 event:/data: 原行；带 event type 的协议事件 SSE
                按目标格式格式化；其余按目标格式裸格式化。末段统一经 passthrough 行
                重组装接缝。upstream_event_type / data_lines / passthrough_lines 取
                当前迭代值（调用点与内联时期逐点一致）。
                """
                if source_type == APIType.OPENAI_RESPONSE.value and is_upstream_event_sse:
                    sse = _format_passthrough_sse_block(
                        upstream_event_type,
                        data_lines,
                        passthrough_lines,
                    )
                elif output_sse_events and is_upstream_event_sse and upstream_event_type:
                    sse = _format_sse_for_list(chunk, upstream_event_type, infer_event_type=output_responses_sse)
                else:
                    sse = _format_sse_for_list(chunk, infer_event_type=output_responses_sse)
                return _reassemble_passthrough_sse(sse, passthrough_lines, source_type)

            async def _emit_terminal_events_on_stream_error():
                """stream_error 置位后补发协议终止事件（ADR-0015 D3：上游错误分支与
                直通分支两处复制块合并；流中错误先输出 error 事件本身再补终止事件）。"""
                for terminal_sse in _terminal_events_for_error():
                    _log_stream_event(terminal_sse)
                    _mark_output()
                    yield terminal_sse

            async def _emit_closing_output(phase: str):
                """[DONE] 正常收尾与 EOF 兜底两条收尾路径的统一输出（ADR-0015 D3：两对复制块合并）。

                phase: "done"（收到上游 [DONE]）或 "eof"（上游断流未发 [DONE]）。
                行为差异仅两处，均由 phase 区分：
                - 日志标签（EOF 前缀）；
                - 终行兜底：EOF 且直通（无 converter）时还须补协议终止事件
                  （Anthropic message_stop / Responses response.completed），
                  [DONE] 路径已收到终止行、不需要。
                输出与合并前逐字节一致。
                """
                label = "" if phase == "done" else "EOF "
                # 1. 刷新 ThinkFilter 残余内容（think 过滤时序接缝：flush 残余合成
                #    按目标格式产出合法事件形状，[DONE]/EOF 两路径字节一致）
                for residual_sse in _think_flush_residual_events(
                    think_filter, output_anthropic_sse, output_responses_sse, model, last_text_delta_index
                ):
                    _log_stream_event(residual_sse)
                    _mark_output()
                    yield residual_sse
                # 2. 调用 converter finalize 补发协议终止事件（两拍协议拍 2）
                if response_converter:
                    final_events = response_converter.finalize_stream(source_type)
                    logger.debug(f"[STREAM {label}FINALIZE] model={model} events={len(final_events)}")
                    for final_event in _format_think_filtered_finalize_events(
                        final_events, think_filter, output_sse_events, log_event=_log_stream_event
                    ):
                        _mark_output()
                        yield final_event
                # 3. 终行兜底：Chat 目标无论是否有 converter 都必须补 `data: [DONE]`
                #    （Anthropic 上游以 message_stop 结束、Responses 以 response.completed
                #    结束，都不发 [DONE]；converter 的 finalize_stream 只负责协议收尾事件，
                #    不负责该终止行）。EOF 阶段直通路径的 Anthropic/Responses 目标仍由
                #    _terminal_events_for_error 补协议终止事件（converter 路径由 step 2 处理）。
                if not output_sse_events:
                    _mark_output()
                    yield "data: [DONE]\n\n"
                elif phase == "eof" and response_converter is None:
                    for terminal_sse in _terminal_events_for_error():
                        _log_stream_event(terminal_sse)
                        _mark_output()
                        yield terminal_sse

            async for (
                upstream_event_type,
                data_lines,
                passthrough_lines,
            ) in _iter_sse_blocks(
                resp.aiter_lines(),
                coalesce_data_lines=is_upstream_event_sse,
            ):
                _line_count += len(data_lines) + len(passthrough_lines) + (1 if upstream_event_type else 0)
                # SSE 心跳行（: 开头的注释/keep-alive）不视为有效数据：
                # 空闲超时只从最后一次收到实际数据块开始计算，避免上游只发心跳
                # keep-alive 时请求永不超时。
                is_heartbeat_only = (
                    not data_lines
                    and upstream_event_type is None
                    and bool(passthrough_lines)
                    and all(line.strip().startswith(":") for line in passthrough_lines)
                )
                if not is_heartbeat_only:
                    _last_payload_time = time.monotonic()
                elif time.monotonic() - _last_payload_time >= _idle_timeout:
                    raise httpx.ReadTimeout(f"Upstream sent only SSE heartbeats/keep-alives for {_idle_timeout:g}s without data")
                if data_lines:
                    _first_line_checked = True
                    data_str = "\n".join(data_lines)
                else:
                    if not _first_line_checked:
                        # 检查是否为 SSE 注释/心跳行（以 : 开头）
                        has_sse_comments = any(line.strip().startswith(":") for line in passthrough_lines)
                        if has_sse_comments:
                            # SSE 注释/心跳行，直接透传并继续读取。
                            # M10: 只标记 emitted_output 不标记 emitted_real_output——
                            # 心跳不是有效响应数据，若随后紧跟 error 事件仍可走首包前故障转移
                            passthrough = "\n".join(passthrough_lines) + "\n\n"
                            _mark_heartbeat()
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
                        # 收尾输出统一（ADR-0015 D3）：think flush 残余 + converter
                        # finalize/extra + 终行兜底，见 _emit_closing_output
                        async for closing_sse in _emit_closing_output("done"):
                            yield closing_sse
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
                        raise ConverterError(f"Streaming chunk is not valid JSON: {data_str[:120]}") from None
                    sse = _format_raw_sse(upstream_event_type, data_str)
                    _log_stream_event(sse)
                    _mark_output()
                    yield sse
                    continue

                _record_chunk(chunk)
                _mark_first_token()

                if not isinstance(chunk, dict) and response_converter:
                    continue

                # M2: Anthropic 目标 + Anthropic 上游直通时，记录最近的 text 块 index，
                # 供 EOF flush 残余以合法 content_block_delta 输出
                if (
                    output_anthropic_sse
                    and isinstance(chunk, dict)
                    and chunk.get("type") == "content_block_delta"
                    and isinstance(chunk.get("index"), int)
                ):
                    last_text_delta_index = chunk["index"]

                upstream_error_detected = False
                if isinstance(chunk, dict) and (
                    is_upstream_anthropic
                    and (upstream_event_type == "error" or chunk.get("type") == "error")
                    or source_type == "openai-response"
                    and (upstream_event_type == "error" or chunk.get("type") == "error" or chunk.get("type") == "response.failed")
                    or source_type == "openai-chat-completions"
                    and isinstance(chunk.get("error"), dict)
                ):
                    upstream_error_detected = True

                if upstream_error_detected:
                    upstream_error = _UpstreamStreamErrorEvent(chunk)
                    # M10: 用 emitted_real_output 而非 emitted_output——仅收到心跳注释行
                    # 时仍可走首包前故障转移，不向客户端吐错
                    if not emitted_real_output:
                        # 首包前失败：预检异常解包后由接入点回退层记账，此处不留证据
                        stream_failure_handed_off = True
                        raise _StreamPreflightError(upstream_error)
                    stream_error = str(upstream_error)
                    # 只保留失败证据：kind 由 finally 调 classify_failure 单点判定（ADR-0014 D1）
                    stream_failure_evidence = upstream_error
                    # 先输出 error 事件本身，再输出协议终止事件
                    sse = _format_upstream_chunk_sse(chunk)
                    _log_stream_event(sse)
                    _mark_output()
                    yield sse
                    if stream_error:
                        async for terminal_sse in _emit_terminal_events_on_stream_error():
                            yield terminal_sse
                        break

                # 增量提取 token 用量和 finish_reason（ADR-0015 D1：usage 方言收敛到
                # stream_usage 累积器，避免 finally 中二次遍历；非 dict chunk 为 no-op）
                usage_acc.feed(chunk)

                if response_converter:
                    # Anthropic 上游 event type 的输入侧注入通道（ADR-0016 D0 保留）：
                    # to_chat / to_response 解析 Anthropic 源事件时读 _event_type 兜底
                    if is_upstream_anthropic and upstream_event_type is not None:
                        chunk = {**chunk, "_event_type": upstream_event_type}
                    try:
                        validate_stream_response_chunk(chunk, APIType(source_type), target_api_type)
                        converted_events = response_converter.convert_stream_chunk(chunk, source_type)
                    except IncompatibleResponseError:
                        raise
                    except Exception as conv_err:
                        logger.warning(f"流式 chunk 转换失败: {type(conv_err).__name__}: {conv_err}")
                        raise ConverterError(f"Streaming chunk conversion failed: {conv_err}") from conv_err
                    beat_types = [e.get("type") for e in converted_events if isinstance(e, dict)]
                    logger.debug(f"[CONVERT_CHUNK] events={len(converted_events)} types={beat_types[:3]}")
                    # 两拍协议（ADR-0016 D0）：拍 1 返回该拍全部事件，
                    # 单循环逐事件 think 过滤 → 格式化 → yield（M2 语义：无事件漏过滤）
                    for converted in converted_events:
                        # M2: 转换产物为 Anthropic content_block_delta 时记录块 index
                        # （EOF flush 复用；过滤前记录，语义与协议改造前一致）
                        if (
                            output_anthropic_sse
                            and isinstance(converted, dict)
                            and converted.get("type") == "content_block_delta"
                            and isinstance(converted.get("index"), int)
                        ):
                            last_text_delta_index = converted["index"]
                        converted = _apply_think_filter_to_event(converted, think_filter)
                        if converted is None:
                            continue
                        sse = _format_sse_for_list(converted, infer_event_type=output_sse_events)
                        _log_stream_event(sse)
                        _mark_output()
                        yield sse
                else:
                    chunk = _apply_think_filter_to_event(chunk, think_filter)
                    if chunk is None:
                        continue
                    sse = _format_upstream_chunk_sse(chunk)
                    _log_stream_event(sse)
                    _mark_output()
                    yield sse
                    if stream_error:
                        async for terminal_sse in _emit_terminal_events_on_stream_error():
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
        if stream_error is None and not _done_received and non_sse_stream_body is None and emitted_output:
            logger.warning(f"[STREAM EOF WITHOUT DONE] model={model} chunks={len(stream_chunks)} target={target_api_type.value}")
            # 收尾输出统一（ADR-0015 D3）：think flush 残余 + converter finalize/extra +
            # 终行兜底（EOF 阶段直通路径另补协议终止事件），见 _emit_closing_output
            async for closing_sse in _emit_closing_output("eof"):
                yield closing_sse
        if non_sse_stream_body is not None:
            logger.debug(f"[STREAM NON-SSE] model={model} body_length={len(non_sse_stream_body)}")
            # non-SSE JSON 兜底接缝（ADR-0015 D0 接缝 3）：整块解析、按目标格式拆事件、
            # usage/finish_reason 归一与解析失败的错误事件输出都在接缝内，
            # 主循环只保留"检测到非 SSE body → 调接缝"的分支。
            non_sse_usage = _NonSseUsageResult()
            async for sse in _emit_non_sse_fallback_events(
                non_sse_stream_body,
                model=model,
                source_type=source_type,
                response_converter=response_converter,
                output_anthropic_sse=output_anthropic_sse,
                output_responses_sse=output_responses_sse,
                output_sse_events=output_sse_events,
                is_upstream_anthropic=is_upstream_anthropic,
                think_filter=think_filter,
                cur_input_tokens=usage_acc.input_tokens,
                cur_output_tokens=usage_acc.output_tokens,
                cur_finish_reason=usage_acc.finish_reason,
                usage_out=non_sse_usage,
                record_chunk=_record_chunk,
                mark_first_token=_mark_first_token,
                mark_output=_mark_output,
                log_event=_log_stream_event,
                emit_error_events=_emit_error_events,
            ):
                yield sse
            if non_sse_usage.parse_failed:
                stream_error = "non_sse_json_parse_error"
                # 此处 success 已在流循环正常结束处被置 True，必须复位，否则 finally
                # 的 success 记账会把解析失败的请求记为成功。
                stream_success = False
            else:
                usage_acc.input_tokens = non_sse_usage.input_tokens
                usage_acc.output_tokens = non_sse_usage.output_tokens
                usage_acc.cache_read_input_tokens = non_sse_usage.cache_read_input_tokens
                usage_acc.cache_creation_input_tokens = non_sse_usage.cache_creation_input_tokens
                usage_acc.finish_reason = non_sse_usage.finish_reason

        if stream_error is None:
            stream_success = True
        logger.debug(
            f"[STREAM FINISH] model={model} done_received={_done_received} "
            f"non_sse_body={'yes' if non_sse_stream_body else 'no'} chunks={len(stream_chunks)}"
        )
        logger.debug(
            f"[STREAM COMPLETE] model={model} chunks={len(stream_chunks)} input_tokens={usage_acc.input_tokens} "
            f"output_tokens={usage_acc.output_tokens} finish_reason={usage_acc.finish_reason}"
        )
    except asyncio.CancelledError:
        cancelled = True
        stream_error = "client_disconnected_before_first_chunk" if not emitted_output else "client_disconnected_mid_stream"
        logger.warning(
            f"[STREAM CANCELLED] model={model} emitted={emitted_output} "
            f"chunks={len(stream_chunks)} first_token={first_token_time is not None} "
            f"error={stream_error}"
        )
        raise
    except GeneratorExit:
        # GeneratorExit 是 BaseException 子类，客户端断开连接时由生成器 close() 注入。
        # 必须显式捕获，否则会穿透到 finally 被误记为失败请求。
        cancelled = True
        stream_error = "client_disconnected_before_first_chunk" if not emitted_output else "client_disconnected_mid_stream"
        logger.warning(
            f"[STREAM GENERATOREXIT] model={model} emitted={emitted_output} "
            f"chunks={len(stream_chunks)} first_token={first_token_time is not None} "
            f"error={stream_error}"
        )
        raise
    except _StreamPreflightError:
        # M10: 首包前错误（如心跳后紧跟上游 error）直接透传，由外层
        # _raise_preflight_stream_errors / Channel Attempt 首包预检解包触发故障转移。
        # 不能落入 except Exception 被当成流中错误转成 error 事件吐给客户端。
        raise
    except Exception as e:
        if isinstance(e, IncompatibleResponseError):
            local_response_incompatible = True
        logger.error(f"[STREAM ERROR TRACEBACK] model={model} url={url}")
        logger.exception("[STREAM ERROR]")
        if isinstance(e, httpx.HTTPStatusError):
            try:
                await e.response.aread()
                stream_error_body = _extract_response_body(e.response)
            except Exception:
                stream_error_body = None
            stream_error = _format_error_msg(e, stream_error_body)
            err_body = _safe_response_text(e.response, 500)
            logger.error(f"[STREAM ERROR] upstream {e.response.status_code} {url} body={err_body}")
        else:
            stream_error = str(e)
            logger.error(f"[STREAM ERROR] {type(e).__name__}: {e} model={model} url={url}")

        if not emitted_output:
            # 首包前失败：预检包装解包后由接入点回退层按原始异常记账（同一失败不双记），
            # 此处不留失败证据、finally 不记账——与非流式回退层是同一记账约定的投影。
            stream_failure_handed_off = True
            if _is_retryable_exception(e) or _is_channel_config_error(e):
                raise _StreamPreflightError(e) from e
            raise
        # 只保留失败证据：kind 由 finally 调 classify_failure 单点判定（ADR-0014 D1）
        if not isinstance(e, IncompatibleResponseError):
            stream_failure_evidence = e
        stream_success = False
        emitted_output = True
        # 错误消息现状语义：Anthropic 目标用 str(e) 原文，Responses/Chat 目标加
        # "流式传输错误: " 前缀（golden 钉死）
        if isinstance(e, IncompatibleResponseError):
            stream_error = f"partial_response=true; {e}"
            err_msg = stream_error
            if conversion_info is not None:
                conversion_info["result"] = "rejected_response"
                conversion_info["partial_response"] = True
                conversion_info["response_diagnostic"] = {
                    "code": e.code,
                    "path": e.path,
                    "feature": e.diagnostic.feature,
                    "disposition": e.diagnostic.disposition.value,
                }
        else:
            err_msg = str(e) if output_anthropic_sse else f"Streaming transport error: {e}"
        async for evt in _emit_error_events(err_msg):
            yield evt
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
            logger.debug(
                f"[STREAM STATS] model={model} success={stream_success} error={stream_error} "
                f"latency={latency_ms}ms lag={lag_ms}ms chunks={len(stream_chunks)} "
                f"input={usage_acc.input_tokens} output={usage_acc.output_tokens} finish={usage_acc.finish_reason}"
            )
            _record_request(
                channel_id=channel.id,
                channel_name=channel.name,
                model=model,
                api_type=source_type,
                requested_model=requested_model,
                request_source=request_source,
                is_stream=True,
                input_tokens=usage_acc.input_tokens,
                output_tokens=usage_acc.output_tokens,
                cache_read_input_tokens=usage_acc.cache_read_input_tokens,
                cache_creation_input_tokens=usage_acc.cache_creation_input_tokens,
                latency_ms=latency_ms,
                lag_ms=lag_ms,
                success=stream_success,
                error_msg=stream_error,
                finish_reason=usage_acc.finish_reason,
                api_key_id=api_key_id,
                client_ip=client_ip,
                request_headers=headers,
                response_headers=resp_headers,
                request_body=upstream_data,
                response_body=response_body or stream_error_body,
                sensitivity_info=sensitivity_info,
                conversion_info=conversion_info,
                shaping_info=shaping_info,
            )
        except Exception as record_err:
            logger.warning(f"stream record request error: {record_err}")

        # 3. 记录负载均衡状态：kind 判定单一住所（ADR-0014 D1）——用保留的失败证据调
        #    classify_failure 一次定 kind（判不出明确档时分类函数兜底 transport_failure）。
        #    健康键 model 段推导唯一住所 outcomes.effective_model（ADR-0025 D0）。
        #    与非流式是同一记账约定的两个投影：首包前失败随预检异常由接入点回退层记账，
        #    此处不重复记（同一失败不双记）；流中失败由本 finally 单点记账。
        try:
            eff_model = outcomes.effective_model(model=model, requested_model=requested_model, channel_id=channel.id)
            if cancelled:
                outcomes.record(eff_model, channel.id, OutcomeKind.cancelled)
                logger.warning(f"[STREAM RECORDED CANCELLED] channel={channel.name} error={stream_error}")
            elif stream_failure_handed_off:
                logger.debug(f"[STREAM FAILURE HANDED OFF] channel={channel.name} error={stream_error}")
            elif local_response_incompatible:
                logger.debug(f"[STREAM LOCAL INCOMPATIBLE] channel={channel.name} error={stream_error}")
            elif stream_failure_evidence is not None:
                kind = classify_failure(stream_failure_evidence)
                outcomes.record(eff_model, channel.id, kind)
                logger.warning(f"[STREAM RECORDED FAILURE] channel={channel.name} kind={kind.value} error={stream_error}")
            elif stream_success:
                outcomes.record(eff_model, channel.id, OutcomeKind.success)
                logger.debug(f"[STREAM RECORDED SUCCESS] channel={channel.name}")
            else:
                # 无失败证据的执行器自有失败（如非 SSE body 解析失败）兜底 transport_failure
                kind = OutcomeKind.transport_failure
                outcomes.record(eff_model, channel.id, kind)
                logger.warning(f"[STREAM RECORDED FAILURE] channel={channel.name} kind={kind.value} error={stream_error}")
        except Exception as lb_err:
            logger.warning(f"stream load balancer record error: {lb_err}")

        # 4. 关闭流式客户端（GeneratorExit 路径可能挂起，需超时防护）
        try:
            await asyncio.wait_for(client.aclose(), timeout=2.0)
        except TimeoutError:
            logger.warning(f"[STREAM CLOSE TIMEOUT] model={model} channel={channel.name} client.aclose() timeout 2.0s")
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as e:
            logger.warning(f"close stream client error: {e}")


async def _raise_preflight_stream_errors(gen):
    has_yielded = False
    try:
        async for chunk in gen:
            # M10: 心跳注释行不算真实输出，不置 has_yielded——
            # 心跳后紧跟上游 error 时仍可包装为 _StreamPreflightError 触发故障转移
            if not _is_heartbeat_chunk(chunk):
                has_yielded = True
            yield chunk
    except _StreamPreflightError:
        # 已是首包前错误（如心跳后紧跟上游 error），直接透传给 Channel Attempt 解包
        raise
    except Exception as exc:
        if not has_yielded and (_is_retryable_exception(exc) or _is_channel_config_error(exc)):
            raise _StreamPreflightError(exc) from exc
        raise
    finally:
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            try:
                await asyncio.wait_for(aclose(), timeout=2.0)
            except TimeoutError:
                logger.warning("[STREAM PREFLIGHT ACLOSE TIMEOUT] gen.aclose() timeout 2.0s")
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as e:
                logger.warning(f"close preflight stream error: {e}")


__all__ = [
    "_do_stream_request",
    "_EmptyStreamError",
    "_filter_think_in_stream_chunk",
    "_is_stream_terminal_event_missing",
    "_raise_preflight_stream_errors",
    "_StreamPreflightError",
    "_UpstreamStreamErrorEvent",
]
