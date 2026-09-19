"""上游限速策略深模块：429 分类、Retry-After 解析、发送前 RPM 限速与预算决策。

负责限速策略全链路：识别限速错误 → 解析重试建议 → 发送前按渠道 RPM 排队
→ 等待预算决策（预算内等待重试同一渠道 vs 预算耗尽转故障转移）。

底层滑动窗口限速器（``rate_limiter.SlidingWindowRateLimiter``）保持为基础设施，
不并入本策略模块；编排层（``proxy.routing`` 的两条 fallback 路径与 responses 路由）
只调用本模块的两个公开 seam：

- ``acquire_send_budget(channel, wait_timeout)``：发送前获取渠道 RPM 许可。
- ``handle_rate_limit(exc, channel, wait_budget, tried_ids)``：单次限速决策。
"""

import asyncio
import email.utils
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from models.channel import Channel
from proxy import outcomes
from proxy.outcomes import OutcomeKind
from rate_limiter import rate_limiter


class RateLimitExceeded(Exception):
    """上游限速（429）或代理侧发送队列等待超时。

    与普通上游错误不同：外层应先按 retry_after 等待后重试同一渠道，
    等待预算耗尽后才转故障转移 / 记失败。
    """

    def __init__(
        self,
        message: str,
        retry_after: float | None = None,
        waited: float = 0.0,
        queue_timeout: bool = False,
        error_body: Any = None,
        response: httpx.Response | None = None,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.waited = waited
        self.queue_timeout = queue_timeout
        self.error_body = error_body
        self.response = response


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头：支持秒数或 HTTP 日期。"""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def _is_rate_limit_exception(exc: BaseException) -> bool:
    """是否为限速类错误（429 / 发送排队超时），应等待重试而非立即故障转移。"""
    if isinstance(exc, RateLimitExceeded):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return False


def _rate_limit_retry_after(exc: BaseException) -> float | None:
    """提取限速错误建议的重试等待秒数（无建议时返回 None）。"""
    if isinstance(exc, RateLimitExceeded):
        return exc.retry_after
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return _parse_retry_after(exc.response.headers.get("retry-after"))
    return None


async def acquire_send_budget(channel: Channel, wait_timeout: float) -> None:
    """请求发送前按渠道 RPM 限速。

    无额度时在 wait_timeout 内排队等待；超时抛 RateLimitExceeded，
    由外层按限速重试逻辑处理（预算耗尽则转故障转移）。
    """
    rpm = getattr(channel, "rate_limit_rpm", None) or 0
    if rpm <= 0:
        return
    start = time.monotonic()
    ok = await rate_limiter.acquire(channel.id, rpm, window_seconds=60.0, wait_timeout=wait_timeout)
    if not ok:
        raise RateLimitExceeded(
            f"上游限速队列等待超时（RPM={rpm}，等待 {time.monotonic() - start:.1f}s 未获额度）",
            waited=time.monotonic() - start,
            queue_timeout=True,
        )


async def handle_rate_limit(
    exc: BaseException,
    selected: Channel,
    wait_budget: float,
    tried_ids: set[str],
    model: str | None = None,
) -> tuple[float, bool]:
    """处理单次限速异常，返回 (更新后的预算, 是否已转入故障转移)。

    - 队列等待超时 / 预算耗尽：记渠道失败并加入 tried_ids，转入故障转移。
    - 预算足够：按 Retry-After 或礼貌延迟休眠，保持同一渠道继续重试。

    model: 真实请求模型（组场景取 entry.model，非组取 requested_model），用于
        outcomes 记账；为 None 时（探活等调用面）跳过 outcomes 记账——沿用
        退场前兼容外壳的「无键跳过」语义，键语义不变（不在 ADR-0025 D1 判决
        节兜底照记范围内）。
    """
    waited = getattr(exc, "waited", 0.0)
    if waited > 0 or getattr(exc, "queue_timeout", False):
        wait_budget = max(0.0, wait_budget - waited)
        _record_rate_limit_exhausted(selected, model)
        tried_ids.add(selected.id)
        return wait_budget, True

    delay = _rate_limit_retry_after(exc)
    # Retry-After: 0（或解析为过去的日期）会令预算恒不减少、同渠道无限重试：
    # 取下限 0.1s，保证每次等待都消耗预算，预算耗尽即转故障转移
    delay = 1.0 if delay is None else max(delay, 0.1)
    if wait_budget >= delay:
        wait_budget -= delay
        logger.warning(f"[RATE LIMIT] 渠道={selected.name} 被上游限速，等待 {delay:.1f}s 后重试（剩余预算 {wait_budget:.1f}s）")
        await asyncio.sleep(delay)
        return wait_budget, False

    _record_rate_limit_exhausted(selected, model)
    tried_ids.add(selected.id)
    return wait_budget, True


def _record_rate_limit_exhausted(selected: Channel, model: str | None) -> None:
    """限速预算耗尽记账：直连 outcomes（ADR-0025 D0，LB 兼容外壳已退场）。

    model 为 None（探活等调用面）沿用旧外壳「无键跳过」语义，键语义不变。
    """
    if not model:
        return
    outcomes.record(model, selected.id, OutcomeKind.rate_limit_exhausted)
