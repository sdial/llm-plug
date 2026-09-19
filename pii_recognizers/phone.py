"""中国手机号识别规则。"""

import re

from pii_engine import PiiRule

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def _valid_segment(pattern_text: str) -> bool:
    # 原实现原样移植：长度 11 且第二位非 1（正则本身已约束，冗余但无害）
    return len(pattern_text) == 11 and pattern_text[1] != "1"


def build_rule() -> PiiRule:
    return PiiRule(
        name="cn_phone_number",
        entity_type="CN_PHONE_NUMBER",
        pattern=_PHONE_RE,
        validator=_valid_segment,
    )
