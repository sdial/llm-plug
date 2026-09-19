"""窗口级限速（额度耗尽型 429）检测与硬限制。

上游部分 CODING 计划的 429 是"额度窗口耗尽"（如方舟 5 小时用量窗口、7 天周窗口），
而非瞬时超速。这类 429 到恢复时刻之前重试同一渠道毫无意义：应快速失败并把
原始报错转发客户端，同时把渠道硬限制到恢复时刻。

检测采用"适配器 + 注册表"（见 quota_limits/vendors/），新增供应商步骤见
docs/quota-limits.md。
"""

import httpx

from quota_limits.models import QuotaLimitInfo
from quota_limits.store import store
from quota_limits.vendors import _ADAPTERS
from rate_limiting import RateLimitExceeded


def _safe_json_body(resp: httpx.Response):
    """best-effort 读取响应体：优先 JSON，否则原文。"""
    try:
        return resp.json()
    except Exception:
        try:
            return resp.text or None
        except Exception:
            return None


def detect_body(body) -> QuotaLimitInfo | None:
    """对原始错误 body 运行全部适配器，先命中先返回。"""
    if body is None:
        return None
    for _name, match in _ADAPTERS:
        info = match(body)
        if info is not None:
            return info
    return None


def detect_exception(exc: BaseException) -> QuotaLimitInfo | None:
    """从限速异常中提取 body 并检测。"""
    if isinstance(exc, RateLimitExceeded):
        # getattr 兜底：error_body 字段 Task 4 才加入构造器，之前可能未设置
        return detect_body(getattr(exc, "error_body", None))
    if isinstance(exc, httpx.HTTPStatusError):
        return detect_body(_safe_json_body(exc.response))
    return None


def mark_blocked(channel_id: str, reset_at, code: str) -> None:
    """把渠道硬限制到 reset_at（aware datetime）。"""
    store.mark_blocked(channel_id, reset_at, code)


def has_active_window(channel_id: str) -> bool:
    """渠道是否有未过期的窗口硬限制（reset_at 在未来）——持久化视图。

    准入语境下的 ``is_blocked``（内存实时视图）唯一指向 ``proxy.outcomes.is_blocked``，
    本包不再使用该名（ADR-0021 D3 同名异义拆除）。
    """
    return store.has_active_window(channel_id)


def unblock(channel_id: str) -> None:
    store.unblock(channel_id)


def load() -> None:
    store.load()


def cleanup(active_channel_ids: set[str]) -> None:
    store.cleanup(active_channel_ids)


def setup() -> None:
    """把 quota 写穿适配器挂到 outcomes：``kind=quota_window`` 事件触发 JSON 持久化。

    ``is_blocked`` 的内存判断由 outcomes 内部派生（事件驱动），LoadBalancer 选路
    直接读 ``outcomes.is_blocked``，不再需要旧的 ``set_blocked_checker`` 桥。
    """
    from proxy.outcomes import set_quota_adapter

    set_quota_adapter(_persist_blocked)


def _persist_blocked(channel_id: str, reset_at_unix: float, code: str) -> None:
    """quota_window 事件触发的写穿适配器：持久化到 data/channel_quota_limits.json。"""
    from datetime import UTC, datetime

    store.mark_blocked(channel_id, datetime.fromtimestamp(reset_at_unix, tz=UTC), code)
