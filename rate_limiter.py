"""上游限速适配：滑动窗口限速器。

用于在请求发送前按渠道 RPM（每分钟请求数）节流，把突发流量在代理侧
平滑掉，从源头降低上游 429 触发概率（如 NVIDIA 免费层 40 RPM 限制）。

语义：每个 key（渠道 id）最近 window_seconds 内最多允许 max_requests
次 acquire 成功。超出时调用方等待最早的许可滑出窗口；等待超时返回 False。

纯 asyncio 实现，无阻塞调用；等待用小步轮询（0.5s），保证窗口滑动后
能及时唤醒，也便于请求取消。
"""

import asyncio
import time
from collections import deque


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        key: str,
        max_requests: int,
        window_seconds: float = 60.0,
        wait_timeout: float | None = None,
    ) -> bool:
        """获取一个发送许可。

        Args:
            key: 限速维度（渠道 id）
            max_requests: 窗口内最大请求数（即 RPM）
            window_seconds: 窗口时长（秒）
            wait_timeout: 无许可时的最大等待秒数；None = 无限等待；0 = 立即放弃

        Returns:
            True = 已获得许可；False = 等待超时未获得
        """
        if max_requests <= 0:
            return True
        deadline = None if wait_timeout is None else time.monotonic() + max(0.0, wait_timeout)

        while True:
            async with self._lock:
                now = time.monotonic()
                q = self._windows.get(key)
                if q is None:
                    q = self._windows[key] = deque()
                # 滑出已过期的时间戳
                while q and now - q[0] >= window_seconds:
                    q.popleft()
                if len(q) < max_requests:
                    q.append(now)
                    return True
                # 没有许可：需要等最早的许可过期
                wait_needed = q[0] + window_seconds - now
            if deadline is not None and time.monotonic() + wait_needed >= deadline:
                return False
            # 小步轮询：窗口滑动即醒来重试，同时保证可取消
            await asyncio.sleep(min(wait_needed, 0.5))

    def remove_key(self, key: str) -> None:
        """删除且只删除一个 Channel 的发送窗口。"""
        self._windows.pop(key, None)


rate_limiter = SlidingWindowRateLimiter()
