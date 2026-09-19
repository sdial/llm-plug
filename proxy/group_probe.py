"""组内主动探活（ADR-0010）— 枚举（Ticket 06）+ 单轮驱动（Ticket 07）+ 成功写回（Ticket 08）+ 后台循环（Ticket 09）。

枚举侧（``take_probe_candidates``）：工作清单 = 降级视图交集（ADR-0010 D1），
每轮枚举当前全部 ``lazy_sticky=true`` 且启用的模型组的**纯模型条目**，把
``entry.model`` 展开为该模型可用的渠道候选（``get_channels_for_model`` 已排除
禁用渠道，再过 ``filter_channels_by_conversion`` 转换格式门控——探活请求以
OpenAI Chat 形态发送，ADR-0010 D3），与降级视图 ``outcomes.probe_targets()``
取交集，剔除窗口硬限制（``is_blocked``）对；按 ``(model, channel)`` 粒度跨组
去重（共享对只探一次），携带该对所属的全部 lazy 组（供成功写回
``remember_preferred``，D7）与行级退避信息（``first_failed_at /
consecutive_failures``）；``requested_model`` 取首个枚举到该对的组名
（first-wins，仅作日志辨识）。硬绑定条目不产出目标：其选路不经健康门控、不参与
粘滞记忆，探活对其无回切收益。严格零记账：不调用 ``outcomes.record``、不写任何
视图（退避未到期对的跳过在读侧完成，D8）。

驱动侧（``run_probe_round``）：对一轮候选并发探测，每个目标以最小流式请求
（``{model, messages, max_tokens:5, stream:true}``）走与业务完全相同的发送路径
（D3）：dispatcher 锁定调度 ``dispatch_pinned``（model=None 跳健康门为接口承诺，
ADR-0021 D1）+ ``DispatchContext(wait_budget=0.0)``（探活不占业务限速等待预算）。成功判据 = 流消费到底正常收尾（D2，比 ADR 原文"prime 然后
aclose"更严格——提前 aclose 会注入 GeneratorExit 记成无责 cancelled 而永不
解冻）；``record(success)`` 由发送链既有 finally 完成，探活层零特殊化记账。
超时 = 劣化证据（D4）：单目标以 ``asyncio.timeout`` 包裹，超时显式
``outcomes.record(transport_failure)``（发送链可能只记过 cancelled——渠道无责
空操作——必须显式补记，否则挂起渠道以 cancelled 全速反复被打、退避永不生效）。
失败经既有链路如实记账（真实 kind），仅压制该对；单目标异常隔离、互不拖累。

写回侧（``reclaim_sticky_preferred`` / ``_converge_writes``）：本轮成功即对其所属
**每个** lazy 组 ``remember_preferred(g, m, ch)`` 抢回粘滞记忆（D7——主恢复 ≤60s
自动回切），失败/跳过零接触 ``_preferred``。同轮收敛按 ``(group, model)`` 键取
items 序最小位置者（该位置为模型级首个条目、同模型渠道恒相等，平局按
channel_id 字典序定胜负），跨组共享对只探一次、写回各自组键。只调
``remember_preferred``，不再新增任何 ``outcomes.record``（成功已由发送链记且仅记一次）。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

import config
from channel_catalog import catalog
from models.api_types import APIType
from models.channel import Channel
from models.model_group import ModelGroup
from proxy import outcomes
from proxy.conversion import filter_channels_by_conversion
from proxy.errors import AllChannelsExhausted, classify_failure
from proxy.outcomes import OutcomeKind
from rate_limiting import _is_rate_limit_exception

# 退避封顶（秒）：interval × 2^k 的硬上限，长期死对稳态成本 ≤ 6 req/h（ADR-0010 D8）。
_BACKOFF_CAP_SECONDS = 600.0
_DEFAULT_INTERVAL_SECONDS = 60.0

# 循环睡眠下限守卫（秒）：配置误配极短间隔时，空枚举/空轮不会退化成热循环。
_MIN_LOOP_INTERVAL_SECONDS = 1.0

# 探活节拍默认值（无配置时回退；配置项见 config._CONFIG_SCHEMA group_probe_*）。
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_CONCURRENCY = 5

# 单目标探测结果的非 failure 类 kind（失败类直接复用 OutcomeKind.value）。
_KIND_SUCCESS = "success"
_KIND_SKIPPED = "skipped"
_KIND_ERROR = "error"


@dataclass(frozen=True)
class ProbeGroup:
    """探活目标的一个所属 lazy 组（供成功写回 ``remember_preferred``）。

    ``group`` 携带完整组对象，后续写回逻辑可在含该模型的所有组间收敛选路。
    """

    id: str
    name: str
    group: ModelGroup


@dataclass(frozen=True)
class ProbeCandidate:
    """探活目标：降级 (model, channel) 对 + 所属 lazy 组 + 行级退避信息。

    ``permanent=True`` 的对不产出（ADR-0010 D5），字段仅作驱动层公约保留；
    ``groups`` 按组枚举序排列（确定序），``requested_model`` 为首个枚举到
    该对的组名（first-wins）。
    """

    model: str
    channel_id: str
    first_failed_at: float
    consecutive_failures: int
    permanent: bool
    groups: list[ProbeGroup]
    requested_model: str
    next_due_at: float


def _next_due_at(first_failed_at: float, consecutive_failures: int, interval_seconds: float) -> float:
    """退避到期时刻：``first_failed_at + min(interval × 2^k, 600)``，

    k = ``max(0, consecutive_failures - 1)``——首次失败一间隔后开探（主恢复 ≤
    探活间隔自动回切），此后每次后续失败翻倍，封顶 600s（ADR-0010 D8）。
    """
    k = max(0, consecutive_failures - 1)
    return first_failed_at + min(interval_seconds * (2**k), _BACKOFF_CAP_SECONDS)


async def take_probe_candidates(
    *,
    now: float | None = None,
    interval_seconds: float | None = None,
) -> list[ProbeCandidate]:
    """纯读枚举本轮回切探活目标清单（ADR-0010 D1 / D8 读侧 / D12-3）。

    Args:
        now: 时钟注入点（测试确定性驱动；缺省 ``time.time()``）。
        interval_seconds: 探活间隔（测试注入；缺省热读
            ``group_probe_interval_seconds`` 配置，无配置回退 60）。

    Returns:
        降级 ``(model, channel)`` 对中尚未进入退避静默期的探活目标，按组枚举序、
        跨组去重后返回；健康对 / 硬绑定条目 / 窗口硬限制渠道 / ``permanent`` 对 /
        退避未到期对均不产出。无任何 ``record`` 副作用。
    """
    if now is None:
        now = time.time()
    if interval_seconds is None:
        interval_seconds = float(config.get_setting("group_probe_interval_seconds") or _DEFAULT_INTERVAL_SECONDS)

    degraded = {(t.model, t.channel_id): t for t in outcomes.probe_targets()}
    if not degraded:
        return []

    by_pair: dict[tuple[str, str], list[ProbeGroup]] = {}
    for group in await catalog.model_groups():
        if not group.enabled or not group.lazy_sticky:
            continue
        for entry in group.items:
            if entry.channel_id is not None:
                continue
            channels = filter_channels_by_conversion(await catalog.channels_for_model(entry.model), APIType.OPENAI_CHAT)
            for ch in channels:
                key = (entry.model, ch.id)
                row = degraded.get(key)
                if row is None or outcomes.is_blocked(ch.id):
                    continue
                owning = by_pair.get(key)
                probe_group = ProbeGroup(id=group.id, name=group.name, group=group)
                if owning is None:
                    by_pair[key] = [probe_group]
                elif owning[-1].id != group.id:
                    owning.append(probe_group)

    candidates: list[ProbeCandidate] = []
    for (model, channel_id), owning in by_pair.items():
        row = degraded[(model, channel_id)]
        next_due_at = _next_due_at(row.first_failed_at, row.consecutive_failures, interval_seconds)
        if now < next_due_at:
            continue
        candidates.append(
            ProbeCandidate(
                model=model,
                channel_id=channel_id,
                first_failed_at=row.first_failed_at,
                consecutive_failures=row.consecutive_failures,
                permanent=row.permanent,
                groups=owning,
                requested_model=owning[0].name,
                next_due_at=next_due_at,
            )
        )
    return candidates


@dataclass(frozen=True)
class ProbeTargetResult:
    """单目标本轮探测结果（供 Ticket 08 成功写回收敛）。

    ``kind`` 为 ``success / skipped / error`` 或失败类 ``OutcomeKind.value``。
    ``groups / requested_model`` 透传候选的所属 lazy 组与组名，供成功后对每个
    组 ``remember_preferred`` 抢回粘滞记忆（D7）。
    """

    model: str
    channel_id: str
    kind: str
    groups: tuple[ProbeGroup, ...]
    requested_model: str


@dataclass(frozen=True)
class ProbeRoundResult:
    """单轮探活结果：成功 / 失败 / 跳过的目标（Ticket 07 产出、08 消费）。

    只携带本轮成功（解冻）对供写回，不重新读取视图推算"未冻结"信息。
    """

    succeeded: tuple[ProbeTargetResult, ...]
    failed: tuple[ProbeTargetResult, ...]
    skipped: tuple[ProbeTargetResult, ...]

    @property
    def succeeded_pairs(self) -> set[tuple[str, str]]:
        """本轮成功（流正常收尾）的 (model, channel_id) 对集合。"""
        return {(r.model, r.channel_id) for r in self.succeeded}


def _items_position_for_model(group: ModelGroup, model: str) -> int:
    """(group, model) 的 items 锚点位置：首个含该模型的条目标引（收敛「回到主」依据）。

    ``group.items`` 未含该模型（测试手搭结果等）时回退 ``len(group.items)``：
    末尾哨兵，确定性且对一切真实条目构建的位置劣后（仍写入记忆，只是无锚点排名）。
    """
    for idx, entry in enumerate(group.items):
        if entry.model == model:
            return idx
    return len(group.items)


def _converge_writes(result: ProbeRoundResult) -> list[tuple[str, str, str]]:
    """成功目标同轮收敛的写回计划（D7/ADR-0010）：有序 ``(group_id, model, channel_id)``。

    按组迭代序 × items 序扫描：对每个成功目标遍历其所属 ``groups``（组枚举序），
    以 ``(group.id, model)`` 为键（粘滞记忆键形）收敛——同组同模型多个渠道成功时
    取 items 位置最小者；该位置是模型级首个条目、同模型各渠道恒相等，故平局按
    channel_id 字典序定胜负（确定性「回到主」）。输出按 ``(group, model)`` 首见
    顺序排列（等价 ``remember_preferred`` 的调用计划）。失败/跳过目标不参与。
    """
    best: dict[tuple[str, str], tuple[int, str]] = {}
    order: list[tuple[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for target in result.succeeded:
        for pg in target.groups:
            triple = (pg.id, target.model, target.channel_id)
            if triple in seen:
                continue
            seen.add(triple)
            key = (pg.id, target.model)
            pos = _items_position_for_model(pg.group, target.model)
            current = best.get(key)
            if key not in best:
                order.append(key)
            if current is None or pos < current[0] or (pos == current[0] and target.channel_id < current[1]):
                best[key] = (pos, target.channel_id)
    return [(group_id, model, best[(group_id, model)][1]) for group_id, model in order]


def reclaim_sticky_preferred(result: ProbeRoundResult) -> None:
    """探活成功后抢回每个含该模型的 lazy 组粘滞记忆（D7 成功写回，独立可测）。

    对 ``_converge_writes`` 的写回计划逐条 ``outcomes.remember_preferred``：
    同轮多对成功收敛到 items 序最靠前渠道（回到主）、跨组共享对写回各自组键。
    零记账：只调 ``remember_preferred``，不产生任何 ``outcomes.record``。
    """
    for group_id, model, channel_id in _converge_writes(result):
        outcomes.remember_preferred(group_id, model, channel_id)


# 最小流式探活请求体（ADR-0010 D3）；每次尝试重建 messages 列表，
# 避免上游 capability 过滤等就地改动污染后续尝试。
def _build_probe_request(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "stream": True,
    }


def _classify_probe_error(exc: BaseException) -> str:
    """把驱动层兜底捕获的异常映射为结果 kind（失败记账已由既有链路完成）。

    kind 统一由 :func:`proxy.errors.classify_failure` 产出（ADR-0014 D0），
    只含 ``OutcomeKind`` 词汇表内的值——词汇表外的 ``http_{code}`` /
    ``exhausted`` 自造标签消失，非 401/403/404 的歧义 4xx 归
    ``transport_failure``（spec 仅有的可观察微调之一，分桶不变）。
    ``AllChannelsExhausted`` 解包 ``last_error`` 后分类（无 last_error 时按
    非 HTTP 异常兜底）；限速类异常在分类函数映射面之外（spec），显式保留
    ``http_429`` 结果标签。
    """
    inner = exc
    while isinstance(inner, AllChannelsExhausted) and inner.last_error is not None:
        inner = inner.last_error
    if _is_rate_limit_exception(inner):
        return OutcomeKind.http_429.value
    return classify_failure(inner).value


async def _probe_one_target(target: ProbeCandidate, *, timeout: float) -> ProbeTargetResult:
    """单目标探测：复用业务发送路径（锁定调度 dispatch_pinned）+ asyncio.timeout 包裹。

    成功判据 = 流消费到底正常收尾；``record(success)`` 由发送链既有 finally 完成
    （D2 偏差）。超时显式记 ``transport_failure``（D4）。异常隔离：本函数兜住
    一切异常并映射为失败结果，不向上传播。
    """
    from proxy.channel_attempt import ChannelAttemptInput, StreamAttemptResult, attempt_channel
    from proxy.dispatcher import DispatchContext
    from proxy.dispatcher import dispatch_pinned as _dispatch_pinned

    pool = filter_channels_by_conversion(await catalog.channels_for_model(target.model), APIType.OPENAI_CHAT)
    channel = next((ch for ch in pool if ch.id == target.channel_id), None)
    if channel is None:
        logger.warning(f"[GROUP PROBE] 目标渠道 {target.channel_id} 已删除/禁用/被格式门控排除，剪枝跳过 model={target.model}")
        return ProbeTargetResult(
            model=target.model,
            channel_id=target.channel_id,
            kind=_KIND_SKIPPED,
            groups=tuple(target.groups),
            requested_model=target.requested_model,
        )

    def make_attempt_fn():
        async def attempt(ch: Channel, wait_budget: float) -> tuple[Any, Channel]:
            result = await attempt_channel(
                ch,
                ChannelAttemptInput(
                    payload=_build_probe_request(target.model),
                    inbound_api_type=APIType.OPENAI_CHAT,
                    requested_model=target.requested_model,
                    serving_model=target.model,
                    is_stream=True,
                    request_source="group_probe",
                ),
                wait_budget=wait_budget,
            )
            assert isinstance(result, StreamAttemptResult)
            return result.stream, result.channel

        return attempt

    def _mk(kind: str) -> ProbeTargetResult:
        return ProbeTargetResult(
            model=target.model,
            channel_id=target.channel_id,
            kind=kind,
            groups=tuple(target.groups),
            requested_model=target.requested_model,
        )

    # 显式零等待预算：探活不占业务限速等待预算（D3）。锁定调度 model=None 跳过
    # 准入健康门（dispatch_pinned 接口承诺，ADR-0021 D1——探活目标的本质就是在探
    # 降级对，冷却期照常探，见 D2/User Story 10；enabled / blocked / 排除集门仍生效）。
    ctx = DispatchContext(wait_budget=0.0)
    first_group = target.groups[0].group if target.groups else None
    stream = None
    try:
        async with asyncio.timeout(timeout):
            stream, _served = await _dispatch_pinned(
                channel,
                make_attempt_fn(),
                model=None,
                group=first_group,
                context=ctx,
                admission=True,
            )
            async for _ in stream:
                pass
    except TimeoutError:
        if stream is not None:
            try:
                await asyncio.wait_for(stream.aclose(), timeout=2.0)
            except TimeoutError:
                logger.warning(f"[GROUP PROBE ACLOSE TIMEOUT] model={target.model} channel={target.channel_id} aclose timeout 2.0s")
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:
                pass
        # D4：发送链可能只记过 cancelled（渠道无责空操作）——挂起渠道必须以
        # transport_failure 出账，否则退避永不推进、该对以 cancelled 全速反复被打。
        outcomes.record(target.model, target.channel_id, OutcomeKind.transport_failure)
        logger.warning(f"[GROUP PROBE] 探活超时 {timeout:g}s → transport_failure: model={target.model} channel={target.channel_id}")
        return _mk(OutcomeKind.transport_failure.value)
    except AllChannelsExhausted as exc:
        kind = _classify_probe_error(exc)
        logger.warning(f"[GROUP PROBE] 探活失败 model={target.model} channel={target.channel_id} kind={kind}: {exc}")
        return _mk(kind)
    except Exception as exc:
        kind = _classify_probe_error(exc)
        logger.error(f"[GROUP PROBE] 探活异常 model={target.model} channel={target.channel_id} kind={kind}: {exc!r}")
        return _mk(kind)
    else:
        logger.debug(f"[GROUP PROBE] 探活成功 model={target.model} channel={target.channel_id}")
        return _mk(_KIND_SUCCESS)


async def run_probe_round(
    candidates: list[ProbeCandidate],
    *,
    timeout: float | None = None,
    concurrency: int | None = None,
) -> ProbeRoundResult:
    """对给定候选执行一轮并发探测（spec 测试缝：单轮驱动函数）。

    Args:
        candidates: 本轮探活目标（``take_probe_candidates`` 产出）。
        timeout: 单目标超时（默认热读 ``group_probe_timeout``，无配置回退 10s）。
        concurrency: 并发上限（默认热读 ``group_probe_concurrency``，无配置回退 5）。

    Returns:
        ``ProbeRoundResult``：成功 / 失败 / 跳过三桶（失败经既有链路如实记账，
        本轮只分类不重复记；成功已由发送链 ``record(success)`` 解冻该对）。
        成功目标随即经 ``reclaim_sticky_preferred`` 抢回所属 lazy 组的粘滞记忆
        （D7 写回，同轮收敛见该函数；失败/跳过零接触 ``_preferred``）。
    """
    if timeout is None:
        timeout = float(config.get_setting("group_probe_timeout") or _DEFAULT_TIMEOUT_SECONDS)
    if concurrency is None:
        concurrency = int(config.get_setting("group_probe_concurrency") or _DEFAULT_CONCURRENCY)

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(target: ProbeCandidate) -> ProbeTargetResult:
        async with semaphore:
            return await _probe_one_target(target, timeout=timeout)

    results = await asyncio.gather(*(_guarded(t) for t in candidates), return_exceptions=True)

    succeeded: list[ProbeTargetResult] = []
    failed: list[ProbeTargetResult] = []
    skipped: list[ProbeTargetResult] = []
    for target, res in zip(candidates, results, strict=True):
        if isinstance(res, BaseException):
            if isinstance(res, asyncio.CancelledError):
                raise res
            logger.error(f"[GROUP PROBE] 单目标异常未隔离 model={target.model} channel={target.channel_id}: {res!r}")
            failed.append(
                ProbeTargetResult(
                    model=target.model,
                    channel_id=target.channel_id,
                    kind=_KIND_ERROR,
                    groups=tuple(target.groups),
                    requested_model=target.requested_model,
                )
            )
            continue
        if res.kind == _KIND_SUCCESS:
            succeeded.append(res)
        elif res.kind == _KIND_SKIPPED:
            skipped.append(res)
        else:
            failed.append(res)
    result = ProbeRoundResult(tuple(succeeded), tuple(failed), tuple(skipped))
    if result.succeeded:
        reclaim_sticky_preferred(result)
    return result


def _read_probe_cadence(interval_override: float | None = None) -> tuple[float, int, float]:
    """热读三项节拍配置（ADR-0010 D10 / 用户故事 21）：``(interval, concurrency, timeout)``。

    interval 缺省热读 ``group_probe_interval_seconds``（无配置回退 60s）并施加
    ≥1s 下限守卫（防 config 极短值把空枚举烧成热循环）；``interval_override``
    非空时为固定间隔（测试注入点）。并发 / 超时恒热读 ``group_probe_concurrency``
    （默认 5）/ ``group_probe_timeout``（默认 10）。
    """
    if interval_override is not None:
        interval = float(interval_override)
    else:
        interval = float(config.get_setting("group_probe_interval_seconds") or _DEFAULT_INTERVAL_SECONDS)
    interval = max(_MIN_LOOP_INTERVAL_SECONDS, interval)
    concurrency = int(config.get_setting("group_probe_concurrency") or _DEFAULT_CONCURRENCY)
    timeout = float(config.get_setting("group_probe_timeout") or _DEFAULT_TIMEOUT_SECONDS)
    return interval, concurrency, timeout


async def _sleep_until_next(started: float, interval: float) -> None:
    """睡到下一轮起点：``started`` 起算、扣除本轮实耗；本轮已超时不睡直接下一轮。

    保证两轮起点约间隔一个 interval（不因探活耗时漂移堆积）；interval 本身已带
    ≥1s 守卫（``_read_probe_cadence``），此处无需再兜。"""
    remaining = interval - (time.monotonic() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)


async def _probe_round_once(*, interval_seconds: float | None = None) -> float:
    """单轮探活主体（loop 内每轮执行体）：取目标 → 并发探（成功写回由 08 内建）。

    返回本轮生效的 interval（含守卫），供循环层按实耗折算睡眠。无 ``lazy_sticky``
    组时 ``take_probe_candidates`` 即返回空且 ``run_probe_round`` 不调用——零开销
    （D12-3：仅有枚举侧既有缓存读，无额外 await/存储）。单目标异常已由 07 隔离，
    本函数不设防整轮异常（交由循环层双层兜底）。
    """
    interval, concurrency, timeout = _read_probe_cadence(interval_seconds)
    candidates = await take_probe_candidates(interval_seconds=interval)
    if candidates:
        await run_probe_round(candidates, timeout=timeout, concurrency=concurrency)
    return interval


async def run_group_probe_loop(*, interval_seconds: float | None = None) -> None:
    """常驻后台探活循环（ADR-0010 D10）：``main.py:lifespan`` 启动时 ``create_task``、
    shutdown 时 ``cancel``。

    每轮：热读间隔 / 并发 / 超时（``config.get_setting``，改后无需重启）→
    ``take_probe_candidates`` 枚举工作清单（06）→ ``run_probe_round`` 并发单轮驱动
    （07，成功写回 08 内建）→ 睡眠至下一轮起点（扣除本轮实耗，两轮约间隔一个
    interval）。剪枝零额外清理（用户故事 16/17/D12-3）：组删 / 停用 / 关
    ``lazy_sticky``、模型条目移除、渠道删 / 禁用、格式门控排除后目标由枚举侧天然
    消失；渠道维度 ``(model, channel)`` 键仅在精确 Channel delete 时由
    ``RuntimeCatalogEffects`` → ``load_balancer.remove_channel`` →
    ``outcomes.remove_channel`` 精确清理。

    异常隔离双层（用户故事 19，循环永不退化成死任务）：内层 try/except 吞掉单轮
    抛出的任何失败（log ``[GROUP PROBE]`` 后继续下一轮）；最外层再兜住内层未覆盖
    的意外（如睡眠层失败），也绝不让任务退出。仅 ``asyncio.CancelledError`` 向上
    传播——shutdown ``cancel()`` 干净退出、无孤儿任务；循环不持有任何跨轮状态，
    重启后从干净状态重新枚举。

    Args:
        interval_seconds: 固定间隔（秒），测试注入点；缺省每轮热读
            ``group_probe_interval_seconds``（默认 60，带 ≥1s 睡眠守卫）。
    """
    while True:
        started = time.monotonic()
        try:
            try:
                interval = await _probe_round_once(interval_seconds=interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[GROUP PROBE] 单轮异常已隔离（循环继续）")
                interval = _DEFAULT_INTERVAL_SECONDS
            await _sleep_until_next(started, interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[GROUP PROBE] 循环层意外异常已隔离（继续下一轮）")
            await asyncio.sleep(_MIN_LOOP_INTERVAL_SECONDS)
