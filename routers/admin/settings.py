"""全局设置域路由：settings 读/写 + 字段描述投影（ADR-0018 D0）。"""

from fastapi import APIRouter, HTTPException

import request_logs

from .common import AdminAuthRoute

router = APIRouter(prefix="/admin", tags=["管理"], route_class=AdminAuthRoute)

# UI 元数据里会进入描述对象的透传键（其余元数据仅服务渲染裁量，不入描述契约）
_UI_META_PASSTHROUGH_KEYS = (
    "group",
    "label_key",
    "help_key",
    "help_class",
    "input_class",
    "label_class",
    "suffix_key",
    "blank_option_key",
    "choice_label_keys",
    "choice_help_keys",
    "unit",
    "hot",
    "trim",
)


def build_settings_schema() -> dict:
    """把 schema ∪ 约束 ∪ UI 元数据投影为每键描述对象（只读函数，加键即自动出现）。

    min/max 保持 wire 刻度原值（与服务端校验同源同值）；显示单位换算由前端按
    unit 字段单点完成。UI 元数据未登记的键兜底 hidden 分区，契约测试会拦漏配。
    """
    import config as _config

    ordered_keys = list(_config._CONFIG_UI_META) + [key for key in _config._CONFIG_SCHEMA if key not in _config._CONFIG_UI_META]
    descriptors: dict = {}
    for key in ordered_keys:
        schema = _config._CONFIG_SCHEMA[key]
        meta = _config._CONFIG_UI_META.get(key, {})
        constraints = _config._CONFIG_CONSTRAINTS.get(key, {})
        entry: dict = {
            "type": schema["type"],
            "default": schema["default"],
            "requires_restart": schema["requires_restart"],
            "section": meta.get("section", "hidden"),
        }
        if schema.get("readonly"):
            entry["readonly"] = True
        if "min" in constraints:
            entry["min"] = constraints["min"]
        if "max" in constraints:
            entry["max"] = constraints["max"]
        if "choices" in constraints:
            entry["choices"] = list(constraints["choices"])
        for meta_key in _UI_META_PASSTHROUGH_KEYS:
            if meta_key in meta:
                entry[meta_key] = meta[meta_key]
        descriptors[key] = entry
    return descriptors


@router.get("/settings/schema")
async def get_settings_schema_endpoint():
    """获取设置项字段描述（type/default/min/max/choices/unit/section/文案键等）"""
    return build_settings_schema()


@router.get("/settings")
async def get_settings_endpoint():
    """获取所有配置项（裸 wire 值；字节↔显示换算由前端绑定器按描述 unit 单点完成）"""
    import config as _config

    return _config.get_settings()


@router.put("/settings")
async def update_settings_endpoint(body: dict):
    """批量更新配置"""
    import config as _config

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是对象")
    unknown = [k for k in body if k not in _config._CONFIG_SCHEMA]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知配置项: {unknown}",
        )
    try:
        result = await _config.update_settings(body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reload_result = await request_logs.reload_backend()
    result["request_log_backend"] = reload_result
    if not reload_result.get("available"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "请求记录库配置已保存，但新 backend 初始化失败，已保留旧 backend",
                "settings": result,
                "request_log_backend": reload_result,
            },
        )
    return result
