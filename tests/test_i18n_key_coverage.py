"""候选 5 — i18n 键校验缝。

消灭「静默英文 UI」整条生产线：扫描全部 JS + htmx 片段 + index.html 中使用的
`data-i18n` 属性键与 `I18n.t()` 调用键，断言中英字典均定义；并断言字典无死键
（防止增删键靠肉眼、防止键拼错后静默回退到英文或键名本身）。

实现：跑 `tests/_tools/i18n_key_scan.mjs`（node 实际执行两本字典 JS 得到真实键集，
避免在 Python 里手写 JS 解析器）。node 缺失时跳过（仓库已有 node --test 测试，属预期环境）。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).parent / "_tools"
SCAN_SCRIPT = TOOLS_DIR / "i18n_key_scan.mjs"
STATIC_DIR = Path("static")


def _scan():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过 i18n 键校验（仓库 node 测试同样依赖 node）")
    proc = subprocess.run(
        [node, str(SCAN_SCRIPT)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_referenced_i18n_key_is_defined_in_both_dicts():
    """所有被引用的键（data-i18n 属性 + I18n.t 字面量 + 绑定别名 tokens() +
    stream-test 动态键白名单）必须在中英两本字典里都有定义；否则静默回退英文/键名。"""
    result = _scan()
    assert not result["missingZh"], f"缺失于中文字典: {result['missingZh']}"
    assert not result["missingEn"], f"缺失于英文字典: {result['missingEn']}"


def test_no_dead_i18n_keys_in_dicts():
    """字典中不应存在从未被引用的死键（防止改名/删除代码后字典残留、防笔误复制）。"""
    result = _scan()
    assert not result["dead"], f"字典死键（未被任何代码引用）: {result['dead']}"


def test_dicts_zh_and_en_have_identical_key_sets():
    """中英字典键集合必须完全一致（平行结构）；键数量不一致说明增删键时漏改了一本。"""
    result = _scan()
    assert result["dictZh"] == result["dictEn"], f"中英字典键数量不一致: zh={result['dictZh']} en={result['dictEn']}"


def test_i18n_scan_covers_all_static_sources():
    """扫描器必须覆盖全部静态源（JS + 片段 + 独立页），确保校验缝不因新增文件而漏网。"""
    scan_src = SCAN_SCRIPT.read_text(encoding="utf-8")
    assert "collect('static', files)" in scan_src
    assert "data-i18n-html" in scan_src
    assert "data-i18n-placeholder" in scan_src
    assert "data-i18n-title" in scan_src
    assert "data-i18n-aria" in scan_src
    # 动态键白名单（stream-test 用 keys 表映射状态）必须在扫描器里显式声明
    assert "streamTest.statusReady" in scan_src
    # 绑定别名 tokens()（requests.js 用 I18n.t.bind 后的局部函数）必须被捕获
    assert "tokens?" in scan_src
