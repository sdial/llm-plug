"""唯一调度器（ADR-0011）——三份 select→attempt→429预算→tried 循环的收敛住所。

收敛自 ``proxy/routing.py`` / ``routers/proxy_response.py`` 的三份重试循环：
单模型 LB 选路循环、模型组纯模型分支（惰性首选 + 降级让位）、Responses 透传
端点循环。本模块是唯一知晓"把一条候选渠道换到下一条"的地方：

- ``dispatch``：候选池 → 选路 → 尝试 → 429 预算 → 排除集 的唯一循环；
- ``dispatch_pinned``（ADR-0021 D1）：锁定调度一等入口——与 ``dispatch`` 共享
  同一循环内核，只把「从候选池选」替换为「取 pinned 渠道过准入」；
- ``DispatchContext``：跨调度单元（模型组跨条目）共享的重试状态；
- ``_handle_rate_limit_or_fast_fail``：429 分流（窗口级快速失败 / 瞬时限速预算）；
- ``ChannelAttemptExhausted``：单渠道尝试穷尽信号。

调用方职责：决定候选池内容与顺序、用闭包构造 ``attempt_fn``（捕获请求上下文）、
处理 ``AllChannelsExhausted`` 的最终错误映射。「预算内重试同一渠道」是
``dispatch_pinned`` 的接口承诺——禁止再造「单候选池」惯用法（CONTEXT.md
Pinned Dispatch 词条）。候选形态 / 能力过滤时机留给 Phase 3（ADR-0012）。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

import config
import quota_limits
from models.channel import Channel
from proxy import outcomes
from proxy.channel_attempt import ChannelAttemptExhausted
from proxy.errors import AllChannelsExhausted
from proxy.outcomes import OutcomeKind
from rate_limiting import (
    RateLimitExceeded,
    _is_rate_limit_exception,
    handle_rate_limit,
)


def _to_upstream_http_status_error(exc: BaseException) -> BaseException:
    """窗口级限速：重新抛原始上游 429，供路由层原样透传（含 Retry-After 头）。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc
    if isinstance(exc, RateLimitExceeded) and exc.response is not None:
        return httpx.HTTPStatusError(str(exc), request=exc.response.request, response=exc.response)
    return exc


async def _handle_rate_limit_or_fast_fail(
    exc: BaseException,
    selected: Channel,
    wait_budget: float,
    tried_ids: set[str],
    model: str | None = None,
) -> tuple[float, bool]:
    """429 分流：窗口级限速 → 硬限制 + 抛原始 429 快速失败；否则走瞬时限速预算逻辑。

    命中窗口级限速时直接 raise（不进入故障转移、不记失败），但需记 quota_window 事件。
    """
    info = quota_limits.detect_exception(exc)
    if info is not None:
        # 记账：窗口级限速 → quota_window（内存视图与 JSON 写穿均由 outcomes 事件驱动）
        try:
            reset_ts = info.reset_at.timestamp() if info.reset_at else time.time() + 3600
            outcomes.record(
                model or selected.id,
                selected.id,
                OutcomeKind.quota_window,
                reset_at=reset_ts,
                code=info.code or "",
            )
        except Exception:
            pass
        reset_txt = info.reset_at.isoformat() if info.reset_at else "未知"
        logger.warning(f"[QUOTA LIMIT] 渠道={selected.name} 窗口级限速 code={info.code} reset_at={reset_txt}，快速失败透传原始 429")
        raise _to_upstream_http_status_error(exc)
    return await handle_rate_limit(exc, selected, wait_budget, tried_ids, model=model)


@dataclass
class DispatchContext:
    """跨调度单元共享的重试状态（ADR-0011 D1）。

    - ``tried``: 已尝试过（接入点穷尽 / 限速预算耗尽）的渠道 id 排除集。
    - ``wait_budget``: 429 等待预算（秒），与发送前 RPM 排队共用，预算耗尽才转故障转移。
    - ``last_error``: 最近一次可重试失败的原因，穷尽时随 ``AllChannelsExhausted`` 透传。

    模型组函数创建一个共享实例传入各条目的 ``dispatch`` 调用，保留跨条目共享的
    ``tried_channels`` 与 ``wait_budget``；单模型 / 透传端点不传，dispatcher 内部新建。
    """

    tried: set[str] = field(default_factory=set)
    wait_budget: float = field(default_factory=lambda: float(config.get_setting("rate_limit_wait_seconds") or 0))
    last_error: BaseException | None = None


def _materialize(candidates) -> list[Channel]:
    """把候选池物化为列表：支持 list / tuple / 同步可迭代。"""
    return list(candidates)


def _raise_exhausted(model: str | None, group_label: str | None, ctx: DispatchContext) -> None:
    """穷尽异常（dispatch / dispatch_pinned 共享）：last_error 存在时随异常透传并链因。"""
    if ctx.last_error is not None:
        raise AllChannelsExhausted(
            f"候选渠道已穷尽: model={model}, group={group_label}, last_error={ctx.last_error}",
            last_error=ctx.last_error,
        ) from ctx.last_error
    raise AllChannelsExhausted(f"候选渠道已穷尽: model={model}, group={group_label}")


async def _dispatch_loop(
    select_fn: Callable[[], Any],
    attempt_fn: Callable[[Channel, float], Any],
    *,
    model: str | None,
    group,
    ctx: DispatchContext,
    yield_predicate: Callable[[Channel], bool] | None,
) -> tuple[Any, Channel]:
    """唯一循环内核（ADR-0021 D1）：选择→尝试→429预算→排除。

    ``select_fn``：零参异步选择函数，返回本轮候选渠道或 None（穷尽）——
    ``dispatch`` 以候选池 + LB 选路实现，``dispatch_pinned`` 以 pinned 渠道过
    准入实现，其余语义（排除集、让位、429 预算分流、穷尽异常）逐字一致。
    """
    group_label = getattr(group, "name", None) or (group.id if getattr(group, "id", None) else model)

    while True:
        selected = await select_fn()
        if selected is None:
            _raise_exhausted(model, group_label, ctx)
        if yield_predicate is not None and not yield_predicate(selected):
            # 让位（软门禁，独立于准入）：计入排除集，下轮选择自动跳过
            ctx.tried.add(selected.id)
            continue
        try:
            result, served_channel = await attempt_fn(selected, ctx.wait_budget)
            return result, served_channel
        except ChannelAttemptExhausted as exhausted:
            # 渠道内接入点已穷尽（失败计数已逐次记录）：排除渠道换下一候选
            ctx.last_error = exhausted.cause
            ctx.tried.add(selected.id)
        except Exception as exc:
            if _is_rate_limit_exception(exc):
                # 429 分流：窗口级限速快速失败；瞬时限速预算内等待后重试，
                # 预算耗尽转故障转移（handle_rate_limit 已记账并加入排除集）
                ctx.wait_budget, did_failover = await _handle_rate_limit_or_fast_fail(exc, selected, ctx.wait_budget, ctx.tried, model=model)
                if did_failover:
                    ctx.last_error = exc
                # did_failover=False：预算等待后回到 loop 顶部重新选——pinned 入口
                # 按准入重取同一渠道（预算内重试同一渠道），候选池入口按 SWRR 轮换
            else:
                raise


async def dispatch(
    candidates,
    attempt_fn: Callable[[Channel, float], Any],
    *,
    model: str | None,
    group=None,
    context: DispatchContext | None = None,
    yield_predicate: Callable[[Channel], bool] | None = None,
    client_ip: str | None = None,
    api_key_id: str | None = None,
    client_headers: dict[str, str] | None = None,
) -> tuple[Any, Channel]:
    """对给定候选渠道池执行唯一的 选择→尝试→429预算→排除 循环。

    Args:
        candidates: 本轮候选渠道池（list / 同步或异步产生器）。
        attempt_fn: ``async (channel, wait_budget) -> (result, served_channel)``；
            失败抛 ``ChannelAttemptExhausted``（渠道尝试穷尽）或限速类异常（429）。
        model: 真实请求模型（组场景取 entry.model，非组取 requested_model），
            作健康键并用于 outcomes 记账；None 时不参与 select_channel 健康门禁
            （探活复用面：对降级对探测时依赖 attempt_fn 闭包内透传真实模型）。
        group: 所属模型组（可为 None），仅用于错误上下文。
        context: 跨调度单元共享的重试状态；None 时内部新建。
        yield_predicate: 选路后、尝试前的让位判定；返回 False 的渠道计入排除集跳过。

    Returns:
        ``(result, served_channel)``：``result`` 为 attempt_fn 的返回结果。

    Raises:
        AllChannelsExhausted: 全部候选耗尽；``last_error`` 携带最近一次失败原因。
        429 窗口级快速失败原样抛 ``HTTPStatusError``（不进入故障转移）。
    """
    from balancer.load_balancer import load_balancer

    ctx = context if context is not None else DispatchContext()
    pool = _materialize(candidates)

    async def _select_from_pool() -> Channel | None:
        return await load_balancer.select_channel(
            pool,
            exclude_ids=ctx.tried,
            model=model,
            client_ip=client_ip,
            api_key_id=api_key_id,
            client_headers=client_headers,
        )

    return await _dispatch_loop(
        _select_from_pool,
        attempt_fn,
        model=model,
        group=group,
        ctx=ctx,
        yield_predicate=yield_predicate,
    )


async def dispatch_pinned(
    channel: Channel,
    attempt_fn: Callable[[Channel, float], Any],
    *,
    model: str | None,
    group=None,
    context: DispatchContext | None = None,
    yield_predicate: Callable[[Channel], bool] | None = None,
    admission: bool = True,
    client_ip: str | None = None,
    api_key_id: str | None = None,
    client_headers: dict[str, str] | None = None,
) -> tuple[Any, Channel]:
    """锁定调度（Pinned Dispatch，ADR-0021 D1）：对单一渠道执行与 :func:`dispatch`
    相同的 选择→尝试→429预算→排除 循环——pinned 只是把「从候选池选」替换为
    「取 pinned 渠道过准入」，其余语义（排除集、让位、429 预算分流、穷尽异常）逐字一致。

    Args:
        channel: 锁定渠道。粘滞首选 / Responses 透传 / 组探活 / 硬绑定条目
            的共享入口；禁止再造「单候选池」惯用法（CONTEXT.md Pinned Dispatch 词条）。
        attempt_fn: 与 :func:`dispatch` 同约。
        model: 真实请求模型；None 时跳过健康门（探活复用语义，接口承诺——与
            :func:`dispatch` 的 model=None 一致），其余准入门仍生效。
        group: 所属模型组（可为 None），仅用于错误上下文。
        context: 跨调度单元共享的重试状态；None 时内部新建。
        yield_predicate: 尝试前的让位判定；返回 False 的渠道计入排除集，
            下轮准入不过即穷尽。
        admission: 准入档位。``True``（标准档）：过 :func:`proxy.outcomes.admits`
            （enabled / 排除集 / blocked / 健康四门）。``False``（仅启用档）：只查
            ``channel.enabled``——硬绑定条目专用，保持 ADR-0011 D2 的「快速失败
            透传 429，而非受健康/阻塞门静默跳过」语义。
        client_ip / api_key_id / client_headers: 与 :func:`dispatch` 签名对齐；
            锁定选择不经 LB（无会话指纹消费），仅为两入口调用形态一致保留。

    Returns:
        ``(result, served_channel)``：``result`` 为 attempt_fn 的返回结果。

    Raises:
        AllChannelsExhausted: pinned 渠道准入不过或穷尽（``last_error`` 携带最近
            一次失败原因；准入即拒时为 None）——由调用方决定下一步（粘滞首选
            落回普通 LB、硬绑定跳下一条目）。
        429 窗口级快速失败原样抛 ``HTTPStatusError``（不进入故障转移）。
    """
    ctx = context if context is not None else DispatchContext()

    async def _select_pinned() -> Channel | None:
        # 排除集语义两档一致（ADR-0021 D1「其余语义逐字一致」）：穷尽 / 429 预算
        # 耗尽后渠道已入 tried，不再重取——否则仅启用档会无限重试同一渠道
        if channel.id in ctx.tried:
            return None
        if admission:
            return channel if outcomes.admits(channel, model, exclude_ids=ctx.tried) else None
        # 仅启用档：硬绑定条目专用（ADR-0021 D2 第四段）——只查 enabled，
        # 不查 blocked / 健康门（ADR-0011 D2 快速失败透传 429 语义）
        return channel if channel.enabled else None

    return await _dispatch_loop(
        _select_pinned,
        attempt_fn,
        model=model,
        group=group,
        ctx=ctx,
        yield_predicate=yield_predicate,
    )
