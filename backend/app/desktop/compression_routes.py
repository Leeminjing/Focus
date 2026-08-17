"""本文件对外提供 compression_router，作为 human-in-the-loop 压缩流程的 HTTP 接口层。

输入为带 X-Focus-Session 的桌面请求与 SummarizeRequest；输出为候选摘要 JSON 或压缩
面板的消息快照。具体工作流为校验本机会话后调用桌面服务与 compression 模块，消息快照
复用 DesktopService.get_checkpoint_messages（checkpoint_ns=""）。示例：
`app.include_router(compression_router)`。
"""

from fastapi import APIRouter, HTTPException, Request

from backend.app.desktop.compression import SummarizeRequest, summarize_messages

compression_router = APIRouter()


@compression_router.post("/desktop/api/compression/summarize")
async def summarize(body: SummarizeRequest, request: Request) -> dict:
    """为选定消息范围生成候选摘要；用户可编辑/重写/放弃，确认前不影响任何上下文。"""
    service = request.app.state.desktop_service
    try:
        summary = await summarize_messages(
            body.messages, body.model_name, service.app_config
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"摘要生成失败: {exc}") from exc
    return {"summary": summary}


@compression_router.get("/desktop/api/tasks/{task_id}/messages")
async def task_messages(task_id: str, request: Request) -> dict:
    """压缩面板消息快照：主图 checkpoint 的当前 messages（含压缩块元数据）。"""
    service = request.app.state.desktop_service
    async with service.session_factory() as session:
        task, _ = await service._get_task_entities(session, task_id)
    messages = await service.get_checkpoint_messages(task.thread_id, "")
    return {"messages": messages}
