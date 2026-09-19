import asyncio
import hashlib
import json
import math

from models.channel import Channel
from proxy import outcomes

VALID_STRATEGIES = {"round_robin", "backup", "sticky"}


class LoadBalancer:
    """优先级分组 + 加权轮询负载均衡器。

    健康 / 降级 / 阻塞 / 粘滞等"服务表现级"状态全部由 :mod:`proxy.outcomes`
    派生（ADR-0008 D0），本类不再维护独立健康表；只保留选择算法自身的
    可移植状态：

    - ``_current_weights``：SWRR 平滑加权轮询的权重累加（键 ``(model, channel_id)``，
      轮询始终发生在单模型候选集内，A 模型的轮询进度不污染 B 模型）。
    - 会话粘滞缓存：由 ``outcomes`` 内部维护（``session_sticky_*`` 适配器）。

    ``select_channel`` 的选路遵循 "blocked 优先于一切"：先查
    ``outcomes.is_blocked(ch)``，再查 ``outcomes.is_healthy(model, ch)``。
    """

    def __init__(self):
        self._current_weights: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()
        self._strategy: str = "round_robin"
        self._sticky_ttl: float = 1800.0

    async def update_config(
        self,
        strategy: str = "round_robin",
        sticky_ttl: int = 1800,
        sticky_cache_max_entries: int = 10000,
    ):
        """热更新策略与会话粘滞配置（阈值直调 outcomes，不经 LB 中转）。"""
        normalized_strategy = str(strategy).lower()
        if normalized_strategy not in VALID_STRATEGIES:
            raise ValueError(f"lb_strategy must be one of {sorted(VALID_STRATEGIES)}, got {strategy!r}")
        async with self._lock:
            clear_sticky_cache = normalized_strategy != self._strategy or float(sticky_ttl) != self._sticky_ttl
            self._strategy = normalized_strategy
            self._sticky_ttl = float(sticky_ttl)
            outcomes.configure_session_sticky(
                ttl=self._sticky_ttl,
                max_entries=int(sticky_cache_max_entries),
            )
            if clear_sticky_cache:
                outcomes.clear_session_sticky()
            else:
                outcomes.trim_session_sticky()

    async def remove_channel(self, channel_id: str) -> None:
        """删除且只删除一个 Channel 的选择算法与 outcome 状态。"""
        async with self._lock:
            for key in [key for key in self._current_weights if key[1] == channel_id]:
                self._current_weights.pop(key, None)
        outcomes.remove_channel(channel_id)

    async def select_channel(
        self,
        channels: list[Channel],
        exclude_ids: set[str] | None = None,
        model: str | None = None,
        client_ip: str | None = None,
        api_key_id: str | None = None,
        client_headers: dict[str, str] | None = None,
    ) -> Channel | None:
        """
        从候选渠道中选择一个：
        1. 过滤掉禁用、阻塞（``outcomes.is_blocked``，优先于一切）、
           ``(model, channel)`` 不健康（``outcomes.is_healthy``）及 exclude_ids 中的渠道
        2. 按优先级分组
        3. 在最高优先级组内按策略（round_robin / backup / sticky）选路

        整个选择过程在锁内完成，确保健康检查与轮询的原子性。
        """
        exclude_ids = exclude_ids or set()
        async with self._lock:
            top_group = self._get_top_priority_group(channels, exclude_ids, model)
            if not top_group:
                return None

            if self._strategy == "backup":
                return self._backup_select(top_group)
            if self._strategy == "sticky":
                session_key = self._build_session_fingerprint(
                    client_ip=client_ip,
                    api_key_id=api_key_id,
                    client_headers=client_headers,
                )
                return self._sticky_select_cached(session_key, top_group)
            return self._weighted_round_robin(top_group, model) if len(top_group) > 1 else top_group[0]

    def _get_top_priority_group(
        self,
        channels: list[Channel],
        exclude_ids: set[str],
        model: str | None = None,
    ) -> list[Channel]:
        # 准入公式单一住所 outcomes.admits()（ADR-0021 D0）：LB 锁内调 outcomes 读视图，
        # 锁序 LB→outcomes 单向，无死锁风险。
        available = [ch for ch in channels if outcomes.admits(ch, model, exclude_ids=exclude_ids)]
        if not available:
            return []
        min_priority = min(ch.priority for ch in available)
        return [ch for ch in available if ch.priority == min_priority]

    def _sticky_select_by_hrw(self, session_key: str, candidates: list[Channel]) -> Channel:
        if not candidates:
            raise ValueError("candidates must not be empty")
        best_channel: Channel | None = None
        best_score: float | None = None
        for channel in candidates:
            digest = hashlib.sha256(f"{session_key}:{channel.id}".encode()).digest()
            value = int.from_bytes(digest[:8], "big") / 2**64
            value = max(value, 1e-12)
            score = -math.log(value) / max(channel.weight, 1)
            if best_score is None or score < best_score:
                best_score = score
                best_channel = channel
        return best_channel

    def _sticky_select_cached(self, session_key: str, candidates: list[Channel]) -> Channel:
        """带缓存的粘性选择（缓存由 outcomes 会话粘滞适配器维护）。

        缓存命中条件：未过期 且 缓存渠道仍在候选列表中。
        缓存未命中时（过期、渠道被 exclude_ids 排除、或首次访问），
        用 HRW 选出新渠道并覆盖缓存条目。

        注意：当原渠道因故障转移被排除时，缓存会被新渠道永久替换，
        即使原渠道后续恢复，会话也不会回切——这是有意为之的设计，
        避免故障恢复后反复震荡导致流量分布不稳定。
        """
        candidate_by_id = {ch.id: ch for ch in candidates}
        cached = outcomes.session_sticky_get(session_key)
        if cached and cached in candidate_by_id:
            return candidate_by_id[cached]

        selected = self._sticky_select_by_hrw(session_key, candidates)
        outcomes.remember_session_sticky(session_key, selected.id)
        return selected

    def _backup_select(self, channels: list[Channel]) -> Channel:
        return sorted(channels, key=lambda ch: (-ch.weight, ch.id))[0]

    def _weighted_round_robin(self, channels: list[Channel], model: str | None = None) -> Channel:
        """平滑加权轮询算法（SWRR，键为 ``(model, channel_id)``）。

        算法：
        1. 所有 channel 的 current_weight += weight
        2. 选择 current_weight 最大的 channel
        3. 被选中 channel 的 current_weight -= total_weight

        轮询进度按 ``(model, channel)`` 隔离：同一渠道上 A 模型的轮询进度
        不污染 B 模型（A 挂不死 B 的权重维度）。``model=None`` 退化为
        空字符串键（与 ``select_channel`` 的兼容语义一致：未传 model
        不参与隔离）。
        """
        key_model = model or ""
        total_weight = sum(ch.weight for ch in channels)

        best: Channel | None = None
        best_weight: int = -1  # Channel.weight 恒为正（ge=1），-1 保证首个渠道必选
        for ch in channels:
            key = (key_model, ch.id)
            current = self._current_weights.get(key, 0) + ch.weight
            self._current_weights[key] = current
            # 选择 current_weight 最大的 channel
            if best is None or current > best_weight:
                best = ch
                best_weight = current

        # 递减选中channel的current_weight（调用方保证 channels 非空）
        assert best is not None
        self._current_weights[(key_model, best.id)] = best_weight - total_weight

        return best

    def _normalize_headers(self, client_headers: dict[str, str] | None) -> dict[str, str]:
        if not client_headers:
            return {}
        return {str(k).lower(): str(v) for k, v in client_headers.items()}

    def _build_session_fingerprint(
        self,
        *,
        client_ip: str | None,
        api_key_id: str | None,
        client_headers: dict[str, str] | None,
    ) -> str:
        headers = self._normalize_headers(client_headers)
        explicit_session = headers.get("x-session-id") or headers.get("x-claude-code-session-id")
        if explicit_session:
            canonical = json.dumps(
                {"session": explicit_session[:512]},
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            canonical = json.dumps(
                {
                    "api_key_id": api_key_id or "",
                    "client_ip": client_ip or "",
                    "user_agent": headers.get("user-agent", ""),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


load_balancer = LoadBalancer()
