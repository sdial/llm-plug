"""中国居民身份证识别规则（GB 11643 校验码验证）。"""

import re

from pii_engine import PiiRule

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_CHECKSUM_CHARS = "10X98765432"

_ID_CARD_RE = re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")


def checksum_valid(number: str) -> bool:
    if len(number) != 18 or not number[:17].isdigit():
        return False
    total = sum(int(number[i]) * _WEIGHTS[i] for i in range(17))
    return _CHECKSUM_CHARS[total % 11] == number[17].upper()


def build_rule() -> PiiRule:
    return PiiRule(
        name="cn_id_card",
        entity_type="CN_ID_CARD",
        pattern=_ID_CARD_RE,
        validator=checksum_valid,
    )
