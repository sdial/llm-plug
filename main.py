from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from client import close_all_clients
from config import HOST, PORT
from routers import admin, proxy_chat, proxy_response, proxy_anthropic, proxy_models


@asynccontextmanager
async def lifespan(app):
    yield
    await close_all_clients()


app = FastAPI(title="LLM API 转换器", version="0.1.0", lifespan=lifespan)

# 注册路由
app.include_router(admin.router)
app.include_router(proxy_chat.router)
app.include_router(proxy_response.router)
app.include_router(proxy_anthropic.router)
app.include_router(proxy_models.router)

# 静态文件（管理页面）
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
