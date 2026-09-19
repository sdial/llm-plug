"""Request-detail Node behavior test wrapper."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TEST = Path(__file__).parent / "test_requests_frontend.mjs"


def test_requests_frontend_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过请求详情前端行为测试")
    proc = subprocess.run(
        [node, "--test", str(NODE_TEST)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"node --test 失败:\n{proc.stdout}\n{proc.stderr}"
