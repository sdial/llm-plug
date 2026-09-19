"""轻量 PII 正则引擎：替代 presidio-analyzer 的 PatternRecognizer 子集。

只做三件事：正则匹配、validator 否决、重叠区间消解（长区间保留）。
规则分（score）已删除：命中即有效，阈值设计随之移除。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PiiMatch:
    """一段命中区间，语义对齐 presidio 的 RecognizerResult。"""

    entity_type: str
    start: int
    end: int


@dataclass(frozen=True)
class PiiRule:
    """一条识别规则：预编译正则 + 可选否决校验器。"""

    name: str
    entity_type: str
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None


class PiiEngine:
    """无状态引擎；rules 由调用方按 settings 构建后传入，便于缓存复用。"""

    def analyze(self, text: str, rules: Sequence[PiiRule]) -> list[PiiMatch]:
        candidates: list[PiiMatch] = []
        for rule in rules:
            for m in rule.pattern.finditer(text):
                if rule.validator is not None and not rule.validator(m.group(0)):
                    continue
                candidates.append(PiiMatch(rule.entity_type, m.start(), m.end()))
        return sorted(self._resolve_overlaps(candidates), key=lambda x: x.start)

    @staticmethod
    def _resolve_overlaps(candidates: list[PiiMatch]) -> list[PiiMatch]:
        # 无 score 后按"长区间优先、同长靠前保留"贪心消解重叠
        kept: list[PiiMatch] = []
        occupied: list[tuple[int, int]] = []
        for m in sorted(candidates, key=lambda x: (-(x.end - x.start), x.start)):
            if any(not (m.end <= s or m.start >= e) for s, e in occupied):
                continue
            kept.append(m)
            occupied.append((m.start, m.end))
        return kept
