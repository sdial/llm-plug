"""P0-3: serve_viewer 基本测试 — 路径安全、内容类型、日志列表"""

from pathlib import Path

import serve_viewer
from loguru import logger


# ═══════════════════════════════════════════
#  SessionViewerHandler._get_content_type
# ═══════════════════════════════════════════

class TestGetContentType:

    def _make_handler(self):
        """创建 handler 实例而无需启动服务器"""
        h = serve_viewer.SessionViewerHandler.__new__(serve_viewer.SessionViewerHandler)
        return h

    def test_html_content_type(self):
        h = self._make_handler()
        assert h._get_content_type(Path("index.html")) == "text/html; charset=utf-8"

    def test_js_content_type(self):
        h = self._make_handler()
        assert h._get_content_type(Path("app.js")) == "application/javascript"

    def test_css_content_type(self):
        h = self._make_handler()
        assert h._get_content_type(Path("style.css")) == "text/css"

    def test_json_content_type(self):
        h = self._make_handler()
        assert h._get_content_type(Path("data.json")) == "application/json"

    def test_jsonl_content_type(self):
        h = self._make_handler()
        assert h._get_content_type(Path("logs.jsonl")) == "application/jsonl"

    def test_png_content_type(self):
        h = self._make_handler()
        assert h._get_content_type(Path("logo.png")) == "image/png"

    def test_svg_content_type(self):
        h = self._make_handler()
        assert h._get_content_type(Path("icon.svg")) == "image/svg+xml"

    def test_unknown_extension_fallback(self):
        h = self._make_handler()
        assert h._get_content_type(Path("file.xyz")) == "application/octet-stream"

    def test_case_insensitive_suffix(self):
        h = self._make_handler()
        assert h._get_content_type(Path("PAGE.HTML")) == "text/html; charset=utf-8"


# ═══════════════════════════════════════════
#  路径安全检查（is_relative_to）
# ═══════════════════════════════════════════

class TestPathTraversalSafety:

    def test_logs_path_traversal_blocked(self):
        """路径穿越 ../../etc/passwd 应被 is_relative_to 拦截"""
        from serve_viewer import LOGS_DIR
        malicious = (LOGS_DIR / ".." / ".." / "etc" / "passwd").resolve()
        assert not malicious.is_relative_to(LOGS_DIR.resolve())

    def test_static_path_traversal_blocked(self):
        from serve_viewer import STATIC_DIR
        malicious = (STATIC_DIR / ".." / ".." / "etc" / "passwd").resolve()
        assert not malicious.is_relative_to(STATIC_DIR.resolve())

    def test_normal_logs_path_allowed(self):
        from serve_viewer import LOGS_DIR
        normal = (LOGS_DIR / "session.jsonl").resolve()
        assert normal.is_relative_to(LOGS_DIR.resolve())

    def test_normal_static_path_allowed(self):
        from serve_viewer import STATIC_DIR
        normal = (STATIC_DIR / "index.html").resolve()
        assert normal.is_relative_to(STATIC_DIR.resolve())


# ═══════════════════════════════════════════
#  _list_logs
# ═══════════════════════════════════════════

class TestListLogs:

    def test_list_logs_returns_jsonl_files(self, tmp_path, monkeypatch):
        """_list_logs 应返回 logs 目录下的 .jsonl 文件"""
        # 创建一些模拟日志文件
        (tmp_path / "log1.jsonl").write_text('{"msg":"a"}\n')
        (tmp_path / "log2.jsonl").write_text('{"msg":"b"}\n')
        (tmp_path / "readme.txt").write_text("not a log")

        monkeypatch.setattr(serve_viewer, "LOGS_DIR", tmp_path)

        h = serve_viewer.SessionViewerHandler.__new__(serve_viewer.SessionViewerHandler)
        result = h._list_logs()

        names = [item["name"] for item in result]
        assert "log1.jsonl" in names
        assert "log2.jsonl" in names
        assert "readme.txt" not in names

    def test_list_logs_sorted_reverse(self, tmp_path, monkeypatch):
        """结果按文件名逆序排列（最新的在前）"""
        (tmp_path / "a.jsonl").write_text("")
        (tmp_path / "b.jsonl").write_text("")
        (tmp_path / "c.jsonl").write_text("")

        monkeypatch.setattr(serve_viewer, "LOGS_DIR", tmp_path)

        h = serve_viewer.SessionViewerHandler.__new__(serve_viewer.SessionViewerHandler)
        result = h._list_logs()

        names = [item["name"] for item in result]
        assert names == sorted(names, reverse=True)

    def test_list_logs_empty_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(serve_viewer, "LOGS_DIR", tmp_path)
        h = serve_viewer.SessionViewerHandler.__new__(serve_viewer.SessionViewerHandler)
        result = h._list_logs()
        assert result == []

    def test_list_logs_includes_size(self, tmp_path, monkeypatch):
        content = b'{"key": "value"}\n'
        (tmp_path / "test.jsonl").write_bytes(content)
        monkeypatch.setattr(serve_viewer, "LOGS_DIR", tmp_path)

        h = serve_viewer.SessionViewerHandler.__new__(serve_viewer.SessionViewerHandler)
        result = h._list_logs()
        assert len(result) == 1
        assert result[0]["size"] == len(content)


# ═══════════════════════════════════════════
#  main() 绑定 loopback 检查
# ═══════════════════════════════════════════

class TestMainBinding:

    def test_main_binds_to_localhost_only(self):
        """源码验证：仅绑定 127.0.0.1"""
        import inspect
        source = inspect.getsource(serve_viewer.main)
        assert "127.0.0.1" in source


class TestViewerLogging:

    def test_configure_logging_writes_standard_level_files(self, tmp_path):
        """viewer 应复用主服务的 loguru 分级文件输出。"""
        handler_ids = serve_viewer.configure_logging(tmp_path)
        try:
            logger.warning("viewer warning smoke")
            logger.error("viewer error smoke")
            logger.critical("viewer critical smoke")
        finally:
            for handler_id in handler_ids:
                logger.remove(handler_id)

        assert "viewer warning smoke" in (tmp_path / "warning.log").read_text(encoding="utf-8")
        assert "viewer error smoke" in (tmp_path / "error.log").read_text(encoding="utf-8")
        assert "viewer critical smoke" in (tmp_path / "critical.log").read_text(encoding="utf-8")
