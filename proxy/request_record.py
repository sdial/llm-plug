"""请求日志落库组装单一住所（ADR-0014 D2）。

既有四个 ``_record_request`` 调用点（非流式 PII 拦截 / 失败 / 成功 + 流式 finally）
收敛到本模块的一个组装函数：kwarg 集中（显式签名，废除 ``**kwargs`` + pop 约定）、
headers 脱敏全仓唯一一处、聚合字段与原始字段在此分发——聚合字段投递 stats 与
request_logs 两个后端；原始字段（headers/body）与仅日志侧维度
（requested_model / api_type / sensitivity_info）只投递 request_logs。
新增落库字段只改本函数一处。

依赖方向：proxy 执行器 → 本模块 → 顶层持久化后端（stats / request_logs），
后端不反向依赖 proxy 包，无循环 import。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import request_logs
import stats

# 敏感头：请求日志侧统一脱敏（凭证不落月度库）
_SENSITIVE_HEADERS = frozenset({"authorization", "x-api-key", "cookie", "set-cookie"})


def _redact_sensitive_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    """剔除敏感头（authorization / x-api-key，大小写不敏感）；全仓唯一的 headers 脱敏住所。"""
    if headers is None:
        return None
    return {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}


def record_request(
    *,
    channel_id: str,
    channel_name: str,
    model: str,
    is_stream: bool,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    success: bool,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    error_msg: str | None = None,
    finish_reason: str | None = None,
    lag_ms: int | None = None,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    request_source: str = "client",
    # 仅日志侧维度：stats 聚合口径不按这些维度拆分（requested_model / api_type 避免
    # 计费行变化；sensitivity_info 是 PII 过滤详情，随请求日志落月度库）。
    # request_source 是统计与日志共用的来源维度（ADR-0009），作为显式签名参数
    # 钉住双后端透传约定——它不属于任何"仅日志侧"名单，两侧都要入账。
    requested_model: str | None = None,
    api_type: str | None = None,
    sensitivity_info: dict[str, Any] | None = None,
    conversion_info: dict[str, Any] | None = None,
    shaping_info: dict[str, Any] | None = None,
    # 原始字段：只投递 request_logs（stats 落库口径不含请求原文）；request_headers
    # 在本函数内统一脱敏，调用方传原始头即可。
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    request_body: Any | None = None,
    response_body: Any | None = None,
) -> None:
    """组装一条请求记录并分发到 stats / request_logs 两个后端（入队，由后台 worker 写入）。"""
    # Request Reference 是跨两套独立写穿队列的领域关联键；不能复用任一库的
    # 自增 id，也不能复用各队列溢出重放所需的 _write_id。
    event_timestamp = datetime.now(UTC).replace(tzinfo=None)
    shared = {
        "request_ref": uuid4().hex,
        "timestamp": event_timestamp,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "model": model,
        "is_stream": is_stream,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "latency_ms": latency_ms,
        "success": success,
        "error_msg": error_msg,
        "api_key_id": api_key_id,
        "client_ip": client_ip,
        "lag_ms": lag_ms,
        "finish_reason": finish_reason,
        "request_source": request_source,
    }
    stats.record_request(**shared)
    request_logs.record_request(
        **shared,
        requested_model=requested_model,
        api_type=api_type,
        sensitivity_info=sensitivity_info,
        conversion_info=conversion_info,
        shaping_info=shaping_info,
        request_headers=_redact_sensitive_headers(request_headers),
        response_headers=response_headers,
        request_body=request_body,
        response_body=response_body,
    )
