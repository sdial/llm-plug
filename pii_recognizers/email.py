"""邮箱识别规则。"""

import re

from pii_engine import PiiRule

_EMAIL_FULL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def _full_address(pattern_text: str) -> bool:
    # 命中片段必须是完整地址（TLD 至少 2 位），原 validate_result 语义
    return bool(_EMAIL_FULL_RE.fullmatch(pattern_text))


def build_rule() -> PiiRule:
    return PiiRule(
        name="email_address",
        entity_type="EMAIL_ADDRESS",
        # 左侧边界使无 @ 的超长字母串只在首字符尝试一次，避免逐位置回溯的 O(n²)。
        pattern=re.compile(r"(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        validator=_full_address,
    )
