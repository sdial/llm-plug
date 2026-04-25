import time
from collections import defaultdict
from typing import Optional

from models.channel import Channel
from config import MAX_FAIL_COUNT, COOLDOWN_SECONDS


class ChannelHealth:
    """跟踪单个渠道的健康状态"""

    def __init__(self):
        self.fail_count: int = 0
        self.last_fail_time: float = 0
        self.current_weight: int = 0  # 用于加权轮询的当前权重

    def record_success(self):
        self.fail_count = 0

    def record_failure(self):
        self.fail_count += 1
        self.last_fail_time = time.time()

    @property
    def is_healthy(self) -> bool:
        """只读：冷却结束后仍视为可选，成功/失败由 record_* 更新计数。"""
        if self.fail_count < MAX_FAIL_COUNT:
            return True
        return (time.time() - self.last_fail_time) > COOLDOWN_SECONDS


class LoadBalancer:
    """优先级分组 + 加权轮询负载均衡器"""

    def __init__(self):
        self._health: dict[str, ChannelHealth] = defaultdict(ChannelHealth)

    def get_health(self, channel_id: str) -> ChannelHealth:
        return self._health[channel_id]

    def record_success(self, channel_id: str):
        self._health[channel_id].record_success()

    def record_failure(self, channel_id: str):
        self._health[channel_id].record_failure()

    def select_channel(
        self,
        channels: list[Channel],
        exclude_ids: set[str] | None = None,
    ) -> Optional[Channel]:
        """
        从候选渠道中选择一个：
        1. 过滤掉禁用、不健康及 exclude_ids 中的渠道
        2. 按优先级分组
        3. 在最高优先级组内加权轮询
        """
        exclude_ids = exclude_ids or set()
        # 过滤可用渠道
        available = [
            ch
            for ch in channels
            if ch.enabled
            and ch.id not in exclude_ids
            and self._health[ch.id].is_healthy
        ]
        if not available:
            return None

        # 按优先级分组
        available.sort(key=lambda ch: ch.priority)
        min_priority = available[0].priority
        top_group = [ch for ch in available if ch.priority == min_priority]

        if len(top_group) == 1:
            return top_group[0]

        # 加权轮询 (Smooth Weighted Round-Robin)
        return self._weighted_round_robin(top_group)

    def _weighted_round_robin(self, channels: list[Channel]) -> Channel:
        """平滑加权轮询算法"""
        total_weight = sum(ch.weight for ch in channels)

        # 增加每个渠道的当前权重
        best: Optional[Channel] = None
        best_health: Optional[ChannelHealth] = None
        for ch in channels:
            health = self._health[ch.id]
            health.current_weight += ch.weight
            if best is None or health.current_weight > best_health.current_weight:
                best = ch
                best_health = health

        # 减去总权重
        best_health.current_weight -= total_weight
        return best


# 全局单例
load_balancer = LoadBalancer()
