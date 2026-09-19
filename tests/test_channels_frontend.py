"""渠道前端行为测试的 pytest 包装（ADR-0020 D0 示证票）。

渠道子域模块的纯函数（列表过滤）与 DOM 耦合函数（卡片渲染/事件委托）由
tests/test_channels_frontend.mjs 在 node 里真实求值——那是列表过滤、
可访问名与 Anthropic 配置节切换行为的回归住所。本文件让 `uv run pytest` 全量门禁
把该 node 套件一并纳入。node 缺失时跳过（与 tests/test_i18n_key_coverage.py 同一
预期环境）。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "test_channels_frontend.mjs"


def test_channels_frontend_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过渠道前端行为测试（仓库 node 测试同样依赖 node）")
    proc = subprocess.run(
        [node, "--test", str(NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"node --test 失败:\n{proc.stdout}\n{proc.stderr}"
