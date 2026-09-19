"""存储管理域路由：空间统计、按月清理与预览。"""

import os
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

import config
import storage_stats

from .common import AdminAuthRoute

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


class StorageCleanupRequest(BaseModel):
    action: Literal["delete_month"]
    target: str = Field(..., pattern=r"^\d{6}$")


@router.get("/storage/stats")
async def get_storage_stats():
    """获取存储空间统计（logs、request_raw_logs、其他数据）"""
    return await storage_stats.get_storage_stats()


@router.post("/storage/cleanup")
async def cleanup_storage(body: StorageCleanupRequest):
    """删除指定月份的 request_raw_logs 数据库（target=YYYYMM）。"""
    raw_logs_dir = os.path.join(config.DATA_DIR, "request_raw_logs")
    return await storage_stats.cleanup_month(raw_logs_dir, body.target)


@router.post("/storage/cleanup/preview")
async def preview_cleanup(body: StorageCleanupRequest):
    """预览清理效果（不实际删除文件）"""
    raw_logs_dir = os.path.join(config.DATA_DIR, "request_raw_logs")
    return await storage_stats.preview_cleanup(raw_logs_dir, body.target)
