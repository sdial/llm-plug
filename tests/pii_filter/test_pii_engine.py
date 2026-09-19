import re

from pii_engine import PiiEngine, PiiRule


def _rule(entity: str, pattern: str, validator=None) -> PiiRule:
    return PiiRule(
        name=f"r_{entity}",
        entity_type=entity,
        pattern=re.compile(pattern),
        validator=validator,
    )


def test_single_match_offsets():
    engine = PiiEngine()
    matches = engine.analyze("abc 13800138000 xyz", [_rule("PHONE", r"\d{11}")])
    assert len(matches) == 1
    m = matches[0]
    assert (m.entity_type, m.start, m.end) == ("PHONE", 4, 15)


def test_longer_match_wins_overlap():
    engine = PiiEngine()
    rules = [
        _rule("WIDE", r"\d{6,}"),
        _rule("NARROW", r"\d{4}"),
    ]
    matches = engine.analyze("123456", rules)
    assert [m.entity_type for m in matches] == ["WIDE"]


def test_earlier_match_wins_equal_length():
    engine = PiiEngine()
    rules = [
        _rule("A", r"1\d{2}"),
        _rule("B", r"\d{3}"),
    ]
    matches = engine.analyze("123", rules)
    assert [m.entity_type for m in matches] == ["A"]


def test_disjoint_matches_all_kept_and_sorted_by_start():
    engine = PiiEngine()
    rules = [
        _rule("B", r"[a-z]{3}"),
        _rule("A", r"\d{3}"),
    ]
    matches = engine.analyze("123 abc", rules)
    assert [(m.entity_type, m.start) for m in matches] == [("A", 0), ("B", 4)]


def test_validator_drops_match():
    engine = PiiEngine()
    rules = [_rule("X", r"\d+", validator=lambda _t: False)]
    assert engine.analyze("123", rules) == []


def test_validator_filters_selectively():
    engine = PiiEngine()
    rules = [_rule("X", r"\d+", validator=lambda t: t == "123")]
    matches = engine.analyze("123 456", rules)
    assert [m.entity_type for m in matches] == ["X"]


def test_no_rules_returns_empty():
    assert PiiEngine().analyze("任何文本", []) == []
