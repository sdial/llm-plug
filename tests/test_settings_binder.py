"""设置页绑定器纯函数 node --test 的 pytest 包装（ADR-0018 D1）。

纯函数（解析归一/单位换算/脏判定/diff 载荷/校验/detail 提取）实现在
static/js/settings.js 内，经 IIFE 提取法由 tests/test_settings_binder.mjs 直测——
那是 NaN 类缺陷修复的回归住所。本文件让 `uv run pytest` 全量门禁把该 node 套件
一并纳入。node 缺失时跳过（与 tests/test_i18n_key_coverage.py 同一预期环境）。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "test_settings_binder.mjs"


def test_settings_binder_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过设置绑定器纯函数测试（仓库 node 测试同样依赖 node）")
    proc = subprocess.run(
        [node, "--test", str(NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"node --test 失败:\n{proc.stdout}\n{proc.stderr}"
