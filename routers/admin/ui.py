"""管理页 htmx 片段域路由。"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from .common import ADMIN_FRAGMENT_DIR, AdminAuthRoute

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


@router.get("/ui/{section}")
async def admin_ui_fragment(section: str):
    """返回管理页局部片段，供 htmx 局部刷新使用"""
    fragment_map = {
        "channels": "channels.html",
        "apikeys": "apikeys.html",
        "stats": "stats.html",
        "requests": "requests.html",
        "settings": "settings.html",
        "whitelist": "whitelist.html",
        "lb": "model-groups.html",
        "storage": "storage.html",
        "context-shaping": "context-shaping.html",
    }
    filename = fragment_map.get(section)
    if not filename:
        raise HTTPException(status_code=404, detail="片段不存在")
    fragment_path = ADMIN_FRAGMENT_DIR / filename
    if not fragment_path.exists():
        raise HTTPException(status_code=404, detail="片段文件不存在")
    return HTMLResponse(fragment_path.read_text(encoding="utf-8"))
