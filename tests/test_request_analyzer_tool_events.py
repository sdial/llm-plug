"""request-analyzer 纯函数 node --test 的 pytest 包装（ADR-0020 D0）。

tests/test_request_analyzer_tool_events.mjs 经 IIFE 提取法真实求值
static/js/request-analyzer.js 的工具事件提取与消息/输出 normalizer（工具事件
配对、redacted thinking、legacy function_call、responses function_call 等）。
此前该套件未纳入 pytest 门禁且已被 src 漂移静默打破；本文件恢复其门禁地位。
node 缺失时跳过。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "test_request_analyzer_tool_events.mjs"


def test_request_analyzer_tool_events_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过 request-analyzer 纯函数测试（仓库 node 测试同样依赖 node）")
    proc = subprocess.run(
        [node, "--test", str(NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"node --test 失败:\n{proc.stdout}\n{proc.stderr}"
