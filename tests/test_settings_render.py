"""设置页渲染纯函数 node --test 的 pytest 包装（ADR-0018 D2/票03）。

渲染构造（label/help/hot pill/换算 min-max/choices/readonly/后缀变体/挂载点
匹配）实现在 static/js/settings.js 内，经 IIFE 提取法由 tests/test_settings_render.mjs
直测——那是"渲染结果与手写版可见内容等价"基线的回归住所。本文件让
`uv run pytest` 全量门禁把该 node 套件一并纳入（与 tests/test_settings_binder.py
同一模式）。node 缺失时跳过。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "test_settings_render.mjs"


def test_settings_render_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过设置渲染纯函数测试（仓库 node 测试同样依赖 node）")
    proc = subprocess.run(
        [node, "--test", str(NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"node --test 失败:\n{proc.stdout}\n{proc.stderr}"
