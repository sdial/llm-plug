"""PII 脱敏算子层：mask / replace。

mask 实现中文习惯的"中间遮星"；encrypt 可逆脱敏已随 presidio 移除（能力归 llm-api-guard）。
"""

from __future__ import annotations

from pii_engine import PiiMatch


class MaskOperator:
    """中间遮星算子：保留头尾指定字符，中间替换为 mask 字符串。"""

    def __init__(self, keep_head: int, keep_tail: int, mask: str):
        self.keep_head = keep_head
        self.keep_tail = keep_tail
        self.mask = mask

    def apply(self, text: str, results: list[PiiMatch]) -> str:
        new_text = text
        for r in sorted(results, key=lambda x: x.start, reverse=True):
            masked = self._mask(new_text[r.start : r.end])
            new_text = new_text[: r.start] + masked + new_text[r.end :]
        return new_text

    def _mask(self, value: str) -> str:
        if len(value) <= self.keep_head + self.keep_tail:
            return self.mask
        return value[: self.keep_head] + self.mask + value[-self.keep_tail :]


class ReplaceOperator:
    def __init__(self, new_value: str):
        self.new_value = new_value

    def apply(self, text: str, results: list[PiiMatch]) -> str:
        new_text = text
        for r in sorted(results, key=lambda x: x.start, reverse=True):
            new_text = new_text[: r.start] + self.new_value + new_text[r.end :]
        return new_text


def build_operators_map() -> dict[str, MaskOperator | ReplaceOperator]:
    """返回 entity_type -> operator 的固定映射。"""
    return {
        "CN_PHONE_NUMBER": MaskOperator(keep_head=3, keep_tail=4, mask="****"),
        "CN_ID_CARD": MaskOperator(keep_head=6, keep_tail=4, mask="********"),
        "CN_BANK_CARD": MaskOperator(keep_head=6, keep_tail=4, mask="********"),
        "EMAIL_ADDRESS": MaskOperator(keep_head=2, keep_tail=2, mask="****"),
        "DEFAULT": ReplaceOperator(new_value="[REDACTED]"),
    }
