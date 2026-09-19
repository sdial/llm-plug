"""依赖方向守卫（ADR-0023 D0）：converters 为纯转换域，禁止 import proxy。

先例：ADR-0020 结构守卫测试模式（tests/test_static_admin_split.py）——
pathlib 扫描 + 纯文本断言，防依赖方向回滑（包级循环复发）。
"""

import re
from pathlib import Path

CONVERTERS_DIR = Path("converters")
_PROXY_IMPORT_RE = re.compile(r"^\s*(?:from\s+proxy[\s.]|import\s+proxy[\s.]?)")


def _py_sources(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def test_converters_never_import_proxy():
    """ADR-0023 D0：converters 全目录零 proxy 依赖——依赖单向 proxy → converters。"""
    offenders: list[str] = []
    for path in _py_sources(CONVERTERS_DIR):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _PROXY_IMPORT_RE.match(line):
                offenders.append(f"{path.as_posix()}:{lineno}: {line.strip()}")
    assert not offenders, "converters 不得 import proxy（ADR-0023 依赖方向守卫）:\n" + "\n".join(offenders)


def test_proxy_event_factory_original_locations_dissolved():
    """ADR-0023 D0：事件工厂原住所已整体迁出，零 re-export 壳（ADR-0019 规矩）。"""
    assert not (Path("proxy") / "stream_sse.py").exists()
    assert not (Path("proxy") / "stream_usage.py").exists()
    assert (CONVERTERS_DIR / "stream_events.py").exists()
    assert (CONVERTERS_DIR / "stream_usage.py").exists()
