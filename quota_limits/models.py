"""quota_limits 数据模型。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class QuotaLimitInfo:
    """窗口级限速识别结果。

    reset_at 为 aware UTC；None 表示响应未给出恢复时刻。
    """

    code: str
    reset_at: datetime | None
    raw: Any
