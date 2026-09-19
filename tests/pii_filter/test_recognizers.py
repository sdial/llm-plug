from pii_engine import PiiEngine
from pii_recognizers import (
    build_bank_card_rule,
    build_email_rule,
    build_id_card_rule,
    build_phone_rule,
)

ENGINE = PiiEngine()


def _first_match(text: str, rule):
    matches = ENGINE.analyze(text, [rule])
    return matches[0] if matches else None


def test_phone_recognizes_valid_mobile():
    m = _first_match("我的手机是13800138000", build_phone_rule())
    assert m is not None
    assert m.entity_type == "CN_PHONE_NUMBER"


def test_phone_rejects_invalid_segment():
    assert _first_match("号码是11100138000", build_phone_rule()) is None


def test_id_card_recognizes_valid():
    # 110101199003071233 是合法校验位
    m = _first_match("身份证号110101199003071233", build_id_card_rule())
    assert m is not None and m.entity_type == "CN_ID_CARD"


def test_id_card_rejects_bad_checksum():
    assert _first_match("身份证号110101199003071235", build_id_card_rule()) is None


def test_bank_card_recognizes_valid():
    m = _first_match("卡号6222021234567891233", build_bank_card_rule())
    assert m is not None and m.entity_type == "CN_BANK_CARD"


def test_bank_card_rejects_bad_luhn():
    assert _first_match("卡号6222021234567891232", build_bank_card_rule()) is None


def test_email_recognizes_address():
    m = _first_match("联系邮箱 foo@example.com", build_email_rule())
    assert m is not None and m.entity_type == "EMAIL_ADDRESS"


def test_email_rule_does_not_scan_long_non_email_identifier_quadratically():
    assert _first_match("a" * 100_000, build_email_rule()) is None
