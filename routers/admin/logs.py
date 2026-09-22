"""请求记录与日志文件域路由：requests 查询/RAW 字段/手动清理、logs 查看。"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

import request_logs
from stats import list_requests as stats_list_requests  # noqa: F401

from .common import (
    LOGS_DIR,
    AdminAuthRoute,
    _decorate_request_items,
    _parsed_request_sources,
    _validate_log_filename,
)

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)

request_log_list_requests = request_logs.list_requests
request_log_get_request_field = request_logs.get_request_field
request_log_get_raw_info = request_logs.get_raw_info
request_log_get_request_field_by_reference = request_logs.get_request_field_by_reference

_FIELD_PATH_MAP = {
    "request-headers": "request_headers",
    "request-body": "request_body",
    "response-headers": "response_headers",
    "response-body": "response_body",
}


@router.get("/requests")
async def list_requests_endpoint(
    source: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    success: Annotated[bool | None, Query()] = None,
    api_key_id: Annotated[str | None, Query()] = None,
    client_ip: Annotated[str | None, Query()] = None,
    is_stream: Annotated[bool | None, Query()] = None,
    request_source: Annotated[str | tuple[str, ...] | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
):
    """查询请求记录（支持分页和过滤）

    request_source 未传＝不过滤（与旧版行为一致）；重复键与逗号串两种多选形式均可，
    非法取值返回 400 并列出合法值（不静默过滤成空结果误导排障者）。
    """
    # 调用时从 routers.admin 命名空间重读，测试会整体替换该名称。
    from . import stats_list_requests  # noqa: F811

    parsed_sources = _parsed_request_sources(request_source)

    if source not in (None, "stats"):
        raise HTTPException(status_code=400, detail=f"不支持的请求记录来源: {source}")
    result = await stats_list_requests(
        model=model,
        channel=channel,
        start=start,
        end=end,
        success=success,
        api_key_id=api_key_id,
        client_ip=client_ip,
        is_stream=is_stream,
        page=page,
        page_size=page_size,
        request_source=parsed_sources,
    )
    result["source"] = "stats"
    return await _decorate_request_items(result)


@router.get("/requests/{request_ref}/raw-info")
async def get_request_raw_info_endpoint(
    request_ref: str,
    timestamp: Annotated[str, Query()],
):
    """按显式 Request Reference 查询 RAW 状态；不允许按元数据或本地 id 猜测。"""
    if len(request_ref) != 32 or any(char not in "0123456789abcdef" for char in request_ref):
        raise HTTPException(status_code=400, detail="无效的请求引用")
    return await request_log_get_raw_info(request_ref, timestamp)


@router.get("/requests/{request_ref}/raw/{field_name}")
async def get_request_raw_field_endpoint(
    request_ref: str,
    field_name: str,
    timestamp: Annotated[str, Query()],
):
    field = _FIELD_PATH_MAP.get(field_name)
    if field is None:
        raise HTTPException(status_code=400, detail="不支持的字段")
    result = await request_log_get_request_field_by_reference(request_ref, timestamp, field)
    if result is None:
        raise HTTPException(status_code=410, detail="RAW 信息不可用或已过期")
    return result


@router.get("/requests/{request_id}/{field_name}")
async def get_request_field_endpoint(request_id: str, field_name: str):
    """获取单个请求的单个 JSONB 字段（请求/返回的 Header 或 Body）"""
    field = _FIELD_PATH_MAP.get(field_name)
    if field is None:
        raise HTTPException(status_code=400, detail=f"不支持的字段: {field_name}")
    result = await request_log_get_request_field(request_id, field)
    if result is None:
        raise HTTPException(status_code=404, detail="请求记录不存在")
    return result


@router.post("/request-logs/cleanup")
async def cleanup_request_logs_endpoint():
    """手动触发请求记录 TTL 清理（按 settings 中的保留天数执行）"""
    return await request_logs.cleanup_old_records()


@router.get("/logs")
async def list_logs():
    """列出所有日志文件"""
    if not LOGS_DIR.exists():
        return []
    files = sorted(LOGS_DIR.glob("*.jsonl"), reverse=True)
    result = []
    for file in files:
        try:
            result.append({"name": file.name, "size": file.stat().st_size})
        except FileNotFoundError:
            # 轮转/清理可与列表读取并发；已消失的文件无需使整个页面失败。
            continue
    return result


@router.get("/logs/{filename}")
async def get_log(filename: str):
    """获取日志文件内容"""
    _validate_log_filename(filename)
    file_path = (LOGS_DIR / filename).resolve()
    if not file_path.is_relative_to(LOGS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="禁止访问")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, media_type="application/jsonl")
