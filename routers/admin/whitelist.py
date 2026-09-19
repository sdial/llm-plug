"""IP 白名单域路由：读/写、保存前预览。"""

import contextlib
import os
import tempfile

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import whitelist as _whitelist_mod

from .common import AdminAuthRoute

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


@router.get("/whitelist")
async def get_whitelist(request: Request):
    """获取白名单 CSV 原始文本及有效规则数"""
    from . import WHITELIST_PATH

    client_ip = request.client.host if request.client else ""
    if not WHITELIST_PATH.exists():
        return {"content": "", "rule_count": 0, "client_ip": client_ip}
    content = WHITELIST_PATH.read_text(encoding="utf-8")
    rules = _whitelist_mod.load_rules(str(WHITELIST_PATH))
    return {"content": content, "rule_count": len(rules), "client_ip": client_ip}


class WhitelistPreviewRequest(BaseModel):
    content: str


@router.post("/whitelist/preview")
async def preview_whitelist(body: WhitelistPreviewRequest, request: Request):
    """校验白名单文本并返回保存后当前客户端是否会被锁定（不落盘）。

    复用 check_request 的权威匹配逻辑，避免前端自行解析 CIDR 产生偏差。
    """

    content = body.content
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content 必须是字符串")
    valid, error, rules = _whitelist_mod.validate_rules_text(content)
    if not valid:
        return {"valid": False, "error": error, "rule_count": 0, "admin_lockout": False}
    client_ip = request.client.host if request.client else ""
    admin_lockout = False
    # 管理界面访问以 GET /admin/ 为最小门槛：若新规则下当前 IP 连读取都失败，
    # 用户保存后将无法再进入管理界面，视为锁定。
    if client_ip and rules:
        _allow, _ = _whitelist_mod.check_request(rules, "/admin/", "GET", client_ip)
        admin_lockout = not _allow
    return {"valid": True, "error": "", "rule_count": len(rules), "admin_lockout": admin_lockout}


@router.put("/whitelist")
async def update_whitelist(body: dict):
    """校验并保存白名单 CSV，热重载自动生效"""
    from . import WHITELIST_PATH

    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content 必须是字符串")
    valid, error, rules = _whitelist_mod.validate_rules_text(content)
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    dir_name = str(WHITELIST_PATH.parent)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=dir_name,
        delete=False,
        prefix=".whitelist_",
        suffix=".tmp",
    ) as f:
        tmp_path = f.name
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp_path, str(WHITELIST_PATH))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return {"message": f"已保存 {len(rules)} 条规则", "rule_count": len(rules)}
