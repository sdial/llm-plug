"""方舟（Volcengine Ark）CODING plan 窗口级限速适配器。

方舟 429 body 示例：
{
  "error": {
    "code": "AccountQuotaExceeded",
    "message": "You have exceeded the 5-hour usage quota. It will reset at
                2026-08-15 19:23:44 +0800 CST. Request id: ...",
    "param": "",
    "type": "TooManyRequests"
  }
}
"""

import re
from datetime import UTC, datetime

from quota_limits.models import QuotaLimitInfo

_RESET_RE = re.compile(r"will reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})")
_RESET_FMT = "%Y-%m-%d %H:%M:%S %z"


def match(body) -> QuotaLimitInfo | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error") or {}
    if not isinstance(error, dict) or error.get("code") != "AccountQuotaExceeded":
        return None
    message = str(error.get("message") or "")
    reset_at = None
    m = _RESET_RE.search(message)
    if m:
        try:
            reset_at = datetime.strptime(m.group(1), _RESET_FMT).astimezone(UTC)
        except ValueError:
            reset_at = None
    return QuotaLimitInfo(code="AccountQuotaExceeded", reset_at=reset_at, raw=body)
