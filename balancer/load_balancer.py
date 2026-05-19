import asyncio
import time
from collections import defaultdict
from typing import Optional

from models.channel import Channel


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

    def update_config(self, max_fail_count: int = 5, cooldown_seconds: int = 60):
        """热更新配置参数"""
        self._max_fail_count = max_fail_count
        self._cooldown_seconds = float(cooldown_seconds)

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
            available = [
                ch
                for ch in channels
                if ch.enabled
                and ch.id not in exclude_ids
                and self._health[ch.id].is_healthy(self._max_fail_count, self._cooldown_seconds)
            ]
            if not available:
                return None

            available.sort(key=lambda ch: ch.priority)
            min_priority = available[0].priority
            top_group = [ch for ch in available if ch.priority == min_priority]

            if len(top_group) == 1:
                return top_group[0]

            return self._weighted_round_robin(top_group)

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


load_balancer = LoadBalancer()
