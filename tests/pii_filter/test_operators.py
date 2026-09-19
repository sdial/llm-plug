from pii_engine import PiiMatch
from pii_operators import MaskOperator, ReplaceOperator


def test_mask_phone_middle_stars():
    op = MaskOperator(keep_head=3, keep_tail=4, mask="****")
    text = "我的手机是13800138000"
    result = op.apply(
        text,
        [PiiMatch(entity_type="CN_PHONE_NUMBER", start=5, end=16)],
    )
    assert result == "我的手机是138****8000"


def test_mask_id_card():
    op = MaskOperator(keep_head=6, keep_tail=4, mask="********")
    text = "身份证110101199003071234"
    result = op.apply(
        text,
        [PiiMatch(entity_type="CN_ID_CARD", start=3, end=21)],
    )
    assert result == "身份证110101********1234"


def test_replace_operator():
    op = ReplaceOperator(new_value="[REDACTED]")
    text = "邮箱 foo@example.com 在这里"
    result = op.apply(
        text,
        [PiiMatch(entity_type="EMAIL_ADDRESS", start=3, end=18)],
    )
    assert result == "邮箱 [REDACTED] 在这里"
