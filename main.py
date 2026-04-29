import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from client import close_all_clients
from config import DEBUG, HOST, PORT
from routers import admin, proxy_chat, proxy_response, proxy_anthropic, proxy_models
from storage import load_api_keys


@asynccontextmanager
async def lifespan(app):
    yield
    await close_all_clients()


app = FastAPI(title="LLM API 转换器", version="0.1.0", lifespan=lifespan)

# uvicorn --debug 会设置 sys.flags.debug
_debug_enabled = DEBUG or getattr(sys.flags, "debug", False)

# 日志级别（由 --log-level 参数控制，默认 info）
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    method = request.method
    path = request.url.path
    query = request.url.query

    # 只记录代理 API 请求
    if method == "POST" and path in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
        start = time.time()
        model = ""
        stream = False
        try:
            body = await request.json()
            model = body.get("model", "")
            stream = body.get("stream", False)
        except Exception:
            pass
        qs = f"?{query}" if query else ""
        ts_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        api_key_id = getattr(request.state, 'api_key_id', None)
        key_tag = f" key={api_key_id}" if api_key_id else ""

        response = await call_next(request)
        elapsed = time.time() - start
        status = response.status_code
        tag = "OK" if status < 400 else "ERR"
        ts_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        channel = getattr(request.state, 'selected_channel_name', '')
        channel_tag = f" channel={channel}" if channel else ""
        print(f"[{ts_start}] [REQ]  {method} {path}{qs} model={model} stream={stream}{channel_tag}{key_tag}")
        print(f"[{ts_end}] [RES]  {method} {path}{qs} -> {status} {tag} ({elapsed:.2f}s)")
        return response

    return await call_next(request)


_PROXY_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")


@app.middleware("http")
async def proxy_auth_middleware(request: Request, call_next):
    """Authenticate proxy requests via Bearer token and enforce model allow-lists."""
    if request.method == "POST" and request.url.path in _PROXY_PATHS:
        keys_data = load_api_keys()
        api_keys = keys_data.get("api_keys", [])

        # If no API keys configured, allow all requests (backward compatible)
        if not api_keys:
            return await call_next(request)

        # Extract Bearer token
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Missing or invalid Authorization header", "type": "auth_error"}},
            )
        token = auth_header[len("Bearer "):]

        # Look up key
        matched_key = None
        for key in api_keys:
            if key.get("key") == token:
                matched_key = key
                break

        if matched_key is None:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )

        # Store key ID for downstream use (stats recording)
        request.state.api_key_id = matched_key.get("id")

        # Check model allow-list
        allowed_models = matched_key.get("allowed_models", [])
        if allowed_models:
            try:
                body_bytes = await request.body()
                body = json.loads(body_bytes)
                request_model = body.get("model", "")
            except Exception:
                request_model = ""
            if request_model and request_model not in allowed_models:
                return JSONResponse(
                    status_code=403,
                    content={"error": {
                        "message": f"Model '{request_model}' is not allowed for this API key",
                        "type": "auth_error",
                    }},
                )
            # Re-inject body so downstream handlers can read it again
            async def receive():
                return {"type": "http.request", "body": body_bytes}
            request._receive = receive

    return await call_next(request)

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
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True, log_level=args.log_level)
