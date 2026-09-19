"""模型组管理域路由：CRUD、启停、负载均衡配置兼容接口。"""

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from channel_catalog import CatalogConflictError, catalog
from models.model_group import LBConfig, ModelGroup, ModelGroupCreate, ModelGroupUpdate
from storage import get_lb_config, save_lb_config

from .common import AdminAuthRoute, _get_channels

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)


async def _validate_model_group_items(body) -> None:
    """保存前强校验条目：模型名非空；硬绑定渠道须存在、已启用且包含该模型。"""
    channels = await _get_channels()
    channel_map = {ch.id: ch for ch in channels}
    for idx, entry in enumerate(body.items or []):
        if not entry.model:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条：模型名不能为空")
        if not entry.channel_id:
            continue
        ch = channel_map.get(entry.channel_id)
        if ch is None:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条：绑定渠道不存在")
        if not ch.enabled:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 条：绑定渠道「{ch.name}」已禁用")
        if entry.model not in (ch.models or []):
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx + 1} 条：渠道「{ch.name}」不包含模型 {entry.model}",
            )


@router.get("/model-groups")
async def list_model_groups():
    """获取所有模型组"""
    return list((await catalog.snapshot()).model_groups)


@router.post("/model-groups", response_model=ModelGroup)
async def create_model_group(body: ModelGroupCreate):
    """创建模型组"""
    await _validate_model_group_items(body)
    group = ModelGroup(**body.model_dump())
    try:
        return await catalog.add_model_group(group)
    except CatalogConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/model-groups/{group_id}", response_model=ModelGroup)
async def update_model_group_endpoint(group_id: str, body: ModelGroupUpdate):
    """更新模型组"""
    update_data = body.model_dump(exclude_unset=True)
    if "items" in update_data:
        await _validate_model_group_items(body)
    try:
        updated = await catalog.update_model_group(group_id, update_data)
    except CatalogConflictError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"字段校验失败: {exc}") from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="模型组不存在")
    return updated


@router.delete("/model-groups/{group_id}")
async def delete_model_group_endpoint(group_id: str):
    """删除模型组"""
    deleted = await catalog.delete_model_group(group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="模型组不存在")
    return {"message": "删除成功"}


@router.patch("/model-groups/{group_id}/toggle", response_model=ModelGroup)
async def toggle_model_group(group_id: str):
    """启用/禁用模型组"""
    updated = await catalog.toggle_model_group(group_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="模型组不存在")
    return updated


@router.get("/lb-config", response_model=LBConfig)
async def get_lb_config_endpoint():
    """获取负载均衡全局配置"""
    return await get_lb_config()


@router.put("/lb-config", response_model=LBConfig)
async def update_lb_config_endpoint(body: LBConfig):
    """更新负载均衡全局配置"""
    await save_lb_config(body)
    return body
