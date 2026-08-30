"""本文件对外提供 compression_router，作为 human-in-the-loop 压缩与关键字快捷压缩的 HTTP 接口层。

输入为带 X-Focus-Session 的桌面请求与 SummarizeRequest / 关键字预览与应用请求；输出为候选
摘要 JSON、压缩面板消息快照或关键字命中预览/应用结果。消息快照复用
DesktopService.get_checkpoint_messages（checkpoint_ns=""）。示例：
`app.include_router(compression_router)`。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.compression import SummarizeRequest, summarize_messages

compression_router = APIRouter()


class QuickPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    keyword: str
    case_sensitive: bool = False


class QuickApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    ranges: list[dict]
    scrub_terms: list[str] = Field(default_factory=list)


@compression_router.post("/desktop/api/compression/summarize")
async def summarize(body: SummarizeRequest, request: Request) -> dict:
    """为选定消息范围生成候选摘要；用户可编辑/重写/放弃，确认前不影响任何上下文。

    forbid_terms 非空时，摘要指令追加禁提条款（快捷压缩禁提关键词用）。
    """
    service = request.app.state.desktop_service
    try:
        summary = await summarize_messages(
            body.messages, body.model_name, service.app_config, forbid_terms=body.forbid_terms
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"摘要生成失败: {exc}") from exc
    return {"summary": summary}


@compression_router.post("/desktop/api/compression/quick-preview")
async def quick_preview(body: QuickPreviewRequest, request: Request) -> dict:
    """关键字快捷压缩预览：机械命中主图消息中含该词的消息，供用户框选/调粒度。"""
    service = request.app.state.desktop_service
    try:
        return await service.quick_compression_preview(
            body.task_id, body.keyword, body.case_sensitive
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"关键字预览失败: {exc}") from exc


@compression_router.post("/desktop/api/compression/quick-apply")
async def quick_apply(body: QuickApplyRequest, request: Request) -> dict:
    """关键字快捷压缩应用：先按 scrub_terms 机械剥离禁用词，再校验范围写回主图 checkpoint。"""
    service = request.app.state.desktop_service
    try:
        return await service.quick_compression_apply(
            body.task_id, body.ranges, scrub_terms=body.scrub_terms
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"快捷压缩应用失败: {exc}") from exc


@compression_router.get("/desktop/api/tasks/{task_id}/messages")
async def task_messages(task_id: str, request: Request) -> dict:
    """压缩面板消息快照：主图 checkpoint 的当前 messages（含压缩块元数据）。"""
    service = request.app.state.desktop_service
    async with service.session_factory() as session:
        task, _ = await service._get_task_entities(session, task_id)
    messages = await service.get_checkpoint_messages(task.thread_id, "")
    return {"messages": messages}
