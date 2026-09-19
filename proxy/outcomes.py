"""单一 outcome 事件流（ADR-0008 D0）——调度记账的唯一写缝。

对外只暴露一个写缝 ``record(model, channel_id, kind, t=None)``、记账键 model
段推导唯一住所 ``effective_model``（ADR-0025 D0）和一组只读视图
（``is_healthy`` / ``is_degraded`` / ``is_blocked`` / ``sticky_preferred`` /
``probe_targets``），以及由视图派生的准入单一住所 ``admits()``（ADR-0021 D0）。
内部用单进程内存事件环退化的聚合视图派生健康 / 降级 /
阻塞 / 粘滞四类状态，现阶段无 SQL 落库。

本模块是深模块：调用方只按业务语义记账（"这条渠道这次成功了 / 失败了"），
不关心事件环形态、键类型或冷却判定细节。事件时间由缝内统一取 ``time.time()``，
调用方不传，保证事件排序与"现在是否已过 cooldown"的判定基于同一时钟。

分层约定：
- 渠道级固有属性（enabled / endpoints / RPM 滑动窗）不归入本事件环；
- 服务表现级（健康 / 降级 / 粘滞 / quota 窗口）只由 ``record(kind)`` 写，
  同一事件既推 ``is_healthy`` 也推 ``is_degraded``，无双写漂移面；
- quota 的 JSON 写穿（``data/channel_quota_limits.json``）由
  ``kind=quota_window`` 事件触发的适配器负责——内存视图与持久化分层不互写：
  ``is_blocked`` 的内存判断在本模块内，写穿由 ``set_quota_adapter`` 注册的
  回调（quota_limits.store）承担。
"""

import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from loguru import logger

import config
from models.channel import Channel


class OutcomeKind(str, Enum):
    """富枚举：8 个取值与 spec 一致。

    - ``success``: 请求成功
    - ``transport_failure``: 网络 / 传输层失败（超时、连接错误、协议违约）
    - ``http_5xx``: 上游 5xx（可重试）
    - ``http_429``: 上游 429（瞬时限速）
    - ``http_4xx_config``: 上游 401/403/404（渠道配置问题，降级持续为 True）
    - ``rate_limit_exhausted``: 代理侧限速预算耗尽，转故障转移
    - ``quota_window``: 窗口级限速（重置窗口，驱动 ``is_blocked`` + JSON 写穿）
    - ``cancelled``: 请求被客户端取消（渠道无责）
    """

    success = "success"
    transport_failure = "transport_failure"
    http_5xx = "http_5xx"
    http_429 = "http_429"
    http_4xx_config = "http_4xx_config"
    rate_limit_exhausted = "rate_limit_exhausted"
    quota_window = "quota_window"
    cancelled = "cancelled"


# 视为"失败"的 kind：计入滚动失败计数并把 (model, channel) 置为降级态。
# quota_window 是渠道级硬限制（走 is_blocked），cancelled 是客户端取消（渠道无责），
# 两者都不计入健康 / 降级视图。
_FAILURE_KINDS = frozenset(
    {
        OutcomeKind.transport_failure,
        OutcomeKind.http_5xx,
        OutcomeKind.http_429,
        OutcomeKind.http_4xx_config,
        OutcomeKind.rate_limit_exhausted,
    }
)


@dataclass(frozen=True)
class ProbeTarget:
    """探活候选富记录行（ADR-0010 D6）。

    ``first_failed_at`` / ``consecutive_failures`` 供探活退避起算与诊断；
    ``permanent`` 为配置级永久降级位（ADR-0010 D5）：命中 401/403/404 置位后
    该对**不再产出**（调度侧停探，连退避都停），由渠道配置保存或真实
    success 解除。所属 lazy 组序信息由枚举层按组 join 时补充，本模块不感知组。
    """

    model: str
    channel_id: str
    first_failed_at: float
    consecutive_failures: int
    permanent: bool = False


@dataclass
class _HealthEntry:
    """(model, channel_id) 健康 / 降级聚合视图的派生源。

    fail_count / last_fail_time: 滚动失败计数与最近失败时刻，供 ``is_healthy``
        做冷却快速熔断（时间到期清零即"假自愈"）。
    degraded: 降级反向表——自最近一次 ``success`` 起是否存在未被成功覆盖的
        失败事件；冷却假自愈不会清除它，只有真实 ``success`` 才清除。
    current_weight: SWRR 平滑加权轮询所需（ADR-0008 D0，保留给后续轮询适配器）。
    first_failed_at: 当前降级期起点（自最近一次 ``success`` 起首个失败事件的
        时刻），供退避起算与诊断；``success`` 清零。
    consecutive_failures: 自最近一次 ``success`` 起连续失败事件数（失败类 kind
        递增、``success`` 清零）。不复用 ``fail_count``——冷却假自愈会清零
        ``fail_count``，会破坏退避计数（ADR-0010 D6）。
    permanent: 配置级永久降级位（ADR-0010 D5）：仅 401/403/404 命中置位，
        渠道配置保存 / 真实 success 解除；置位后保留 degraded（组层持续不可选），
        仅移出探活调度（连退避都停）。
    """

    fail_count: int = 0
    last_fail_time: float = 0.0
    degraded: bool = False
    current_weight: int = 0
    first_failed_at: float = 0.0
    consecutive_failures: int = 0
    permanent: bool = False


# 内部存储：单进程内存事件环（键为 (model, channel_id) 或 channel_id）
_health: dict[tuple[str, str], _HealthEntry] = {}
_blocked: dict[str, tuple[float, str]] = {}  # channel_id -> (reset_at_unix, code)
_preferred: dict[tuple[str, str], str] = {}  # (group_id, model) -> channel_id


@dataclass
class _SessionStickyEntry:
    """会话粘滞缓存条目（原 LoadBalancer._sticky_cache，收编为本模块适配器）。

    channel_id: 该会话指纹绑定的渠道。
    last_active_at: 最近一次命中时刻（TTL 判定 + LRU 淘汰依据）。
    """

    channel_id: str
    last_active_at: float


_session_sticky: OrderedDict[str, _SessionStickyEntry] = OrderedDict()
_session_sticky_ttl: float = 1800.0
_session_sticky_max_entries: int = 10000

_lock = threading.Lock()

# 熔断阈值动态兜底 — 启动引导安全网（ADR-0025 D2）：
# 未显式 configure 时回退到 config.get_setting 动态值。settings.json 本身自带
# 3/120/1800/10000 默认值，兜底不是主路径也非配置系统降级保护；真价值是消除
# “import 时机冻结阈值”的顺序耦合，与 ADR-0021 的 model=None 跳健康门同属
# 接口承诺而非热路径。
_configured_max_fail_count: int | None = None
_configured_cooldown_seconds: float | None = None

# quota 写穿适配器：kind=quota_window 事件触发，负责 JSON 持久化
_quota_adapter: Callable[[str, float, str], None] | None = None


def _quota_path() -> str:
    return os.path.join(config.DATA_DIR, "channel_quota_limits.json")


def configure(*, max_fail_count: int | None = None, cooldown_seconds: float | None = None) -> None:
    """覆盖熔断阈值；None 表示回退到 config 动态值（默认 3 / 120，启动引导安全网）。

    该动态兜底是启动引导安全网而非主路径/降级保护——消除 import 时机冻结阈值的
    顺序耦合（ADR-0025 D2），与 ADR-0021 model=None 跳健康门同属接口承诺。
    """
    global _configured_max_fail_count, _configured_cooldown_seconds
    _configured_max_fail_count = max_fail_count
    _configured_cooldown_seconds = cooldown_seconds


def _max_fail_count() -> int:
    if _configured_max_fail_count is not None:
        return _configured_max_fail_count
    return int(config.get_setting("max_fail_count") or 3)


def _cooldown_seconds() -> float:
    if _configured_cooldown_seconds is not None:
        return _configured_cooldown_seconds
    return float(config.get_setting("cooldown_seconds") or 120)


def configure_session_sticky(*, ttl: float = 1800.0, max_entries: int = 10000) -> None:
    """配置会话粘滞缓存（TTL / 容量），不改动既有条目。

    由 LoadBalancer.update_config 在 sticky_ttl / sticky_cache_max_entries 变更时
    转发；策略或 TTL 变更时由调用方显式 ``clear_session_sticky``。
    """
    global _session_sticky_ttl, _session_sticky_max_entries
    with _lock:
        _session_sticky_ttl = float(ttl)
        _session_sticky_max_entries = int(max_entries)


def clear_session_sticky() -> None:
    """清空会话粘滞缓存（策略 / TTL 变更时调用，等价旧 LoadBalancer 行为）。"""
    with _lock:
        _session_sticky.clear()


def session_sticky_get(session_key: str, now: float | None = None) -> str | None:
    """会话粘滞视图：命中且未过期则 touch（更新 last_active_at + LRU）并返回渠道 id；
    未命中或已过期返回 None（过期项惰性弹出）。"""
    now = time.time() if now is None else now
    with _lock:
        entry = _session_sticky.get(session_key)
        if entry is None:
            return None
        if now - entry.last_active_at >= _session_sticky_ttl:
            _session_sticky.pop(session_key, None)
            return None
        entry.last_active_at = now
        _session_sticky.move_to_end(session_key)
        return entry.channel_id


def remember_session_sticky(session_key: str, channel_id: str, now: float | None = None) -> None:
    """写入会话粘滞记忆；写入后触发过期清理与 LRU 淘汰。"""
    now = time.time() if now is None else now
    with _lock:
        _session_sticky[session_key] = _SessionStickyEntry(channel_id, now)
        _session_sticky.move_to_end(session_key)
        _trim_session_sticky_locked(now)


def trim_session_sticky(now: float | None = None) -> None:
    """维护性裁剪：清理过期项与超容量 LRU（策略 / TTL 未变时的例行调用）。"""
    now = time.time() if now is None else now
    with _lock:
        _trim_session_sticky_locked(now)


def _trim_session_sticky_locked(now: float) -> None:
    expired = []
    for key, entry in _session_sticky.items():
        if len(expired) >= 100:
            break
        if now - entry.last_active_at >= _session_sticky_ttl:
            expired.append(key)
    for key in expired:
        _session_sticky.pop(key, None)
    while len(_session_sticky) > _session_sticky_max_entries:
        _session_sticky.popitem(last=False)


def set_quota_adapter(fn: Callable[[str, float, str], None] | None) -> None:
    """注册 quota 写穿适配器。

    ``fn(channel_id, reset_at_unix, code)`` 把 blocked 状态持久化到
    ``data/channel_quota_limits.json``（当前由 quota_limits.store 承担写穿）。
    None 表示只维护内存视图不写穿。
    """
    global _quota_adapter
    _quota_adapter = fn


def effective_model(
    *,
    model: str | None,
    body_model: str | None = None,
    requested_model: str | None = None,
    channel_id: str,
) -> str:
    """健康记账键的 model 段推导唯一住所（ADR-0025 D0）。

    兜底链：``model or body_model or requested_model or channel_id``——前一级
    非空（真值）即胜出，全函数恒返回 ``str``（channel_id 兜底），调用点无需
    各自背「跳过 or 兜底」判断。

    各记账站点约定**只传自己作用域拥有的字段**：

    - ``model``: 已解析的请求模型（routing / 执行器作用域；上游 body model
      已提取进该参数时，``body_model`` 不重复传）；
    - ``body_model``: 原始请求体 ``request_data["model"]``（仅 routing 在
      ``model`` 参数可能为 None 时额外持有）；
    - ``requested_model``: 客户端原始请求模型（仅 routing 同时持有）；
    - ``channel_id``: 必传——被记账号渠道，也是最终兜底（渠道级虚拟键，
      语义即「无真实模型」，不进模型级探活管道，见 ``probe_targets``）。
    """
    return model or body_model or requested_model or channel_id


def record(
    model: str,
    channel_id: str,
    kind: OutcomeKind | str,
    t: float | None = None,
    *,
    reset_at: float | None = None,
    code: str = "",
) -> None:
    """记账唯一写缝：一次写入 = 一处更新。

    Args:
        model: 真实请求模型（组场景取 entry.model，非组取 requested_model）。
        channel_id: 被记账号渠道。
        kind: 富枚举 OutcomeKind（也接受同名字符串）。
        t: 事件时间，缺省时缝内统一取 ``time.time()``，调用方一般不传。
        reset_at: 仅 kind=quota_window 使用——窗口重置时刻（unix 时间戳），
            缺省取事件时刻。驱动 ``is_blocked`` 并传给写穿适配器。
        code: 仅 kind=quota_window 使用——供应商限速错误码
            （如 AccountQuotaExceeded）。
    """
    if not isinstance(kind, OutcomeKind):
        kind = OutcomeKind(kind)
    now = time.time() if t is None else t
    permanent_newly_set = False
    with _lock:
        if kind is OutcomeKind.success:
            entry = _health.setdefault((model, channel_id), _HealthEntry())
            entry.fail_count = 0
            entry.degraded = False
            entry.consecutive_failures = 0
            entry.first_failed_at = 0.0
            # 解除路径②：业务真实 success 整条出环（permanent 与 is_degraded 同时清除）
            entry.permanent = False
        elif kind is OutcomeKind.quota_window:
            _record_quota_window_locked(channel_id, reset_at if reset_at is not None else now, code)
        elif kind is OutcomeKind.cancelled:
            # 客户端取消：渠道无责，不影响健康 / 降级视图
            pass
        elif kind in _FAILURE_KINDS:
            entry = _health.setdefault((model, channel_id), _HealthEntry())
            entry.fail_count += 1
            entry.last_fail_time = now
            entry.degraded = True
            if entry.consecutive_failures == 0:
                # 降级期起点：首个失败事件时刻，退避起算基准；同期内后续失败不刷新
                entry.first_failed_at = now
            entry.consecutive_failures += 1
            if kind is OutcomeKind.http_4xx_config and not entry.permanent:
                # 401/403/404 为配置级不自愈错误（key 失效 / 无权限 / 模型不存在），
                # 与观察者身份无关（探活或业务流量观察到均置位，不给 record() 加身份参数）；
                # 置 permanent 停探（连退避都停）待人工（ADR-0010 D5）。
                # 400 等歧义 4XX 不属此列，维持普通失败走退避。
                entry.permanent = True
                permanent_newly_set = True
        else:  # pragma: no cover - OutcomeKind 穷尽后不可达，防御性
            raise ValueError(f"unknown outcome kind: {kind!r}")
    if permanent_newly_set:
        logger.warning(
            f"[OUTCOMES] (model={model}, channel={channel_id}) 命中 401/403/404（key 失效 / "
            f"无权限 / 模型不存在），置 permanent 永久降级并停止探活，请人工检查渠道配置"
        )


def _record_quota_window_locked(channel_id: str, reset_at: float, code: str) -> None:
    _blocked[channel_id] = (reset_at, code)
    adapter = _quota_adapter
    if adapter is not None:
        adapter(channel_id, reset_at, code)


def is_healthy(model: str, channel_id: str) -> bool:
    """(model, channel) 对是否健康。

    滚动失败计数未达 ``max_fail_count`` 即健康；达到后进入冷却，冷却期到期
    清零计数"假自愈"（快速熔断），真实故障由 ``is_degraded`` 兜底验证。
    A 模型的失败只记入 (A, ch) 键，不污染 ``is_healthy(B, ch)``。
    """
    with _lock:
        entry = _health.get((model, channel_id))
        if entry is None:
            return True
        if entry.fail_count < _max_fail_count():
            return True
        if (time.time() - entry.last_fail_time) <= _cooldown_seconds():
            return False
        # 冷却到期"假自愈"：只清快速熔断计数，不动 consecutive_failures /
        # first_failed_at——退避计数与快速熔断解耦（ADR-0010 D6），真实降级仍由
        # is_degraded 兜住。
        entry.fail_count = 0
        entry.last_fail_time = 0.0
        return True


def is_degraded(model: str, channel_id: str) -> bool:
    """(model, channel) 是否处于真实降级（反向表）。

    自最近一次 ``success`` 起存在未被成功覆盖的失败事件即 True；冷却假自愈
    不会清除它，只有真实 ``success`` 事件才清除。
    """
    entry = _health.get((model, channel_id))
    return entry is not None and entry.degraded


def is_blocked(channel_id: str) -> bool:
    """渠道是否处于窗口级硬限制（quota_window 的 reset_at 仍在未来）。"""
    with _lock:
        entry = _blocked.get(channel_id)
        if entry is None:
            return False
        reset_at, _ = entry
        if reset_at <= time.time():
            _blocked.pop(channel_id, None)
            return False
        return True


def admits(channel: Channel, model: str | None, *, exclude_ids: set[str] | None = None) -> bool:
    """渠道准入（ADR-0021 D0）：当前时刻该渠道可被尝试的硬门禁判定，全库单一住所。

    语义：``channel.enabled ∧ channel.id ∉ exclude_ids ∧ ¬is_blocked(channel.id)
    ∧ (model is None ∨ is_healthy(model, channel.id))``。

    - ``blocked``（窗口级硬限制）优先于一切：命中即 False，不再看健康视图。
    - ``model=None`` 跳过健康门——探活等无具体模型语境的调用复用此语义，
      这是接口承诺而非实现细节；其余三门（enabled / exclude / blocked）仍生效。
    - 「降级让位」（``is_degraded`` / yield_predicate）不属于准入：让位是独立的
      软门禁概念，由调用方组合，见 CONTEXT.md「Admission」词条与 ADR-0021 D0。
    """
    if not channel.enabled:
        return False
    if exclude_ids and channel.id in exclude_ids:
        return False
    if is_blocked(channel.id):
        return False
    return model is None or is_healthy(model, channel.id)


def sticky_preferred(group_id: str, model: str) -> str | None:
    """组内粘滞首选渠道（ADR-0008 D1）：(group_id, model) → channel_id。

    本期为基础版：记忆由组选路逻辑经 ``remember_preferred`` 写入；success /
    失败事件自动派生的完整语义由后续 ticket 接缝补齐。调用方不直接读写
    ``_preferred`` 字典。
    """
    return _preferred.get((group_id, model))


def remember_preferred(group_id: str, model: str, channel_id: str) -> None:
    """写入组内粘滞记忆（基础版；供组选路逻辑在成功 / 失败后更新首选）。"""
    with _lock:
        _preferred[(group_id, model)] = channel_id


def probe_targets() -> list[ProbeTarget]:
    """ADR-0010 探活候选：当前处于降级态、需主动探活验证恢复的
    (model, channel) 富记录行列表。

    行级字段 ``first_failed_at`` / ``consecutive_failures`` 供退避起算与诊断。
    ``permanent`` 对**不产出**（ADR-0010 D5）：命中 401/403/404 后停探
    （连退避都停），其降级态仍由 ``is_degraded`` 保留（组层持续不可选），
    等待渠道配置保存或真实 success 解除。本期仅暴露视图，不实现后台探活循环。

    虚拟键过滤（ADR-0025 D1-A2）：``model == channel_id`` 的键是渠道级兜底
    虚拟键——``effective_model`` 的 channel_id 兜底使「无真实模型」的记账以
    渠道自身 id 为 model 段落账，其语义即「渠道级」而非某个模型坏了，本就不
    参与**模型级**探活回切；否则会对不存在的模型名发真实探测 → 上游 404 →
    置 permanent 停探（单次事件至多一轮脏探活，但属静默浪费）。历史 ``""``
    model 残留键同被该语义覆盖：``""`` 同样表示「无真实模型」的渠道级记账
    （兜底链把 ``""`` 归一为渠道自身键的前身），一并排除——「兜底照记」与
    「渠道兜底键不进模型探活管道」是同一条规则的左右手。"""
    with _lock:
        return [
            ProbeTarget(
                model=m,
                channel_id=ch,
                first_failed_at=entry.first_failed_at,
                consecutive_failures=entry.consecutive_failures,
                permanent=entry.permanent,
            )
            for (m, ch), entry in _health.items()
            # 排除虚拟键（m == ch）与历史 "" 脏键：两者同为渠道级语义，不进模型级探活
            if entry.degraded and not entry.permanent and m and m != ch
        ]


def clear_all_permanent() -> None:
    """清除全部 permanent 位；仅供显式运维重置与测试使用。"""
    with _lock:
        for entry in _health.values():
            entry.permanent = False


def clear_permanent_for_channel(channel_id: str) -> None:
    """Channel Change 后只重新开放该 Channel 的 permanent degradation。"""
    with _lock:
        for (_model, recorded_channel_id), entry in _health.items():
            if recorded_channel_id == channel_id:
                entry.permanent = False


def load_quota_limits(path: str | None = None) -> None:
    """重启后从 ``data/channel_quota_limits.json`` 重载 blocked 视图。

    只载入 reset_at 仍在未来的条目；文件格式与 quota_limits.store 一致
    （reset_at 为 ISO datetime 字符串或 unix 时间戳）。
    """
    path = path or _quota_path()
    loaded: dict[str, tuple[float, str]] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.warning(f"[OUTCOMES] quota limits 文件读取失败，忽略重载: {path}", exc_info=True)
            raw = {}
        if isinstance(raw, dict):
            now = time.time()
            for ch_id, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                reset_at = _parse_reset_at(entry.get("reset_at"))
                if reset_at is None or reset_at <= now:
                    continue
                loaded[ch_id] = (reset_at, str(entry.get("code", "")))
    with _lock:
        _blocked.clear()
        _blocked.update(loaded)


def _parse_reset_at(value: object) -> float | None:
    """把 quota 文件的 reset_at（ISO datetime 字符串或 unix 时间戳）解析为 unix 时间戳。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


def remove_channel(channel_id: str) -> None:
    """删除且只删除一个 Channel 的健康、阻塞与粘滞状态。"""
    with _lock:
        for key in [key for key in _health if key[1] == channel_id]:
            _health.pop(key, None)
        _blocked.pop(channel_id, None)
        for key in [key for key, preferred_channel_id in _preferred.items() if preferred_channel_id == channel_id]:
            _preferred.pop(key, None)
        for key in [key for key, entry in _session_sticky.items() if entry.channel_id == channel_id]:
            _session_sticky.pop(key, None)


def reset() -> None:
    """清空全部内部状态并回退熔断阈值到 config 动态值（等价进程重启）。
    供测试与模块生命周期使用。"""
    global _configured_max_fail_count, _configured_cooldown_seconds
    with _lock:
        _health.clear()
        _blocked.clear()
        _preferred.clear()
        _session_sticky.clear()
        _configured_max_fail_count = None
        _configured_cooldown_seconds = None
