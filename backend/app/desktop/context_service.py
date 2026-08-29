"""
本文件对外提供 ContextService，负责桌面 Context 的快照、派生、lineage、执行投影审批
与独立 checkpoint 初始化。

输入为桌面 session factory、LangGraph checkpointer、AppConfig 以及 Context 请求模型；
输出为可直接返回给桌面 API 的 Context 数据。具体工作流为校验同工作区来源和已提交
checkpoint，保存用户 authored messages，调用 context_projection 编译执行投影，在无需
降级或用户批准后以新 thread_id 调用 aupdate_state 创建独立 checkpoint。示例：
`context = await service.derive(body)`。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_projection import ContextProjection, compile_context_messages
from backend.app.desktop.models import (
    AgentBoardTask,
    AgentMessage,
    ContextDefinitionUpdate,
    ContextDeriveCreate,
    ContextProjectionDecision,
    DesktopContextDefinition,
    DesktopContextSource,
    DesktopMaterial,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    PatrolAgent,
    PatrolDraft,
    SwarmAgent,
)
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from focus.agents.lead import make_lead_agent
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import deserialize_messages, serialize_message, validate_messages


logger = logging.getLogger(__name__)
_RUNNABLE_STATUSES = frozenset({"valid", "repaired", "approved"})


def _new_id() -> str:
    return uuid.uuid4().hex


class ContextService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        app_config: AppConfig,
    ) -> None:
        self.session_factory = session_factory
        self.checkpointer = checkpointer
        self.app_config = app_config

    async def snapshot(self, context_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            definition = await session.get(DesktopContextDefinition, context_id)
        if definition and definition.initial_checkpoint_id is None and checkpoint_id is None:
            return {
                "context_id": context_id,
                "checkpoint_id": None,
                "messages": deepcopy(definition.authored_messages),
            }
        config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
        if checkpoint_id:
            config["configurable"]["checkpoint_id"] = checkpoint_id
        checkpoint = await self.checkpointer.aget_tuple(config)
        if checkpoint is None:
            if checkpoint_id:
                raise HTTPException(404, "Context checkpoint 不存在")
            messages = deepcopy(definition.authored_messages) if definition else []
            return {"context_id": context_id, "checkpoint_id": None, "messages": messages}
        actual_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if checkpoint_id and actual_id != checkpoint_id:
            raise HTTPException(404, "Context checkpoint 不属于该 Context")
        values = checkpoint.checkpoint.get("channel_values", {})
        runtime_messages = [serialize_message(message) for message in values.get("messages", [])]
        messages = (
            self._display_messages(definition, runtime_messages)
            if definition and (not checkpoint_id or checkpoint_id == definition.initial_checkpoint_id)
            else runtime_messages
        )
        return {"context_id": context_id, "checkpoint_id": actual_id, "messages": messages}

    async def derive(self, body: ContextDeriveCreate) -> dict[str, Any]:
        refs = [(source.context_id, source.checkpoint_id) for source in body.sources]
        if len(refs) != len(set(refs)):
            raise HTTPException(422, "Context 来源不能重复")
        async with self.session_factory() as session:
            parents = [await session.get(DesktopThread, context_id) for context_id, _ in refs]
            if any(parent is None for parent in parents):
                raise HTTPException(404, "来源 Context 不存在")
            workspace_ids = {parent.workspace_id for parent in parents if parent is not None}
            if len(workspace_ids) != 1:
                raise HTTPException(422, "只能合并同一工作区的 Context")
        for parent, (_, checkpoint_id) in zip(parents, refs):
            await self._require_checkpoint(parent.thread_id, checkpoint_id)

        projection = compile_context_messages(body.messages)
        context_id = _new_id()
        thread_id = _new_id()
        async with self.session_factory() as session:
            task = DesktopThread(
                task_id=context_id,
                workspace_id=next(iter(workspace_ids)),
                thread_id=thread_id,
                title=body.title,
                ui_state={},
            )
            session.add(task)
            await session.flush()
            definition = DesktopContextDefinition(
                context_id=context_id,
                authored_messages=projection.authored_messages,
                execution_messages=projection.execution_messages,
                repair_manifest=projection.repair_manifest,
                issues=projection.issues,
                definition_hash=projection.definition_hash,
                projection_hash=projection.projection_hash,
                projection_status=projection.status,
                initial_message_ids=[],
            )
            sources = [
                DesktopContextSource(
                    source_id=_new_id(),
                    context_id=context_id,
                    parent_context_id=source.context_id,
                    source_checkpoint_id=source.checkpoint_id,
                    position=index,
                )
                for index, source in enumerate(body.sources)
            ]
            session.add_all([definition, *sources])
            await session.commit()
        if projection.status != "approval_required":
            await self._initialize(context_id, projection.status)
        return await self.get(context_id)

    async def update_definition(
        self, context_id: str, body: ContextDefinitionUpdate
    ) -> dict[str, Any]:
        projection = compile_context_messages(body.messages)
        async with self.session_factory() as session:
            definition = await session.scalar(
                select(DesktopContextDefinition)
                .where(DesktopContextDefinition.context_id == context_id)
                .with_for_update()
            )
            if not definition:
                raise HTTPException(404, "派生 Context 不存在")
            has_main_run = await session.scalar(
                select(DesktopRun.run_id)
                .where(DesktopRun.task_id == context_id, DesktopRun.kind == "main")
                .limit(1)
            )
            if has_main_run:
                raise HTTPException(409, "Context 已开始运行；请从当前快照继续派生")
            self._apply_projection(definition, projection)
            if projection.status != "approval_required":
                definition.projection_status = "initializing"
            definition.decision = None
            definition.decided_definition_hash = None
            definition.decided_projection_hash = None
            definition.decided_at = None
            await session.commit()
        if projection.status != "approval_required":
            await self._initialize(context_id, projection.status)
        return await self.get(context_id)

    async def decide(
        self, context_id: str, body: ContextProjectionDecision
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            definition = await session.get(DesktopContextDefinition, context_id)
            if not definition:
                raise HTTPException(404, "派生 Context 不存在")
            if (
                body.definition_hash != definition.definition_hash
                or body.projection_hash != definition.projection_hash
            ):
                raise HTTPException(409, "Context 定义或拟议投影已经变化，请重新确认")
            if definition.initial_checkpoint_id:
                raise HTTPException(409, "Context 执行 checkpoint 已初始化")
            definition.decision = body.decision
            definition.decided_definition_hash = body.definition_hash
            definition.decided_projection_hash = body.projection_hash
            definition.decided_at = datetime.now(timezone.utc)
            if body.decision == "reject":
                definition.projection_status = "rejected"
            else:
                try:
                    validate_messages(definition.execution_messages)
                except ValueError as exc:
                    raise HTTPException(422, f"拟议投影仍不合法，不能批准：{exc}") from exc
                definition.projection_status = "initializing"
            await session.commit()
        if body.decision == "accept":
            await self._initialize(context_id, "approved")
        return await self.get(context_id)

    async def archive(self, context_id: str, cascade: bool = False) -> dict[str, Any]:
        """归档一个会话（根/派生 Context）。归档可恢复；有后代时侧栏保留「已归档」墓碑。

        输入:
            context_id: str — 会话标识
            cascade: bool — True 时递归归档该会话及全部派生后代（无墓碑）

        输出:
            dict — {context_id, archived: True}

        工作流:
            (1) 会话不存在 404；已删除 409；存在 pending/running 主运行 409
            (2) cascade 时对自身与全部后代置 archived_at；否则仅置自身 archived_at
        """
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            if task.deleted_at is not None:
                raise HTTPException(409, "会话已删除，不能归档")
            if await self._has_active_run(session, context_id):
                raise HTTPException(409, "会话正在运行，不能归档")
            now = datetime.now(timezone.utc)
            if cascade:
                for cid in [context_id, *await self._descendant_ids(session, context_id)]:
                    row = await session.get(DesktopThread, cid)
                    if row is not None and row.deleted_at is None:
                        row.archived_at = now
            else:
                task.archived_at = now
            await session.commit()
        return {"context_id": context_id, "archived": True}

    async def unarchive(self, context_id: str) -> dict[str, Any]:
        """恢复一个已归档会话（清空 archived_at，回到 active）。"""
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            if task.deleted_at is not None:
                raise HTTPException(409, "会话已删除，不能恢复")
            task.archived_at = None
            await session.commit()
        return {"context_id": context_id, "archived": False}

    async def delete(self, context_id: str, cascade: bool = False) -> dict[str, Any]:
        """删除一个会话（根/派生 Context）。永久删除、需确认；有后代时侧栏保留「已删除」墓碑。

        输入:
            context_id: str — 会话标识
            cascade: bool — True 时递归删除该会话及全部派生后代（无墓碑）

        输出:
            dict — {context_id, deleted: True}

        工作流:
            (1) 会话不存在 404；存在 pending/running 主运行 409
            (2) cascade：自底向上物理删除自身与全部后代线程行 + 清理各自 checkpoint（无墓碑）
            (3) 非 cascade 且无后代：物理删除自身线程行 + 清理 checkpoint（无墓碑）
            (4) 非 cascade 且有后代：保留自身线程行并置 deleted_at（墓碑），清自身内容与 checkpoint，
                保留后代 source 行
        """
        to_clean: list[str] = []
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            if await self._has_active_run(session, context_id):
                raise HTTPException(409, "会话正在运行，不能删除")
            if cascade:
                ids = [context_id, *await self._descendant_ids(session, context_id)]
                await self._hard_delete_threads(session, ids, to_clean)
            else:
                descendants = await self._descendant_ids(session, context_id)
                if descendants:
                    await self._tombstone_deleted(session, task, to_clean)
                else:
                    await self._hard_delete_threads(session, [context_id], to_clean)
            await session.commit()
        for thread_id in to_clean:
            try:
                await self.checkpointer.adelete_thread(thread_id)
            except Exception:
                logger.warning("清理会话 checkpoint 失败: thread_id=%s", thread_id, exc_info=True)
        return {"context_id": context_id, "deleted": True}

    async def delete_many(self, context_ids: list[str], cascade: bool = False) -> dict[str, Any]:
        """删除多个会话（根/派生 Context），即批量删除。语义与 delete 完全一致，需确认。

        输入:
            context_ids: list[str] — 待删除的会话标识集合（重复或血缘重叠会被去重/合并）
            cascade: bool — True 时递归删除每个被选会话及其全部派生后代（无墓碑）

        输出:
            dict — {context_ids, deleted: True}

        工作流:
            (1) 去重 context_ids；为空集合时 422
            (2) 逐个校验：会话不存在 404、存在 pending/running 主运行 409（整批拒绝，不删除任何会话）
            (3) 按「级联 / 有后代 / 无后代」分类复用删除引擎：级联并入硬删除列表；有后代走墓碑；
                无后代并入硬删除列表；touched 并集去重血缘重叠，避免重复处理
            (4) 单事务提交；提交后统一用 checkpointer.adelete_thread 清理（去重、best-effort）
        """
        ids = list(dict.fromkeys(context_ids))
        if not ids:
            raise HTTPException(422, "未提供要删除的会话")
        to_clean: list[str] = []
        async with self.session_factory() as session:
            for cid in ids:
                task = await session.get(DesktopThread, cid)
                if not task:
                    raise HTTPException(404, f"会话不存在: {cid}")
                if await self._has_active_run(session, cid):
                    raise HTTPException(409, f"会话正在运行，不能删除: {cid}")
            hard: list[str] = []
            touched: set[str] = set()
            for cid in ids:
                if cid in touched:
                    continue
                descendants = await self._descendant_ids(session, cid)
                if cascade:
                    hard.extend([cid, *descendants])
                    touched.update([cid, *descendants])
                elif descendants:
                    task = await session.get(DesktopThread, cid)
                    await self._tombstone_deleted(session, task, to_clean)
                    touched.add(cid)
                else:
                    hard.append(cid)
                    touched.add(cid)
            await self._hard_delete_threads(session, list(dict.fromkeys(hard)), to_clean)
            await session.commit()
        for thread_id in dict.fromkeys(to_clean):
            try:
                await self.checkpointer.adelete_thread(thread_id)
            except Exception:
                logger.warning("清理会话 checkpoint 失败: thread_id=%s", thread_id, exc_info=True)
        return {"context_ids": ids, "deleted": True}

    async def list_archived(self) -> list[dict[str, Any]]:
        """列出全部已归档会话（archived_at 非空且未 deleted_at）。"""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(DesktopThread, DesktopWorkspace)
                    .join(DesktopWorkspace, DesktopThread.workspace_id == DesktopWorkspace.workspace_id)
                    .where(
                        DesktopThread.archived_at.is_not(None),
                        DesktopThread.deleted_at.is_(None),
                    )
                    .order_by(DesktopThread.archived_at)
                )
            ).all()
            return [
                {
                    "context_id": task.task_id,
                    "thread_id": task.thread_id,
                    "title": task.title,
                    "workspace_id": task.workspace_id,
                    "workspace_name": workspace.display_name,
                    "archived_at": task.archived_at.isoformat() if task.archived_at else None,
                }
                for task, workspace in rows
            ]

    async def get(self, context_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            definition = await session.get(DesktopContextDefinition, context_id)
            sources = (
                await session.scalars(
                    select(DesktopContextSource)
                    .where(DesktopContextSource.context_id == context_id)
                    .order_by(DesktopContextSource.position)
                )
            ).all()
            has_main_run = bool(
                await session.scalar(
                    select(DesktopRun.run_id)
                    .where(DesktopRun.task_id == context_id, DesktopRun.kind == "main")
                    .limit(1)
                )
            )
            return self._payload(task, definition, sources, editable=bool(definition) and not has_main_run)

    async def lineage(self, context_id: str) -> dict[str, Any]:
        payload = await self.get(context_id)
        async with self.session_factory() as session:
            depth = await self._depth(session, context_id, {})
        return {"context_id": context_id, "depth": depth, "sources": payload["sources"]}

    async def tree(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            tasks = (
                await session.scalars(
                    select(DesktopThread)
                    .where(DesktopThread.workspace_id == workspace_id)
                    .order_by(DesktopThread.created_at)
                )
            ).all()
            source_rows = (
                await session.scalars(
                    select(DesktopContextSource)
                    .where(DesktopContextSource.context_id.in_([task.task_id for task in tasks]))
                    .order_by(DesktopContextSource.context_id, DesktopContextSource.position)
                )
            ).all() if tasks else []
            definitions = {
                item.context_id: item
                for item in (
                    await session.scalars(
                        select(DesktopContextDefinition).where(
                            DesktopContextDefinition.context_id.in_([task.task_id for task in tasks])
                        )
                    )
                ).all()
            } if tasks else {}
            run_stats = {
                task_id: (int(input_tokens or 0), int(hit_tokens or 0))
                for task_id, input_tokens, hit_tokens in (
                    await session.execute(
                        select(
                            DesktopRun.task_id,
                            func.sum(DesktopRun.prompt_input_tokens),
                            func.sum(DesktopRun.prompt_cache_hit_tokens),
                        )
                        .where(
                            DesktopRun.task_id.in_([task.task_id for task in tasks]),
                            DesktopRun.kind == "main",
                        )
                        .group_by(DesktopRun.task_id)
                    )
                ).all()
            } if tasks else {}
            by_child: dict[str, list[DesktopContextSource]] = {}
            for source in source_rows:
                by_child.setdefault(source.context_id, []).append(source)
            memo: dict[str, int] = {}

            def depth(context_id: str) -> int:
                if context_id in memo:
                    return memo[context_id]
                sources = by_child.get(context_id, [])
                memo[context_id] = 0 if not sources else depth(sources[0].parent_context_id) + 1
                return memo[context_id]

            result = []
            for task in tasks:
                input_tokens, hit_tokens = run_stats.get(task.task_id, (0, 0))
                result.append({
                    "context_id": task.task_id,
                    "task_id": task.task_id,
                    "thread_id": task.thread_id,
                    "title": task.title,
                    "depth": depth(task.task_id),
                    "lifecycle": self._lifecycle(task),
                    "projection_status": (
                        definitions[task.task_id].projection_status if task.task_id in definitions else "root"
                    ),
                    "editable": task.task_id in definitions and task.task_id not in run_stats,
                    "cache_input_tokens": input_tokens,
                    "cache_hit_tokens": hit_tokens,
                    "cache_hit_rate": hit_tokens / input_tokens if input_tokens else None,
                    "parents": [self._source_payload(source) for source in by_child.get(task.task_id, [])],
                })
            return result

    async def ensure_runnable(self, session: AsyncSession, context_id: str) -> None:
        definition = await session.scalar(
            select(DesktopContextDefinition)
            .where(DesktopContextDefinition.context_id == context_id)
            .with_for_update()
        )
        if definition and definition.projection_status not in _RUNNABLE_STATUSES:
            raise HTTPException(
                409,
                {
                    "code": "context_projection_blocked",
                    "message": "Context 执行投影尚未得到安全确认",
                    "context": self._definition_payload(definition),
                },
            )

    async def _initialize(self, context_id: str, ready_status: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            definition = await session.get(DesktopContextDefinition, context_id)
            if not task or not definition:
                raise HTTPException(404, "Context 不存在")
            config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
            existing = await self.checkpointer.aget_tuple(config)
            execution_messages = deepcopy(definition.execution_messages)
            replace_existing = False
            if existing is not None:
                values = existing.checkpoint.get("channel_values", {})
                stored = [serialize_message(message) for message in values.get("messages", [])]
                if self._comparable_messages(stored) != self._comparable_messages(execution_messages):
                    replace_existing = True
            definition.projection_status = "initializing"
            await session.commit()
        try:
            if existing is None or replace_existing:
                graph = await self._make_state_graph()
                messages = deserialize_messages(execution_messages)
                for raw, message in zip(execution_messages, messages):
                    if raw.get("curation_synthetic"):
                        message.additional_kwargs["curation_synthetic"] = True
                update = (
                    [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
                    if replace_existing
                    else messages
                )
                updated_config = await graph.aupdate_state(
                    config,
                    {"messages": update},
                    as_node="model" if replace_existing else None,
                )
                state = await graph.aget_state(updated_config)
                checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id")
                initial_ids = [message.id for message in state.values.get("messages", []) if message.id]
            else:
                checkpoint_id = existing.config.get("configurable", {}).get("checkpoint_id")
                values = existing.checkpoint.get("channel_values", {})
                initial_ids = [message.id for message in values.get("messages", []) if message.id]
        except Exception as exc:
            async with self.session_factory() as session:
                definition = await session.get(DesktopContextDefinition, context_id)
                if definition:
                    definition.projection_status = "initialization_failed"
                    definition.issues = [*definition.issues, {"index": None, "reason": str(exc)}]
                    await session.commit()
            return
        async with self.session_factory() as session:
            definition = await session.get(DesktopContextDefinition, context_id)
            if not definition:
                return
            definition.initial_checkpoint_id = checkpoint_id
            definition.initial_message_ids = initial_ids
            definition.projection_status = ready_status
            await session.commit()

    async def _make_state_graph(self):
        graph = await make_lead_agent(
            tools=[], system_prompt="", middlewares=[], app_config=self.app_config
        )
        graph.checkpointer = self.checkpointer
        return graph

    async def _require_checkpoint(self, thread_id: str, checkpoint_id: str) -> None:
        checkpoint = await self.checkpointer.aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id}}
        )
        actual = checkpoint.config.get("configurable", {}).get("checkpoint_id") if checkpoint else None
        if actual != checkpoint_id:
            raise HTTPException(404, "来源 checkpoint 不存在或不属于指定 Context")

    async def _has_active_run(self, session: AsyncSession, context_id: str) -> bool:
        """判断会话是否存在 pending/running 主运行（避免边运行边归档/删除）。"""
        return bool(
            await session.scalar(
                select(DesktopRun.run_id)
                .where(
                    DesktopRun.task_id == context_id,
                    DesktopRun.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
        )

    async def _descendant_ids(self, session: AsyncSession, context_id: str) -> list[str]:
        """经 desktop_context_sources（parent_context_id → context_id）BFS 求全部派生子会话 id。"""
        result: list[str] = []
        queue = [context_id]
        seen = {context_id}
        while queue:
            current = queue.pop(0)
            children = (
                await session.scalars(
                    select(DesktopContextSource.context_id)
                    .where(DesktopContextSource.parent_context_id == current)
                )
            ).all()
            for cid in children:
                if cid not in seen:
                    seen.add(cid)
                    result.append(cid)
                    queue.append(cid)
        return result

    async def _hard_delete_threads(
        self, session: AsyncSession, ids: list[str], to_clean: list[str]
    ) -> None:
        """自底向上（叶子优先）物理删除多个会话线程行，确保 parent RESTRICT 不报错。

        删除线程行会经 FK CASCADE 清掉其 definition/sources/runs 等关联记录；
        将被删线程的 thread_id 追加到 to_clean，供提交后清理 LangGraph checkpoint。
        """
        ordered = list(reversed(ids))
        for cid in ordered:
            row = await session.get(DesktopThread, cid)
            if row is None:
                continue
            to_clean.append(row.thread_id)
            await session.execute(delete(DesktopThread).where(DesktopThread.task_id == cid))

    async def _tombstone_deleted(
        self, session: AsyncSession, task: DesktopThread, to_clean: list[str]
    ) -> None:
        """针对有后代的删除：保留线程行并置 deleted_at（墓碑），清自身内容与 checkpoint。

        保留以 parent_context_id=自身 的后代 source 行（后代引用墓碑，血缘不断）。
        """
        cid = task.task_id
        task.deleted_at = datetime.now(timezone.utc)
        await session.execute(
            delete(DesktopContextDefinition).where(DesktopContextDefinition.context_id == cid)
        )
        await session.execute(
            delete(DesktopContextSource).where(DesktopContextSource.context_id == cid)
        )
        await session.execute(delete(DesktopRun).where(DesktopRun.task_id == cid))
        await session.execute(delete(DesktopMaterial).where(DesktopMaterial.task_id == cid))
        await session.execute(delete(PatrolDraft).where(PatrolDraft.task_id == cid))
        await session.execute(delete(PatrolAgent).where(PatrolAgent.task_id == cid))
        await session.execute(delete(SwarmAgent).where(SwarmAgent.task_id == cid))
        await session.execute(delete(AgentMessage).where(AgentMessage.task_id == cid))
        await session.execute(delete(AgentBoardTask).where(AgentBoardTask.thread_task_id == cid))
        to_clean.append(task.thread_id)

    @staticmethod
    def _lifecycle(task: DesktopThread) -> str:
        """返回会话生命周期标记：deleted / archived / active。"""
        if task.deleted_at is not None:
            return "deleted"
        if task.archived_at is not None:
            return "archived"
        return "active"

    async def _depth(self, session: AsyncSession, context_id: str, memo: dict[str, int]) -> int:
        if context_id in memo:
            return memo[context_id]
        source = await session.scalar(
            select(DesktopContextSource)
            .where(DesktopContextSource.context_id == context_id)
            .order_by(DesktopContextSource.position)
        )
        memo[context_id] = 0 if not source else await self._depth(session, source.parent_context_id, memo) + 1
        return memo[context_id]

    @staticmethod
    def _apply_projection(definition: DesktopContextDefinition, projection: ContextProjection) -> None:
        definition.authored_messages = projection.authored_messages
        definition.execution_messages = projection.execution_messages
        definition.repair_manifest = projection.repair_manifest
        definition.issues = projection.issues
        definition.definition_hash = projection.definition_hash
        definition.projection_hash = projection.projection_hash
        definition.projection_status = projection.status
        definition.initial_message_ids = []
        definition.initial_checkpoint_id = None

    @staticmethod
    def _display_messages(
        definition: DesktopContextDefinition | None,
        runtime_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if definition is None:
            return runtime_messages
        initial_ids = set(definition.initial_message_ids or [])
        suffix = [message for message in runtime_messages if message.get("id") not in initial_ids]
        return [*deepcopy(definition.authored_messages), *suffix]

    @staticmethod
    def _comparable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = deepcopy(messages)
        for message in result:
            message.pop("id", None)
            message.pop("locked", None)
            message.pop("curation_synthetic", None)
        return result

    @classmethod
    def _payload(
        cls,
        task: DesktopThread,
        definition: DesktopContextDefinition | None,
        sources: list[DesktopContextSource],
        *,
        editable: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "context_id": task.task_id,
            "task_id": task.task_id,
            "workspace_id": task.workspace_id,
            "thread_id": task.thread_id,
            "title": task.title,
            "projection_status": "root",
            "lifecycle": cls._lifecycle(task),
            "sources": [cls._source_payload(source) for source in sources],
            "editable": editable,
        }
        if definition:
            payload.update(cls._definition_payload(definition))
        return payload

    @staticmethod
    def _definition_payload(definition: DesktopContextDefinition) -> dict[str, Any]:
        return {
            "projection_status": definition.projection_status,
            "authored_messages": deepcopy(definition.authored_messages),
            "execution_messages": deepcopy(definition.execution_messages),
            "repair_manifest": deepcopy(definition.repair_manifest),
            "issues": deepcopy(definition.issues),
            "definition_hash": definition.definition_hash,
            "projection_hash": definition.projection_hash,
            "initial_checkpoint_id": definition.initial_checkpoint_id,
            "decision": definition.decision,
        }

    @staticmethod
    def _source_payload(source: DesktopContextSource) -> dict[str, Any]:
        return {
            "context_id": source.parent_context_id,
            "checkpoint_id": source.source_checkpoint_id,
            "position": source.position,
        }
