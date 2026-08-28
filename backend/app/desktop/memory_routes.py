"""本文件对外提供 memory_router，作为全局记忆库的 HTTP 接口层。

输入为带 X-Focus-Session 的桌面请求（由统一 AuthMiddleware 保护）与记忆库请求模型；
输出为记忆库列表/单条 payload 或压缩总结出的记忆草稿。具体工作流为校验本机会话后调用
DesktopService.memory 的 CRUD 与 resolve_selection/summarize。示例：
`app.include_router(memory_router)`。
"""

from fastapi import APIRouter, HTTPException, Request

from backend.app.desktop.memory import (
    MemoryCreate,
    MemorySummarizeRequest,
    MemoryUpdate,
)

memory_router = APIRouter()


@memory_router.get("/desktop/api/memory")
async def list_memories(request: Request) -> dict:
    return {"memories": await request.app.state.desktop_service.memory.list()}


@memory_router.get("/desktop/api/memory/{memory_id}")
async def get_memory(memory_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.memory.get(memory_id)


@memory_router.post("/desktop/api/memory")
async def create_memory(body: MemoryCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.memory.create(body)


@memory_router.put("/desktop/api/memory/{memory_id}")
async def update_memory(memory_id: str, body: MemoryUpdate, request: Request) -> dict:
    return await request.app.state.desktop_service.memory.update(memory_id, body)


@memory_router.delete("/desktop/api/memory/{memory_id}")
async def delete_memory(memory_id: str, request: Request) -> dict:
    await request.app.state.desktop_service.memory.delete(memory_id)
    return {"ok": True}


@memory_router.post("/desktop/api/memory/summarize")
async def summarize_memory(body: MemorySummarizeRequest, request: Request) -> dict:
    """为选定的记忆来源生成中文候选摘要；用户可编辑/重写/放弃，确认前不影响记忆库。

    返回结构化草稿：complete 模式为 {mode, content}；segmented 模式为 {mode, segments, content}。
    """
    service = request.app.state.desktop_service
    try:
        return await service.memory.summarize(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"摘要生成失败: {exc}") from exc
