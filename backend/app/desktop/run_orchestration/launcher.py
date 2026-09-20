r"""本文件对外提供 RunLauncher、RunRegistrar 与严格 RunRegistrationRequest。

输入为 PreparedRun、FastAPI 资源、可信执行身份和幂等键；输出为现有 `start_run → run_agent`
脊柱上的 RunRecord 或唯一持久 Run。具体工作流为 Registrar 依靠数据库活跃执行/idempotency 约束
裁决并发，Launcher 只调用既有 start_run 并挂接统一 lifecycle，绝不复制 Agent 执行循环。
示例：`record = await launcher.launch(prepared, request)`。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from fastapi import Request
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.models import DesktopRun
from focus.runtime.runs.manager import RunRecord


class PreparedRunLike(Protocol):
    body: Any
    thread_id: str
    agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None
    payload: dict[str, Any]


class RunStarter(Protocol):
    async def __call__(
        self,
        body: Any,
        thread_id: str,
        request: Request,
        agent_factory: Callable[[], Awaitable[CompiledStateGraph]] | None = None,
    ) -> RunRecord: ...


class RunRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    execution_thread_id: str = Field(min_length=1)
    checkpoint_ns: str = ""
    context_revision_id: str | None = None
    context_checkpoint_id: str | None = None
    origin_message_id: str | None = None
    directive_id: str | None = None
    user_intent_id: str | None = None
    loop_id: str | None = None
    round_id: str | None = None
    action_id: str | None = None
    equipment: dict[str, Any] = Field(default_factory=dict)
    workspace_anchor: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    input_messages: tuple[dict[str, Any], ...] = ()
    model_name: str | None = None


class RunRegistrationConflict(RuntimeError):
    pass


class RunRegistrar:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def register(self, request: RunRegistrationRequest) -> DesktopRun:
        async with self._sessions() as session:
            if request.idempotency_key is not None:
                existing = await self._by_key(session, request.idempotency_key)
                if existing is not None:
                    return existing
            run = DesktopRun(
                run_id=request.run_id,
                task_id=request.task_id,
                agent_id=request.agent_id,
                kind=request.kind,
                status="pending",
                origin=request.origin,
                execution_thread_id=request.execution_thread_id,
                checkpoint_ns=request.checkpoint_ns,
                context_revision_id=request.context_revision_id,
                context_checkpoint_id=request.context_checkpoint_id,
                origin_message_id=request.origin_message_id,
                directive_id=request.directive_id,
                user_intent_id=request.user_intent_id,
                loop_id=request.loop_id,
                round_id=request.round_id,
                action_id=request.action_id,
                equipment=request.equipment,
                workspace_anchor=request.workspace_anchor,
                idempotency_key=request.idempotency_key,
                input_messages=list(request.input_messages),
                model_name=request.model_name,
            )
            session.add(run)
            try:
                await session.commit()
                return run
            except IntegrityError as exc:
                await session.rollback()
                if request.idempotency_key is not None:
                    winner = await self._by_key(session, request.idempotency_key)
                    if winner is not None:
                        return winner
                active = await session.scalar(
                    select(DesktopRun).where(
                        DesktopRun.task_id == request.task_id,
                        DesktopRun.execution_thread_id == request.execution_thread_id,
                        DesktopRun.checkpoint_ns == request.checkpoint_ns,
                        DesktopRun.kind == "main",
                        DesktopRun.status.in_(["pending", "running"]),
                    )
                )
                raise RunRegistrationConflict(
                    f"执行身份已有活跃 main Run: {active.run_id if active else request.task_id}"
                ) from exc

    @staticmethod
    async def _by_key(session: AsyncSession, key: str) -> DesktopRun | None:
        return await session.scalar(
            select(DesktopRun).where(DesktopRun.idempotency_key == key)
        )


class RunLauncher:
    def __init__(self, starter: RunStarter) -> None:
        self._starter = starter

    async def launch(
        self,
        prepared: PreparedRunLike | None,
        request: Request,
    ) -> RunRecord | None:
        if prepared is None or prepared.agent_factory is None:
            return None
        record = await self._starter(
            prepared.body,
            prepared.thread_id,
            request,
            agent_factory=prepared.agent_factory,
        )
        request.app.state.desktop_service.attach_run_sync(record)
        return record
