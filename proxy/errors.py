"""代理错误域：异常类 + 失败分类（OutcomeKind）单一住所。

「文件名即职责」——异常与错误分类是 ``errors`` 的职责。编排层
（:mod:`proxy.routing`）与流执行层从本站 imports 这些符号。

流执行异常 ``_StreamPreflightError`` / ``_UpstreamStreamErrorEvent`` / ``_EmptyStreamError``
的定义体全部迁入本域（ADR-0014 D0 迁入后两者；ADR-0015 D2 迁入首包预检包装异常），
:mod:`proxy.stream_executor` 保留同名 re-export 维持既有测试的 import / patch 路径。
错误域对流执行器零反向依赖，循环依赖在根部解除。

:func:`classify_failure` 是"异常/状态码 → OutcomeKind"的单一判定住所（纯函数，
ADR-0014 D0）：四处既有映射（routing 回退层 / 流执行器 kind 判定段 / 组探活
分类 / Responses 透传）后续统一改走它。依赖方向恢复单向：执行层 → 错误域 →
:mod:`proxy.outcomes`（outcomes 不反向依赖本域）。
"""

import json
from typing import Any

import httpx

from pii_errors import SensitiveBlockError as SensitiveBlockError
from proxy.outcomes import OutcomeKind
from rate_limiting import _is_rate_limit_exception

_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.TransportError,
    # M9: 上游 200 + 非 JSON body（HTML 错误页等）属协议违约，
    # 应故障转移到其他渠道而非直接向客户端吐错
    json.JSONDecodeError,
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


class _StreamPreflightError(Exception):
    """流式响应首个输出前失败，允许外层故障转移（ADR-0015 D2 自流执行器迁入）。"""

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


class _UpstreamStreamErrorEvent(Exception):
    """流内收到上游 error 事件（SSE error chunk 的异常包装，ADR-0014 D0 自流执行器迁入）。"""

    def __init__(self, event: dict[str, Any]):
        self.event = event
        error = event.get("error", {})
        super().__init__(error.get("message") or "upstream stream error")


class _EmptyStreamError(Exception):
    """上游流式响应为空（没有任何 SSE 输出），触发故障转移。"""


# 401/403/404：渠道配置级不自愈错误（key 失效 / 无权限 / 模型不存在）
_HTTP_4XX_CONFIG_CODES = frozenset({401, 403, 404})


def _classify_status_code(status_code: int) -> OutcomeKind:
    """状态码 → OutcomeKind 档位；未命中档位（含歧义 4xx）兜底 transport_failure。"""
    if status_code in _HTTP_4XX_CONFIG_CODES:
        return OutcomeKind.http_4xx_config
    if 500 <= status_code < 600:
        return OutcomeKind.http_5xx
    if status_code == 429:
        return OutcomeKind.http_429
    return OutcomeKind.transport_failure


def classify_failure(exc: BaseException | None = None, status: int | None = None) -> OutcomeKind:
    """失败分类单一接缝（ADR-0014 D0）：异常 / 状态码 → :class:`OutcomeKind`。

    纯函数：只做判定并返回富枚举——不记账、不抛出、不做调用期防御 import。
    语义表为四处现有映射（routing 回退层 / 流执行器 kind 判定段 / 组探活
    ``_classify_probe_error`` / Responses 透传 attempt_fn）的并集：

    - ``httpx.HTTPStatusError``：状态 ∈ {401, 403, 404} → ``http_4xx_config``；
      500 ≤ 状态 < 600 → ``http_5xx``；状态 = 429 → ``http_429``；
      其余状态（歧义 4xx 等）→ ``transport_failure``。
    - 流执行异常（``_EmptyStreamError`` / ``_UpstreamStreamErrorEvent``）→ ``transport_failure``。
    - 其余一切异常（``ConverterError``、超时、传输错误、JSON 解码失败、非 HTTP 异常）
      → ``transport_failure``。
    - ``status`` 是"无异常对象"路径（透传记账）的裸状态码兜底，仅在 ``exc`` 为
      None 时生效，命中上述状态档位才有效。

    ``RateLimitExceeded`` 刻意不进映射面：它永远在接入点回退层被重抛走限速分流
    （``rate_limiting.handle_rate_limit`` / dispatcher 429 预算），不会落到 kind
    判定——此处对它按兜底档返回 ``transport_failure``，不做任何特殊化。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return _classify_status_code(exc.response.status_code)
    if exc is not None:
        return OutcomeKind.transport_failure
    if status is not None:
        return _classify_status_code(status)
    return OutcomeKind.transport_failure


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, _UpstreamStreamErrorEvent):
        return True
    if isinstance(exc, _EmptyStreamError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return _is_rate_limit_exception(exc) or 500 <= status_code < 600
    if isinstance(exc, ConverterError):
        return True
    return isinstance(exc, _RETRYABLE_EXCEPTIONS)


def _is_channel_config_error(exc: BaseException) -> bool:
    """检查是否为渠道配置错误（如认证失败、路径错误等）"""
    # 延迟导入避免错误域在模块初始化时依赖目录存储；缺失/损坏 revision 或
    # 档案不支持 Endpoint 属于该 Channel 的局部配置错误，应允许调度器换渠道。
    from upstream_profile_resolver import ProfileResolutionError

    if isinstance(exc, ProfileResolutionError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403, 404)
    return False
