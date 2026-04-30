import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from client import close_all_clients
from config import DEBUG, HOST, PORT
from routers import admin, proxy_chat, proxy_response, proxy_anthropic, proxy_models
from stats import init_db as init_stats_db, close_pool as close_stats_pool
from storage import load_data, load_api_keys


@asynccontextmanager
async def lifespan(app):
    # 启动预热：提前加载数据到缓存，避免首次请求同步读磁盘
    channels_data = load_data()
    keys_data = load_api_keys()
    channel_count = len(channels_data.get("channels", []))
    model_count = len({m for ch in channels_data.get("channels", []) for m in ch.get("models", [])})
    key_count = len(keys_data.get("api_keys", []))
    print(f"[STARTUP] 就绪: {channel_count} 个渠道, {model_count} 个模型, {key_count} 个 API Key")
    await init_stats_db()
    try:
        yield
    except asyncio.CancelledError:
        pass  # suppress CancelledError from signal handling on Windows/Python 3.14
    await close_stats_pool()
    await close_all_clients()


from starlette.types import ASGIApp, Receive, Scope, Send, Message

_PROXY_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")


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

        # Only process proxy API requests
        if method != "POST" or path not in _PROXY_PATHS:
            await self.app(scope, receive, send)
            return

        start = time.time()
        ts_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        query = scope.get("query_string", b"").decode()

        # Buffer the request body once
        body_parts = []
        more_body = True
        while more_body:
            message = await receive()
            body_parts.append(message.get("body", b""))
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

        # Store tracked headers
        from config import STATS_TRACKED_HEADERS, TRACK_ALL_HEADERS
        headers_dict = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        if TRACK_ALL_HEADERS:
            scope["state"]["tracked_headers"] = headers_dict
        else:
            scope["state"]["tracked_headers"] = {
                k: v for k, v in headers_dict.items()
                if k.lower() in [h.lower() for h in STATS_TRACKED_HEADERS]
            }

        # Store body for downstream handlers
        scope["state"]["body_bytes"] = body_bytes

        # Auth check
        keys_data = load_api_keys()
        api_keys = keys_data.get("api_keys", [])

        if api_keys:
            auth_header = headers_dict.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                await self._send_error(send, 401, "Missing or invalid Authorization header")
                self._log_request(ts_start, method, path, query, model, stream, "", 401, start)
                return
            token = auth_header[len("Bearer "):]

            matched_key = None
            for key in api_keys:
                if key.get("key") == token:
                    matched_key = key
                    break

            if matched_key is None:
                await self._send_error(send, 401, "Invalid API key")
                self._log_request(ts_start, method, path, query, model, stream, "", 401, start)
                return

            scope["state"]["api_key_id"] = matched_key.get("id")

            allowed_models = matched_key.get("allowed_models", [])
            if allowed_models and model and model not in allowed_models:
                await self._send_error(send, 403, f"Model '{model}' is not allowed for this API key")
                self._log_request(ts_start, method, path, query, model, stream, "", 403, start)
                return

        # Create a new receive that returns the buffered body
        body_received = False

        async def buffered_receive() -> Message:
            nonlocal body_received
            if not body_received:
                body_received = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return await receive()

        # Track response status
        response_status = 200
        original_send = send

        async def tracking_send(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 200)
            await original_send(message)

        try:
            await self.app(scope, buffered_receive, tracking_send)
        finally:
            state = scope.get("state", {})
            channel = state.get("selected_channel_name", "")
            self._log_request(ts_start, method, path, query, model, stream, channel, response_status, start)

    def _log_request(self, ts_start: str, method: str, path: str, query: str,
                     model: str, stream: bool, channel: str, status: int, start: float) -> None:
        qs = f"?{query}" if query else ""
        channel_tag = f" channel={channel}" if channel else ""
        ts_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag = "OK" if status < 400 else "ERR"
        elapsed = time.time() - start
        print(f"[{ts_start}] [REQ]  {method} {path}{qs} model={model} stream={stream}{channel_tag}")
        print(f"[{ts_end}] [RES]  {method} {path}{qs} -> {status} {tag} ({elapsed:.2f}s)")

    async def _send_error(self, send: Send, status: int, message: str) -> None:
        error_body = json.dumps({"error": {"message": message, "type": "auth_error"}}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({
            "type": "http.response.body",
            "body": error_body,
        })


app = FastAPI(title="LLM API 转换器", version="0.1.0", lifespan=lifespan)

# uvicorn --debug 会设置 sys.flags.debug
_debug_enabled = DEBUG or getattr(sys.flags, "debug", False)

# 日志级别（由 --log-level 参数控制，默认 info）
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()

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
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/static/index.html")


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="LLM API 转换器")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"],
                        help="日志级别 (默认: info)")
    args = parser.parse_args()

    os.environ["LOG_LEVEL"] = args.log_level
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "format": "[%(asctime)s] %(levelprefix)s %(client_addr)s - \"%(request_line)s\" %(status_code)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
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
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True, log_level=args.log_level, log_config=log_config)
