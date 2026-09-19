"""供应商适配器注册表。

新增供应商步骤（详见 docs/quota-limits.md）：
1. 在 quota_limits/vendors/ 新建 <vendor>.py，实现 match(body) -> QuotaLimitInfo | None；
2. 在本文件 _ADAPTERS 列表追加 ("<vendor>", <vendor>.match)。

检测按 _ADAPTERS 顺序执行，先命中先返回。
"""

from collections.abc import Callable
from typing import Any

from quota_limits.models import QuotaLimitInfo

from . import ark

_ADAPTERS: list[tuple[str, Callable[[Any], QuotaLimitInfo | None]]] = [
    ("ark", ark.match),
]

__all__ = ["_ADAPTERS"]
