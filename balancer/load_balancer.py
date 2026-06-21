import asyncio
import hashlib
import json
import math
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Optional

from models.channel import Channel


VALID_STRATEGIES = {"round_robin", "backup", "sticky"}


@dataclass
class StickyCacheEntry:
    channel_id: str
    last_active_at: float


class ChannelHealth:
    """跟踪单个渠道的健康状态"""

    def __init__(self):
        self.fail_count: int = 0
        self.last_fail_time: float = 0
        self.current_weight: int = 0

    def record_success(self):
        self.fail_count = 0

    def record_failure(self):
        self.fail_count += 1
        self.last_fail_time = time.time()

    def is_healthy(self, max_fail_count: int, cooldown_seconds: float) -> bool:
        """检查渠道是否健康

        Args:
            max_fail_count: 最大允许失败次数
            cooldown_seconds: 冷却时间（秒）
        """
        if self.fail_count < max_fail_count:
            return True
        return (time.time() - self.last_fail_time) > cooldown_seconds


class LoadBalancer:
    """优先级分组 + 加权轮询负载均衡器"""

    def __init__(self):
        self._health: dict[str, ChannelHealth] = defaultdict(ChannelHealth)
        self._lock = asyncio.Lock()
        self._max_fail_count: int = 5
        self._cooldown_seconds: float = 60.0
        self._strategy: str = "round_robin"
        self._sticky_ttl: float = 1800.0
        self._sticky_cache_max_entries: int = 10000
        self._sticky_cache: OrderedDict[str, StickyCacheEntry] = OrderedDict()

    async def update_config(
        self,
        max_fail_count: int = 5,
        cooldown_seconds: int = 60,
        strategy: str = "round_robin",
        sticky_ttl: int = 1800,
        sticky_cache_max_entries: int = 10000,
    ):
        """热更新配置参数"""
        normalized_strategy = str(strategy).lower()
        if normalized_strategy not in VALID_STRATEGIES:
            raise ValueError(f"lb_strategy must be one of {sorted(VALID_STRATEGIES)}, got {strategy!r}")
        async with self._lock:
            clear_sticky_cache = (
                normalized_strategy != self._strategy
                or float(sticky_ttl) != self._sticky_ttl
            )
            self._max_fail_count = max_fail_count
            self._cooldown_seconds = float(cooldown_seconds)
            self._strategy = normalized_strategy
            self._sticky_ttl = float(sticky_ttl)
            self._sticky_cache_max_entries = int(sticky_cache_max_entries)
            if clear_sticky_cache:
                self._sticky_cache.clear()
            else:
                self._trim_sticky_cache(time.time())

    async def record_success(self, channel_id: str):
        async with self._lock:
            self._health[channel_id].record_success()

    async def record_failure(self, channel_id: str):
        async with self._lock:
            self._health[channel_id].record_failure()

    async def cleanup_removed_channels(self, active_channel_ids: set[str]):
        async with self._lock:
            for ch_id in list(self._health.keys()):
                if ch_id not in active_channel_ids:
                    del self._health[ch_id]

    async def select_channel(
        self,
        channels: list[Channel],
        exclude_ids: set[str] | None = None,
        client_ip: str | None = None,
        api_key_id: str | None = None,
        client_headers: dict[str, str] | None = None,
    ) -> Optional[Channel]:
        """
        从候选渠道中选择一个：
        1. 过滤掉禁用、不健康及 exclude_ids 中的渠道
        2. 按优先级分组
        3. 在最高优先级组内加权轮询

        整个选择过程在锁内完成，确保健康检查与轮询的原子性。
        """
        exclude_ids = exclude_ids or set()
        async with self._lock:
            top_group = self._get_top_priority_group(channels, exclude_ids)
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
            return self._weighted_round_robin(top_group) if len(top_group) > 1 else top_group[0]

    def _get_top_priority_group(self, channels: list[Channel], exclude_ids: set[str]) -> list[Channel]:
        available = [
            ch
            for ch in channels
            if ch.enabled
            and ch.id not in exclude_ids
            and self._health[ch.id].is_healthy(self._max_fail_count, self._cooldown_seconds)
        ]
        if not available:
            return []
        min_priority = min(ch.priority for ch in available)
        return [ch for ch in available if ch.priority == min_priority]

    def _sticky_select_by_hrw(self, session_key: str, candidates: list[Channel]) -> Channel:
        if not candidates:
            raise ValueError("candidates must not be empty")
        best_channel: Optional[Channel] = None
        best_score: float | None = None
        for channel in candidates:
            digest = hashlib.sha256(f"{session_key}:{channel.id}".encode("utf-8")).digest()
            value = int.from_bytes(digest[:8], "big") / 2**64
            value = max(value, 1e-12)
            score = -math.log(value) / max(channel.weight, 1)
            if best_score is None or score < best_score:
                best_score = score
                best_channel = channel
        return best_channel

    def _sticky_select_cached(self, session_key: str, candidates: list[Channel]) -> Channel:
        """带缓存的粘性选择。

        缓存命中条件：未过期 且 缓存渠道仍在候选列表中。
        缓存未命中时（过期、渠道被 exclude_ids 排除、或首次访问），
        用 HRW 选出新渠道并覆盖缓存条目。

        注意：当原渠道因故障转移被排除时，缓存会被新渠道永久替换，
        即使原渠道后续恢复，会话也不会回切——这是有意为之的设计，
        避免故障恢复后反复震荡导致流量分布不稳定。
        """
        now = time.time()
        candidate_by_id = {ch.id: ch for ch in candidates}
        entry = self._sticky_cache.get(session_key)
        if entry and now - entry.last_active_at < self._sticky_ttl and entry.channel_id in candidate_by_id:
            entry.last_active_at = now
            self._sticky_cache.move_to_end(session_key)
            return candidate_by_id[entry.channel_id]
        if entry:
            self._sticky_cache.pop(session_key, None)

        selected = self._sticky_select_by_hrw(session_key, candidates)
        self._sticky_cache[session_key] = StickyCacheEntry(selected.id, now)
        self._sticky_cache.move_to_end(session_key)
        self._trim_sticky_cache(now)
        return selected

    def _trim_sticky_cache(self, now: float) -> None:
        expired = []
        for key, entry in self._sticky_cache.items():
            if len(expired) >= 100:
                break
            if now - entry.last_active_at >= self._sticky_ttl:
                expired.append(key)
        for key in expired:
            self._sticky_cache.pop(key, None)
        while len(self._sticky_cache) > self._sticky_cache_max_entries:
            self._sticky_cache.popitem(last=False)

    def _backup_select(self, channels: list[Channel]) -> Channel:
        return sorted(channels, key=lambda ch: (-ch.weight, ch.id))[0]

    def _weighted_round_robin(self, channels: list[Channel]) -> Channel:
        """平滑加权轮询算法

        算法：
        1. 所有 channel 的 current_weight += weight
        2. 选择 current_weight 最大的 channel
        3. 被选中 channel 的 current_weight -= total_weight
        """
        total_weight = sum(ch.weight for ch in channels)

        best: Optional[Channel] = None
        best_health: Optional[ChannelHealth] = None
        for ch in channels:
            health = self._health[ch.id]
            health.current_weight += ch.weight
            # 选择 current_weight 最大的 channel
            if best is None or health.current_weight > best_health.current_weight:
                best = ch
                best_health = health

        # 递减选中channel的current_weight
        best_health.current_weight -= total_weight

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
