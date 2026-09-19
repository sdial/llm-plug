"""Tab 生命周期与 requests/stats 前端行为测试的 pytest 包装（ADR-0020 D0）。

tests/test_tab_lifecycle.mjs 在 node 里真实求值 tab_runtime.js + stats.js +
requests.js（共享同一 window，业务模块 self-register 进真实 TabRuntime），
守护定时器生命周期归属、深链恢复契约、token 渲染与时区聚合等行为。本文件让
`uv run pytest` 全量门禁把该 node 套件一并纳入。node 缺失时跳过。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "test_tab_lifecycle.mjs"


def test_tab_lifecycle_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过 tab 生命周期行为测试（仓库 node 测试同样依赖 node）")
    proc = subprocess.run(
        [node, "--test", str(NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"node --test 失败:\n{proc.stdout}\n{proc.stderr}"
