import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import request_logs
import whitelist as _whitelist
from client import cleanup_stale_clients, close_all_clients
from config import HOST, MAX_BODY_SIZE, PORT, get_setting, init_settings
from response_state import get_responses_store, reload_responses_store
from routers import admin, proxy_anthropic, proxy_chat, proxy_models, proxy_response
from stats import close_pool as close_stats_pool
from stats import init_db as init_stats_db
from stats import start_stats_workers, stop_stats_workers
from storage import load_api_keys, load_data, register_api_keys_save_callback
from logging_config import configure_level_file_logging

# 静态资源版本号 — 每次更新 JS/CSS 后修改此值即可强制浏览器刷新缓存
STATIC_ASSET_VERSION = "2"

# 配置日志级别文件输出
_log_dir = Path(__file__).parent / "logs"
configure_level_file_logging(_log_dir)


_responses_store = get_responses_store()


async def _session_cleanup_loop():
    """定期清理过期会话文件"""
    interval = get_setting("response_state_cleanup_interval_minutes") or 30
    while True:
        await asyncio.sleep(interval * 60)
        try:
            await _responses_store._cleanup_if_needed()
        except Exception as e:
            logger.warning(f"Session cleanup failed: {e}")


async def _request_log_cleanup_loop():
    """清理过期请求日志记录"""
    await asyncio.sleep(10)
    try:
        await request_logs.cleanup_old_records()
    except Exception as e:
        logger.warning(f"request log cleanup error on startup: {e}")
    while True:
        await asyncio.sleep(86400)
        try:
            await request_logs.cleanup_old_records()
        except Exception as e:
            logger.warning(f"request log cleanup error: {e}")


@asynccontextmanager
async def lifespan(app):
    await init_settings()
    reload_responses_store()
    channels_data = await load_data()
    keys_data = await load_api_keys()
    channel_count = len(channels_data.get("channels", []))
    model_count = len({m for ch in channels_data.get("channels", []) for m in ch.get("models", [])})
    key_count = len(keys_data.get("api_keys", []))
    logger.info(f"就绪: {channel_count} 个渠道, {model_count} 个模型, {key_count} 个 API Key")
    await init_stats_db()
    await request_logs.init_backend()
    start_stats_workers()
    request_logs.start_request_log_workers()

    async def _client_cleanup_loop():
        while True:
            await asyncio.sleep(300)
            try:
                await cleanup_stale_clients(max_age=600)
            except Exception as e:
                logger.warning(f"client cleanup error: {e}")

    cleanup_task = asyncio.create_task(_client_cleanup_loop())
    session_cleanup_task = asyncio.create_task(_session_cleanup_loop())
    request_log_cleanup_task = asyncio.create_task(_request_log_cleanup_loop())
    try:
        yield
    except asyncio.CancelledError:
        pass
    finally:
        cleanup_task.cancel()
        session_cleanup_task.cancel()
        request_log_cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        try:
            await session_cleanup_task
        except asyncio.CancelledError:
            pass
        try:
            await request_log_cleanup_task
        except asyncio.CancelledError:
            pass
        await stop_stats_workers()
        await close_stats_pool()
        await request_logs.close_backend()
        await close_all_clients()

_PROXY_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")

_DATA_DIR = Path(__file__).parent / "data"
_whitelist_cache = _whitelist.WhitelistCache(str(_DATA_DIR / "whitelist.csv"))


_api_key_index: dict[str, dict] | None = None
_api_key_index_lock = asyncio.Lock()


def _invalidate_api_key_index() -> None:
    global _api_key_index
    _api_key_index = None


register_api_keys_save_callback(_invalidate_api_key_index)


async def _get_api_key_index() -> dict[str, dict]:
    global _api_key_index
    if _api_key_index is not None:
        return _api_key_index

    async with _api_key_index_lock:
        if _api_key_index is not None:
            return _api_key_index
        keys_data = await load_api_keys()
        _api_key_index = {
            key.get("key") or "": key
            for key in keys_data.get("api_keys", [])
            if key.get("key")
        }
        return _api_key_index


class CombinedMiddleware:
    """Pure ASGI middleware combining auth and logging - avoids BaseHTTPMiddleware streaming bug."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]

        # IP 白名单检查（对所有 HTTP 请求生效）
        client_ip = (scope.get("client") or ("", 0))[0]
        _wl_rules = _whitelist_cache.get_rules()
        _wl_allowed, _wl_reason = _whitelist.check_request(_wl_rules, path, method, client_ip)
        if not _wl_allowed:
            await self._send_error(send, 403, _wl_reason, "ip_whitelist_error")
            return

        if path in ("/admin", "/admin/"):
            from admin_auth import get_session_cookie_name, validate_admin_session
            session_cookie = None
            for key, value in scope.get("headers", []):
                if key.lower() == b"cookie":
                    cookie_text = value.decode()
                    for part in cookie_text.split(";"):
                        name, _, cookie_value = part.strip().partition("=")
                        if name == get_session_cookie_name():
                            session_cookie = cookie_value
                            break
            if not await validate_admin_session(session_cookie):
                await self._send_redirect(send, "/admin/login")
                return

        if (
            path.startswith("/admin")
            and path not in ("/admin", "/admin/", "/admin/login", "/admin/login/")
            and not path.startswith("/admin/auth")
            and not path.startswith("/admin/static/")
        ):
            from admin_auth import get_session_cookie_name, validate_admin_session
            session_cookie = None
            for key, value in scope.get("headers", []):
                if key.lower() == b"cookie":
                    cookie_text = value.decode()
                    for part in cookie_text.split(";"):
                        name, _, cookie_value = part.strip().partition("=")
                        if name == get_session_cookie_name():
                            session_cookie = cookie_value
                            break
            if not await validate_admin_session(session_cookie):
                await self._send_error(
                    send,
                    401,
                    "Admin login required",
                    "admin_login_required",
                )
                return

        # Only process proxy API requests
        if method != "POST" or path not in _PROXY_PATHS:
            await self.app(scope, receive, send)
            return

        start = time.time()
        ts_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        query = scope.get("query_string", b"").decode()
        headers_dict = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        content_length = headers_dict.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_SIZE:
                    await self._send_error(send, 413, "Request body too large")
                    self._log_request(ts_start, method, path, query, "", False, "", 413, start)
                    return
            except ValueError:
                pass

        # Buffer the request body once
        body_parts = []
        more_body = True
        total_size = 0
        while more_body:
            message = await receive()
            chunk = message.get("body", b"")
            body_parts.append(chunk)
            total_size += len(chunk)
            if total_size > MAX_BODY_SIZE:
                await self._send_error(send, 413, "Request body too large")
                self._log_request(ts_start, method, path, query, "", False, "", 413, start)
                return
            more_body = message.get("more_body", False)
        body_bytes = b"".join(body_parts)

        # Parse body for logging and validation
        model = ""
        stream = False
        try:
            body = json.loads(body_bytes)
            model = body.get("model", "")
            stream = body.get("stream", False)
        except Exception:
            pass

        # Initialize state
        scope.setdefault("state", {})

        # Store body for downstream handlers
        scope["state"]["body_bytes"] = body_bytes

        # Auth check
        api_key_index = await _get_api_key_index()

        if api_key_index:
            # 支持两种认证方式：Authorization: Bearer xxx 或 x-api-key: xxx
            auth_header = headers_dict.get("authorization", "")
            x_api_key = headers_dict.get("x-api-key", "")

            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer "):]
            elif x_api_key:
                token = x_api_key
            else:
                await self._send_error(send, 401, "Missing or invalid Authorization header")
                self._log_request(ts_start, method, path, query, model, stream, "", 401, start)
                return

            matched_key = api_key_index.get(token)
            if matched_key is not None and not secrets.compare_digest(matched_key.get("key") or "", token):
                matched_key = None

            if matched_key is None:
                await self._send_error(send, 401, "Invalid API key")
                self._log_request(ts_start, method, path, query, model, stream, "", 401, start)
                return

            scope["state"]["api_key_id"] = matched_key.get("name") or matched_key.get("id")

            allowed_models = matched_key.get("allowed_models", [])
            if allowed_models and model and model not in allowed_models:
                await self._send_error(send, 403, f"Model '{model}' is not allowed for this API key")
                self._log_request(ts_start, method, path, query, model, stream, "", 403, start)
                return

        scope["state"]["proxy_auth_checked"] = True

        # Create a new receive that returns the buffered body
        body_received = False

        async def buffered_receive() -> Message:
            nonlocal body_received
            if not body_received:
                body_received = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return await receive()

        # Track response status
        response_status: int | None = None
        original_send = send

        async def tracking_send(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 200)
            await original_send(message)

        try:
            await self.app(scope, buffered_receive, tracking_send)
        except Exception:
            if response_status is None:
                response_status = 500
            raise
        finally:
            state = scope.get("state", {})
            channel = state.get("selected_channel_name", "")
            self._log_request(ts_start, method, path, query, model, stream, channel, response_status or 500, start)

    def _log_request(self, ts_start: str, method: str, path: str, query: str,
                     model: str, stream: bool, channel: str, status: int, start: float) -> None:
        qs = f"?{query}" if query else ""
        channel_tag = f" channel={channel}" if channel else ""
        ts_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag = "OK" if status < 400 else "ERR"
        elapsed = time.time() - start
        logger.info(f"[{ts_start}] [REQ]  {method} {path}{qs} model={model} stream={stream}{channel_tag}")
        logger.info(f"[{ts_end}] [RES]  {method} {path}{qs} -> {status} {tag} ({elapsed:.2f}s)")

    async def _send_error(self, send: Send, status: int, message: str, error_type: str = "auth_error") -> None:
        error_body = json.dumps({"error": {"message": message, "type": error_type}}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({
            "type": "http.response.body",
            "body": error_body,
        })

    async def _send_redirect(self, send: Send, location: str) -> None:
        await send({
            "type": "http.response.start",
            "status": 302,
            "headers": [[b"location", location.encode("utf-8")]],
        })
        await send({
            "type": "http.response.body",
            "body": b"",
        })


app = FastAPI(title="LLM API 转换器", version="0.1.0", lifespan=lifespan)

# Add pure ASGI middleware
app.add_middleware(CombinedMiddleware)

# 注册路由
app.include_router(admin.router)
app.include_router(proxy_chat.router)
app.include_router(proxy_response.router)
app.include_router(proxy_anthropic.router)
app.include_router(proxy_models.router)

# 静态文件（管理页面）
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin_static")


@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/admin/")


def _html_response(file_path: Path) -> HTMLResponse:
    """返回 HTML 文件，同时替换静态资源版本占位符。"""
    content = file_path.read_text(encoding="utf-8")
    content = content.replace("__STATIC_ASSET_VERSION__", STATIC_ASSET_VERSION)
    return HTMLResponse(content)


@app.get("/admin/login")
@app.get("/admin/login/")
async def admin_login_page(request: Request):
    from admin_auth import get_session_cookie_name, validate_admin_session

    session_cookie = request.cookies.get(get_session_cookie_name())
    if await validate_admin_session(session_cookie):
        return RedirectResponse(url="/admin/")
    return _html_response(STATIC_DIR / "admin-login.html")


@app.get("/admin")
@app.get("/admin/")
async def admin_index(request: Request):
    from admin_auth import get_session_cookie_name, validate_admin_session

    session_cookie = request.cookies.get(get_session_cookie_name())
    if await validate_admin_session(session_cookie):
        return _html_response(STATIC_DIR / "index.html")
    return _html_response(STATIC_DIR / "admin-login.html")


if __name__ == "__main__":
    import argparse
    import signal
    import socket

    import uvicorn

    parser = argparse.ArgumentParser(description="LLM API 转换器")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"],
                        help="日志级别 (默认: info)")
    parser.add_argument("--no-reload", action="store_true",
                        help="禁用热重载（避免 Windows 下进程退出后端口未释放的问题）")
    args = parser.parse_args()

    import config as _config
    _config.LOG_LEVEL = args.log_level
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "[%(asctime)s] %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": True,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": "[%(asctime)s] %(levelprefix)s %(client_addr)s - \"%(request_line)s\" %(status_code)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": True,
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": args.log_level.upper()},
            "uvicorn.error": {"handlers": ["default"], "level": args.log_level.upper(), "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": args.log_level.upper(), "propagate": False},
        },
    }

    if args.no_reload:
        # 无热重载模式：手动创建 socket 设置 SO_REUSEADDR，确保 Windows 下端口可立即复用
        _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _sock.bind((HOST, PORT))
        _sock.listen(1024)

        config = uvicorn.Config("main:app", log_level=args.log_level, log_config=log_config)
        server = uvicorn.Server(config)

        def _shutdown_handler(sig, frame):
            server.should_exit = True

        signal.signal(signal.SIGINT, _shutdown_handler)
        signal.signal(signal.SIGTERM, _shutdown_handler)

        server.run(sockets=[_sock])
    else:
        # 热重载模式：注意 Windows 下 Ctrl+C 后端口可能短暂占用
        uvicorn.run("main:app", host=HOST, port=PORT, reload=True,
        log_level=args.log_level, log_config=log_config, http="httptools", loop="auto")
