import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

import quota_limits
import request_logs
from channel_catalog import catalog
from client import cleanup_stale_clients, close_all_clients
from config import HOST, PORT, get_setting, init_settings
from logging_config import configure_level_file_logging
from middleware.admin_auth_middleware import AdminAuthMiddleware
from middleware.body_buffer_middleware import BodyBufferMiddleware
from middleware.proxy_auth_middleware import ProxyAuthMiddleware
from middleware.request_log_middleware import RequestLogMiddleware
from middleware.whitelist_middleware import WhitelistMiddleware
from response_state import get_responses_store, reload_responses_store
from routers import admin, proxy_anthropic, proxy_chat, proxy_models, proxy_response
from stats import close_pool as close_stats_pool
from stats import init_db as init_stats_db
from stats import start_stats_workers, stop_stats_workers
from storage import load_api_keys
from upstream_catalog import catalog as upstream_catalog

# 应用版本号 — 发布新版本时改这一行即可，无需动 static/index.html
APP_VERSION = "v1.6.18"
APP_RELEASE_DATE = "2026-09-19"
# 静态资源版本号 — 每次更新 JS/CSS 后修改此值即可强制浏览器刷新缓存
STATIC_ASSET_VERSION = "51"

# 配置日志级别文件输出
_log_dir = Path(__file__).parent / "logs"
configure_level_file_logging(_log_dir)


_responses_store = get_responses_store()


async def _session_cleanup_loop():
    """定期清理过期会话文件"""
    while True:
        interval = get_setting("response_state_cleanup_interval_minutes") or 30
        await asyncio.sleep(interval * 60)
        try:
            await _responses_store._cleanup_if_needed()
        except Exception as e:
            logger.warning(f"Session cleanup failed: {e}")


async def _request_log_cleanup_loop():
    """清理过期请求日志记录"""

    async def _try_cleanup():
        try:
            await request_logs.cleanup_old_records()
        except Exception as e:
            logger.warning(f"request log cleanup error: {e}")

    await asyncio.sleep(10)
    await _try_cleanup()
    while True:
        await asyncio.sleep(86400)
        await _try_cleanup()


async def _group_probe_loop():
    """组内主动探活后台循环（ADR-0010 D10）：主恢复 ≤ 探活间隔自动回切。"""
    from proxy.group_probe import run_group_probe_loop

    await run_group_probe_loop()


async def _upstream_catalog_refresh_loop():
    """固定公共来源只生成 Catalog Candidate，不阻塞启动或自动发布。"""
    from upstream_catalog_refresh import run_catalog_refresh_loop

    await run_catalog_refresh_loop()


@asynccontextmanager
async def lifespan(app):
    await init_settings()
    await upstream_catalog.ensure_builtin()
    reload_responses_store()
    quota_limits.load()
    from proxy import outcomes as _outcomes

    _outcomes.load_quota_limits()
    quota_limits.setup()
    catalog_snapshot = await catalog.snapshot()
    quota_limits.cleanup({channel.id for channel in catalog_snapshot.channels})
    keys_data = await load_api_keys()
    channel_count = len(catalog_snapshot.channels)
    model_count = len({model for channel in catalog_snapshot.channels for model in channel.models})
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
    probe_task = asyncio.create_task(_group_probe_loop())
    catalog_refresh_task = asyncio.create_task(_upstream_catalog_refresh_loop())
    try:
        yield
    except asyncio.CancelledError:
        pass
    finally:
        cleanup_task.cancel()
        session_cleanup_task.cancel()
        request_log_cleanup_task.cancel()
        probe_task.cancel()
        catalog_refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        with suppress(asyncio.CancelledError):
            await session_cleanup_task
        with suppress(asyncio.CancelledError):
            await request_log_cleanup_task
        with suppress(asyncio.CancelledError):
            await probe_task
        with suppress(asyncio.CancelledError):
            await catalog_refresh_task
        await stop_stats_workers()
        await close_stats_pool()
        await request_logs.close_backend()
        await close_all_clients()


# 纯 ASGI 中间件链（add_middleware 后加在外层，逆序添加得到目标执行序）
# 执行序（外→内）：Whitelist → AdminAuth → ProxyAuth → BodyBuffer → RequestLog → app
app = FastAPI(title="LLM API 转换器", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(RequestLogMiddleware)
app.add_middleware(BodyBufferMiddleware)
app.add_middleware(ProxyAuthMiddleware)
app.add_middleware(AdminAuthMiddleware)
app.add_middleware(WhitelistMiddleware)

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
    """返回 HTML 文件，同时替换静态资源版本占位符与应用版本占位符。"""
    content = file_path.read_text(encoding="utf-8")
    content = content.replace("__STATIC_ASSET_VERSION__", STATIC_ASSET_VERSION)
    content = content.replace("__APP_VERSION__", APP_VERSION)
    content = content.replace("__APP_RELEASE_DATE__", APP_RELEASE_DATE)
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


@app.get("/admin/request-analyzer")
@app.get("/admin/request-analyzer/")
async def admin_request_analyzer():
    return _html_response(STATIC_DIR / "request-analyzer.html")


if __name__ == "__main__":
    import argparse
    import signal
    import socket

    import uvicorn

    parser = argparse.ArgumentParser(description="LLM API 转换器")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="日志级别 (默认: info)",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="禁用热重载（避免 Windows 下进程退出后端口未释放的问题）",
    )
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
                "fmt": '[%(asctime)s] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
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
            "uvicorn.error": {
                "handlers": ["default"],
                "level": args.log_level.upper(),
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": args.log_level.upper(),
                "propagate": False,
            },
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
        uvicorn.run(
            "main:app",
            host=HOST,
            port=PORT,
            reload=True,
            log_level=args.log_level,
            log_config=log_config,
            http="httptools",
            loop="auto",
        )
