#!/usr/bin/env python
"""Simple server to serve the session viewer with access to logs."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from logging_config import configure_level_file_logging

BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
LOGS_DIR = BASE_DIR / "logs"


def configure_logging(log_dir: Path | str = LOGS_DIR) -> list[int]:
    return configure_level_file_logging(log_dir)


class SessionViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002
        logger.info(f"[{self.address_string()}] {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        logger.info(f"GET {path}")

        # API endpoint to list log files
        if path == "/api/logs":
            self.send_json_response(self._list_logs())
            return

        # Serve logs files
        if path.startswith("/logs/"):
            filename = path[6:]
            file_path = (LOGS_DIR / filename).resolve()
            if not file_path.is_relative_to(LOGS_DIR.resolve()):
                self.send_error(403, "Forbidden")
                return
            if file_path.exists() and file_path.is_file():
                self.send_file_response(file_path, "application/jsonl")
            else:
                self.send_error(404, f"File not found: {filename}")
            return

        # Serve static files
        if path == "/":
            path = "/index.html"

        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not file_path.is_relative_to(STATIC_DIR.resolve()):
            self.send_error(403, "Forbidden")
            return
        if file_path.exists() and file_path.is_file():
            content_type = self._get_content_type(file_path)
            self.send_file_response(file_path, content_type)
        else:
            self.send_error(404, f"File not found: {path}")

    def _list_logs(self):
        files = sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)
        return [{"name": f.name, "size": f.stat().st_size} for f in files]

    def _get_content_type(self, file_path):
        suffix = file_path.suffix.lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "application/javascript",
            ".json": "application/json",
            ".jsonl": "application/jsonl",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
        }
        return types.get(suffix, "application/octet-stream")

    def send_json_response(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_file_response(self, file_path, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(file_path.read_bytes())


def main(port=8080):
    configure_logging()
    logger.info(f"BASE_DIR: {BASE_DIR}")
    logger.info(f"STATIC_DIR: {STATIC_DIR}")
    logger.info(f"LOGS_DIR: {LOGS_DIR}")
    logger.info(
        f"Session Viewer running at http://localhost:{port}/session-viewer.html"
    )
    logger.info("Press Ctrl+C to stop")

    # 仅绑定 loopback：viewer 无认证，任何能访问该端口的人都能读到 logs/ 下的会话记录。
    server = HTTPServer(("127.0.0.1", port), SessionViewerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped.")
        server.shutdown()


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    main(port)
