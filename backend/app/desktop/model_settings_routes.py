"""
本文件对外提供 model_settings_router，作为桌面模型设置的 HTTP 接口层。

输入为桌面会话请求与 models.py 的请求体；输出为设置快照、保存结果、连通性测试结果与
条目引用清单。具体工作流为：读取当前生效配置与文件层，调用 model_settings 域的纯函数；
删除被引用的条目时先返回 409 与引用清单，人工确认后才写入。

写入口只接受本机会话（AuthMiddleware 的 loopback + X-Focus-Session 保护），且 MUST NOT
注册为 Agent 可调用的工具——人点保存本身就是那个「人工决定」。凭据值只写不读：任何响应
都不包含密钥值，只含来源变量名与是否已设置。

示例：PUT /desktop/api/settings/models
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.app.desktop.model_settings import (
    ModelSettingsError,
    probe_model_connection,
    referencing_models,
    save_model_settings,
    settings_snapshot,
)

model_settings_router = APIRouter(prefix="/desktop/api/settings")


def _service(request: Request):
    service = getattr(request.app.state, "desktop_service", None)
    if service is None:
        raise HTTPException(503, {"code": "desktop_unavailable"})
    return service


@model_settings_router.get("/models")
async def get_model_settings(request: Request) -> dict:
    """当前生效目录、删除名单、可选适配器与默认模型解析状态。"""
    return settings_snapshot(_service(request).app_config)


@model_settings_router.put("/models")
async def put_model_settings(body: dict, request: Request) -> dict:
    """校验并保存模型设置；被引用的条目在删除前需要显式确认。"""
    service = _service(request)
    removed = _removed_entry_names(service.app_config, body)
    if removed and not body.get("acknowledge_references"):
        references = await referencing_models(service.session_factory, removed)
        if references:
            raise HTTPException(
                409,
                {
                    "code": "model_referenced",
                    "references": references,
                    "message": "这些条目仍被任务或运行引用，确认删除后它们需要重新选择模型",
                },
            )
    try:
        return save_model_settings(service.app_config, body)
    except ModelSettingsError as exc:
        raise HTTPException(
            422, {"code": "model_settings_invalid", "errors": exc.errors}
        ) from exc


@model_settings_router.post("/models/references")
async def post_model_references(body: dict, request: Request) -> dict:
    """查询引用给定条目名的任务、运行与巡检。"""
    names = body.get("names")
    if not isinstance(names, list):
        raise HTTPException(422, {"code": "invalid_names", "message": "names 必须是列表"})
    return {"references": await referencing_models(_service(request).session_factory, names)}


@model_settings_router.post("/models/test")
async def post_model_test(body: dict, request: Request) -> dict:
    """用候选条目发起一次最小真实调用；只在人显式点击时执行。"""
    entry = body.get("entry")
    if not isinstance(entry, dict):
        raise HTTPException(422, {"code": "invalid_entry", "message": "entry 必须是映射"})
    api_key_value = body.get("api_key_value")
    _service(request)  # 统一在未就绪时返回 503
    try:
        return await probe_model_connection(
            entry, api_key_value if isinstance(api_key_value, str) else None
        )
    except ModelSettingsError as exc:
        raise HTTPException(422, {"code": "model_settings_invalid", "errors": exc.errors}) from exc


def _removed_entry_names(app_config, body: dict) -> list[str]:
    """本次提交会让哪些已生效条目消失——删除确认只对这些名字提问。"""
    submitted = body.get("models")
    if not isinstance(submitted, list):
        return []
    kept = {
        entry.get("name")
        for entry in submitted
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    return sorted(model.name for model in app_config.models if model.name not in kept)
