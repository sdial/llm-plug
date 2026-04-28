import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from client import close_all_clients
from config import DEBUG, HOST, PORT
from routers import admin, proxy_chat, proxy_response, proxy_anthropic, proxy_models


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
        print(f"[REQ]  {method} {path}{qs} model={model} stream={stream}")

        response = await call_next(request)
        elapsed = time.time() - start
        status = response.status_code
        tag = "OK" if status < 400 else "ERR"
        print(f"[RES]  {method} {path}{qs} -> {status} {tag} ({elapsed:.1f}s)")
        return response

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
