import sys
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

if _debug_enabled:
    @app.middleware("http")
    async def debug_log_middleware(request: Request, call_next):
        method = request.method
        path = request.url.path
        query = request.url.query
        model = ""
        stream = False
        if method == "POST" and path in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
            try:
                body = await request.json()
                model = body.get("model", "")
                stream = body.get("stream", False)
            except Exception:
                pass
        print(f"[DEBUG] {method} {path}{'?' + query if query else ''} model={model} stream={stream}")
        response = await call_next(request)
        print(f"[DEBUG] {method} {path} -> {response.status_code}")
        return response

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
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
