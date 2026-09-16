"""
本文件对外提供 ContextService，协调桌面 Context 的快照、派生、投影审批、受管发布与生命周期。

输入为 session factory、LangGraph checkpointer、AppConfig 和 Context 请求模型；输出为 API 可序列化
的 current/historical revision、Evolution Graph、display messages 与生命周期结果。具体工作流为把
手工派生和审批交给 ContextEvolutionService，把一对一 Curator 先映射成单 Lane Curation Program，
再经 SingleLanePortfolioPublisher 原子发布；已有受管 Context 的真实运行后缀会从旧 revision 提取并
接到新 authored/execution 前缀之后。删除时由 retention planner 保留仍被后代引用的最小 tombstone，
本服务不再读写 identity-level definition/source 权威表。示例：`context = await service.derive(body)`。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any, Literal
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_projection import ContextProjection, compile_context_messages
from backend.app.desktop.context_curation import (
    PortfolioPublicationError,
    PortfolioSuperseded,
    SingleLaneCurationProgramService,
    SingleLanePortfolioPublisher,
)
from backend.app.desktop.context_evolution import (
    ContextEvolutionQueryService,
    ContextEvolutionService,
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionRepository,
    ContextRevisionRetentionPlanner,
    ContextRevisionRef,
    ContextRevisionSourceContract,
)
from backend.app.desktop.models import (
    AgentBoardTask,
    AgentMessage,
    ContextDefinitionUpdate,
    ContextDeriveCreate,
    ContextProjectionDecision,
    DesktopMaterial,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    PatrolAgent,
    PatrolContextBinding,
    PatrolContextRevision,
    PatrolDraft,
    SwarmAgent,
)
from focus.agents.lead import make_lead_agent
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import serialize_message


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
        self.evolution = ContextEvolutionService(
            session_factory, checkpointer, self.make_state_graph
        )
        self.evolution_queries = ContextEvolutionQueryService(
            ContextRevisionRepository()
        )
        self.revision_retention = ContextRevisionRetentionPlanner()
        self.single_lane_programs = SingleLaneCurationProgramService()
        self.single_lane_publications = SingleLanePortfolioPublisher(
            session_factory,
            checkpointer,
            self.make_state_graph,
        )

    async def snapshot(self, context_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if not task:
                raise HTTPException(404, "Context 不存在")
            if checkpoint_id is not None:
                view = await self.evolution.read_checkpoint_display(
                    session, context_id, checkpoint_id
                )
                if view is not None:
                    return {
                        "context_id": context_id,
                        "checkpoint_id": view.ref.checkpoint_id,
                        "messages": [deepcopy(message) for message in view.messages],
                    }
        revision_view = await self.evolution.read_current_display(context_id)
        if revision_view is not None and checkpoint_id is None:
            return {
                "context_id": context_id,
                "checkpoint_id": revision_view.ref.checkpoint_id,
                "messages": [deepcopy(message) for message in revision_view.messages],
            }
        config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
        if checkpoint_id is not None:
            config["configurable"]["checkpoint_id"] = checkpoint_id
        checkpoint = await self.checkpointer.aget_tuple(config)
        if checkpoint is None:
            if checkpoint_id is not None:
                raise HTTPException(404, "Context checkpoint 不存在")
            return {"context_id": context_id, "checkpoint_id": None, "messages": []}
        actual_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if checkpoint_id is not None and actual_id != checkpoint_id:
            raise HTTPException(404, "Context checkpoint 不属于该 Context")
        return {
            "context_id": context_id,
            "checkpoint_id": actual_id,
            "messages": [
                serialize_message(message)
                for message in checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
            ],
        }

    async def resolve_chat_root(self, context_id: str) -> str:
        async with self.session_factory() as session:
            return await self._resolve_chat_root(session, context_id)

    async def current_checkpoint_id(self, context_id: str) -> str | None:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if task is None:
                raise HTTPException(404, "Context 不存在")
            current = await self.evolution.current(session, context_id)
            if (
                current is not None
                and current.ref.checkpoint_id is not None
                and (
                    current.ref.execution_thread_id != task.thread_id
                    or current.ref.checkpoint_ns
                )
            ):
                return current.ref.checkpoint_id
        checkpoint = await self.checkpointer.aget_tuple(
            {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
        )
        return checkpoint.config.get("configurable", {}).get("checkpoint_id") if checkpoint else None

    async def derive(self, body: ContextDeriveCreate) -> dict[str, Any]:
        refs = [(source.context_id, source.checkpoint_id) for source in body.sources]
        if len(refs) != len(set(refs)):
            raise HTTPException(422, "Context 来源不能重复")
        context_id = _new_id()
        async with self.session_factory.begin() as session:
            parents = [await session.get(DesktopThread, context_id) for context_id, _ in refs]
            if any(parent is None for parent in parents):
                raise HTTPException(404, "来源 Context 不存在")
            workspace_ids = {parent.workspace_id for parent in parents if parent is not None}
            if len(workspace_ids) != 1:
                raise HTTPException(422, "只能合并同一工作区的 Context")
            task = DesktopThread(
                task_id=context_id,
                workspace_id=next(iter(workspace_ids)),
                thread_id=_new_id(),
                title=body.title,
                ui_state={},
            )
            session.add(task)
            await session.flush()
            source_contracts = []
            for index, source in enumerate(body.sources):
                source_ref = await self.evolution.ensure_checkpoint_revision(
                    session,
                    source.context_id,
                    source.checkpoint_id,
                )
                source_contracts.append(
                    ContextRevisionSourceContract(source=source_ref, position=index)
                )
            await self.evolution.stage_definition(
                session,
                context_id,
                tuple(deepcopy(body.messages)),
                tuple(source_contracts),
                ContextRevisionOriginKind.MANUAL_DERIVE,
                context_id,
            )
        return await self.get(context_id)

    async def update_definition(
        self, context_id: str, body: ContextDefinitionUpdate
    ) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.managed_context_id == context_id)
                .with_for_update()
            )
            if binding and binding.control_state != "stopped":
                binding.control_state = "stopped"
                binding.health_state = "idle"
                binding.revision += 1
            current = await self.evolution.current(session, context_id)
            if current is None or current.ref.payload_mode is not ContextRevisionPayloadMode.DEFINITION:
                raise HTTPException(404, "派生 Context 不存在")
            has_main_run = await session.scalar(
                select(DesktopRun.run_id)
                .where(DesktopRun.task_id == context_id, DesktopRun.kind == "main")
                .limit(1)
            )
            if has_main_run:
                raise HTTPException(409, "Context 已开始运行；请从当前快照继续派生")
            await self.evolution.stage_definition(
                session,
                context_id,
                tuple(deepcopy(body.messages)),
                self._next_definition_sources(current),
                ContextRevisionOriginKind.DEFINITION_UPDATE,
                context_id,
            )
        return await self.get(context_id)

    async def decide(
        self, context_id: str, body: ContextProjectionDecision
    ) -> dict[str, Any]:
        async with self.session_factory.begin() as session:
            try:
                await self.evolution.decide_definition(
                    session,
                    context_id,
                    body.decision,
                    body.definition_hash,
                    body.projection_hash,
                )
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return await self.get(context_id)

    async def publish_managed_revision(
        self,
        revision_id: str,
        messages: list[dict[str, Any]],
        dispositions: list[dict[str, Any]],
        *,
        outcome: Literal["replace", "no_change"] = "replace",
    ) -> str:
        if outcome == "no_change" and messages:
            raise ValueError("no_change 不得携带 authored messages")
        projection = compile_context_messages(messages)
        if outcome == "replace" and projection.status != "valid":
            return await self._reject_invalid_managed_projection(
                revision_id, projection, dispositions
            )
        prepared = await self._prepare_managed_portfolio_publication(revision_id)
        if isinstance(prepared, str):
            return prepared
        binding_id, source_refs, continuation = prepared
        try:
            published = await self.single_lane_publications.publish(
                binding_id=binding_id,
                source_frontier=source_refs,
                projection=projection,
                suffix_messages=continuation,
                outcome=outcome,
            )
        except PortfolioSuperseded:
            return await self._supersede_managed_revision(revision_id)
        except PortfolioPublicationError as exc:
            await self.fail_managed_revision(revision_id, str(exc))
            return "error"
        return await self._complete_managed_portfolio_publication(
            revision_id,
            projection,
            dispositions,
            published.context_revision.checkpoint_id,
            outcome,
        )

    async def _prepare_managed_portfolio_publication(
        self,
        revision_id: str,
    ) -> tuple[str, tuple[ContextRevisionRef, ...], tuple[dict[str, Any], ...]] | str:
        async with self.session_factory.begin() as session:
            revision_probe = await session.get(PatrolContextRevision, revision_id)
            if revision_probe is None:
                raise HTTPException(404, "策展修订不存在")
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision_probe.binding_id)
                .with_for_update()
            )
            if binding is None:
                raise HTTPException(404, "策展绑定不存在")
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == revision_id)
                .with_for_update()
            )
            if not self._revision_is_current(binding, revision):
                revision.status = "superseded"
                revision.completed_at = datetime.now(timezone.utc)
                return "superseded"
            task = await self._ensure_managed_identity(session, binding)
            active_main_run = await session.scalar(
                select(DesktopRun.run_id)
                .where(
                    DesktopRun.task_id == task.task_id,
                    DesktopRun.kind == "main",
                    DesktopRun.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
            if active_main_run is not None:
                revision.status = "observed"
                binding.health_state = "idle"
                return "deferred"
            root_ref = await self.evolution.ensure_checkpoint_revision(
                session,
                binding.root_context_id,
                revision.source_checkpoint_id,
            )
            current = await self.evolution.current(session, task.task_id)
            continuation = await self.evolution.continuation_messages(
                session,
                current,
                task.thread_id,
            )
            source_refs = []
            if current is not None and current.ref.is_runnable:
                source_refs.append(current.ref)
            if root_ref.revision_id not in {item.revision_id for item in source_refs}:
                source_refs.append(root_ref)
            revision.status = "publishing"
            binding.health_state = "preparing"
            await self.single_lane_programs.provision(session, binding)
            return binding.binding_id, tuple(source_refs), continuation

    async def _complete_managed_portfolio_publication(
        self,
        revision_id: str,
        projection: ContextProjection,
        dispositions: list[dict[str, Any]],
        checkpoint_id: str | None,
        outcome: Literal["replace", "no_change"],
    ) -> str:
        async with self.session_factory.begin() as session:
            revision = await session.get(PatrolContextRevision, revision_id)
            if revision is None:
                raise HTTPException(404, "策展修订不存在")
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision.binding_id)
                .with_for_update()
            )
            if binding is None:
                raise HTTPException(404, "策展绑定不存在")
            revision.authored_messages = (
                deepcopy(projection.authored_messages) if outcome == "replace" else []
            )
            revision.execution_messages = (
                deepcopy(projection.execution_messages) if outcome == "replace" else []
            )
            revision.disposition_manifest = deepcopy(dispositions)
            revision.repair_manifest = (
                deepcopy(projection.repair_manifest) if outcome == "replace" else []
            )
            revision.issues = deepcopy(projection.issues) if outcome == "replace" else []
            revision.definition_hash = (
                projection.definition_hash if outcome == "replace" else None
            )
            revision.projection_hash = (
                projection.projection_hash if outcome == "replace" else None
            )
            revision.projection_status = (
                projection.status if outcome == "replace" else "unchanged"
            )
            binding.published_checkpoint_id = revision.source_checkpoint_id
            binding.prepared_checkpoint_id = revision.source_checkpoint_id
            binding.revision += 1
            binding.health_state = "idle"
            binding.last_error = None
            revision.status = "published" if outcome == "replace" else "unchanged"
            revision.published_context_checkpoint_id = checkpoint_id
            revision.error = None
            revision.completed_at = datetime.now(timezone.utc)
            revision.published_at = datetime.now(timezone.utc)
        return "published" if outcome == "replace" else "unchanged"

    async def _supersede_managed_revision(self, revision_id: str) -> str:
        async with self.session_factory.begin() as session:
            revision = await session.get(PatrolContextRevision, revision_id)
            if revision is not None:
                revision.status = "superseded"
                revision.completed_at = datetime.now(timezone.utc)
                binding = await session.get(PatrolContextBinding, revision.binding_id)
                if binding is not None:
                    binding.health_state = "idle"
        return "superseded"

    async def _reject_invalid_managed_projection(
        self,
        revision_id: str,
        projection: ContextProjection,
        dispositions: list[dict[str, Any]],
    ) -> str:
        error = (
            "自动策展候选必须直接得到 valid 投影；"
            f"实际为 {projection.status}，不得通过隐藏修补或人工审批发布"
        )
        async with self.session_factory() as session:
            revision_probe = await session.get(PatrolContextRevision, revision_id)
            if revision_probe is None:
                raise HTTPException(404, "策展修订不存在")
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision_probe.binding_id)
                .with_for_update()
            )
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == revision_id)
                .with_for_update()
            )
            if binding is None or revision is None:
                raise HTTPException(404, "策展绑定或修订不存在")
            if not self._revision_is_current(binding, revision):
                revision.status = "superseded"
                revision.completed_at = datetime.now(timezone.utc)
                await session.commit()
                return "superseded"
            revision.authored_messages = deepcopy(projection.authored_messages)
            revision.execution_messages = deepcopy(projection.execution_messages)
            revision.disposition_manifest = deepcopy(dispositions)
            revision.repair_manifest = deepcopy(projection.repair_manifest)
            revision.issues = deepcopy(projection.issues)
            revision.definition_hash = projection.definition_hash
            revision.projection_hash = projection.projection_hash
            revision.projection_status = projection.status
            revision.status = "error"
            revision.error = error
            revision.completed_at = datetime.now(timezone.utc)
            binding.health_state = "degraded"
            binding.last_error = error
            await session.commit()
        return "invalid"

    async def fail_managed_revision(self, revision_id: str, error: str) -> None:
        abandoned_thread_id: str | None = None
        async with self.session_factory() as session:
            revision_probe = await session.get(PatrolContextRevision, revision_id)
            if revision_probe is None:
                return
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision_probe.binding_id)
                .with_for_update()
            )
            current = (
                await self.evolution.current(session, binding.managed_context_id)
                if binding is not None and binding.managed_context_id
                else None
            )
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == revision_id)
                .with_for_update()
            )
            if revision.status in {"published", "superseded"}:
                return
            revision.status = "error"
            revision.error = error
            revision.completed_at = datetime.now(timezone.utc)
            if binding is not None:
                binding.last_error = error
                binding.health_state = "degraded"
                if (
                    binding.published_checkpoint_id is None
                    and current is None
                ):
                    task = await session.get(DesktopThread, binding.managed_context_id)
                    if task is not None:
                        abandoned_thread_id = task.thread_id
                        await session.execute(
                            delete(DesktopThread).where(
                                DesktopThread.task_id == binding.managed_context_id
                            )
                        )
                    binding.managed_context_id = None
            await session.commit()
        if abandoned_thread_id is not None:
            try:
                await self.checkpointer.adelete_thread(abandoned_thread_id)
            except Exception:
                logger.warning(
                    "清理失败的首版受管 Context checkpoint 失败: thread_id=%s",
                    abandoned_thread_id,
                    exc_info=True,
                )

    async def _ensure_managed_identity(
        self,
        session: AsyncSession,
        binding: PatrolContextBinding,
    ) -> DesktopThread:
        if binding.managed_context_id is not None:
            task = await session.get(DesktopThread, binding.managed_context_id)
            if task is None:
                raise HTTPException(409, "受管 Context 已不存在")
            return task

        root = await session.get(DesktopThread, binding.root_context_id)
        if root is None:
            raise HTTPException(409, "根 Context 已不存在")
        context_id = _new_id()
        task = DesktopThread(
            task_id=context_id,
            workspace_id=root.workspace_id,
            thread_id=_new_id(),
            title=f"{root.title} · 策展 Context",
            ui_state={},
        )
        session.add(task)
        await session.flush()
        binding.managed_context_id = context_id
        await self.single_lane_programs.attach_managed_context(
            session,
            binding,
            context_id,
        )
        return task

    @staticmethod
    def _revision_is_current(
        binding: PatrolContextBinding, revision: PatrolContextRevision
    ) -> bool:
        return (
            binding.control_state == "following"
            and binding.desired_checkpoint_id == revision.source_checkpoint_id
            and binding.revision == revision.base_binding_revision
            and revision.status in {"ready", "publishing"}
        )

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
                    await self._transition_bindings_for_context(session, cid, "paused")
            else:
                task.archived_at = now
                await self._transition_bindings_for_context(session, context_id, "paused")
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
                for cid in ids:
                    await self._transition_bindings_for_context(session, cid, "stopped")
                await self._hard_delete_threads(session, ids, to_clean)
            else:
                descendants = await self._descendant_ids(session, context_id)
                await self._transition_bindings_for_context(session, context_id, "stopped")
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
                await self._transition_bindings_for_context(session, cid, "stopped")
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
            current = await self.evolution.current(session, context_id)
            sources = await self._external_sources(session, current)
            has_main_run = bool(
                await session.scalar(
                    select(DesktopRun.run_id)
                    .where(DesktopRun.task_id == context_id, DesktopRun.kind == "main")
                    .limit(1)
                )
            )
            binding = await session.scalar(
                select(PatrolContextBinding).where(
                    PatrolContextBinding.managed_context_id == context_id
                )
            )
            payload = {
                "context_id": task.task_id,
                "task_id": task.task_id,
                "workspace_id": task.workspace_id,
                "thread_id": task.thread_id,
                "title": task.title,
                "projection_status": (
                    current.projection_status.value if current is not None else "root"
                ),
                "lifecycle": self._lifecycle(task),
                "sources": [self._revision_source_payload(source, index) for index, source in enumerate(sources)],
                "editable": bool(
                    current is not None
                    and current.ref.payload_mode is ContextRevisionPayloadMode.DEFINITION
                    and not has_main_run
                ),
                "managed_status": binding.control_state if binding else None,
            }
            if current is not None and current.ref.payload_mode is ContextRevisionPayloadMode.DEFINITION:
                payload.update(self._revision_definition_payload(current))
            return payload

    async def lineage(self, context_id: str) -> dict[str, Any]:
        payload = await self.get(context_id)
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            tree = await self.evolution_queries.first_parent_tree(
                session, task.workspace_id
            )
            node = next(item for item in tree.nodes if item.context_id == context_id)
            depth = node.depth
        return {"context_id": context_id, "depth": depth, "sources": payload["sources"]}

    async def tree(self, workspace_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            tasks = list((
                await session.scalars(
                    select(DesktopThread)
                    .where(DesktopThread.workspace_id == workspace_id)
                    .order_by(DesktopThread.created_at)
                )
            ).all())
            compatibility_tree = await self.evolution_queries.first_parent_tree(
                session, workspace_id
            )
            tree_nodes = {node.context_id: node for node in compatibility_tree.nodes}
            managed_bindings = {
                item.managed_context_id: item
                for item in (
                    await session.scalars(
                        select(PatrolContextBinding).where(
                            PatrolContextBinding.managed_context_id.in_(
                                [task.task_id for task in tasks]
                            )
                        )
                    )
                ).all()
                if item.managed_context_id
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
            result = []
            for task in tasks:
                input_tokens, hit_tokens = run_stats.get(task.task_id, (0, 0))
                current = await self.evolution.current(session, task.task_id)
                node = tree_nodes[task.task_id]
                sources = tuple(
                    source
                    for source in (node.primary_source, *node.secondary_sources)
                    if source is not None
                )
                result.append({
                    "context_id": task.task_id,
                    "task_id": task.task_id,
                    "thread_id": task.thread_id,
                    "title": task.title,
                    "depth": node.depth,
                    "lifecycle": self._lifecycle(task),
                    "projection_status": (
                        current.projection_status.value if current is not None else "root"
                    ),
                    "editable": bool(
                        current is not None
                        and current.ref.payload_mode is ContextRevisionPayloadMode.DEFINITION
                        and task.task_id not in run_stats
                    ),
                    "cache_input_tokens": input_tokens,
                    "cache_hit_tokens": hit_tokens,
                    "cache_hit_rate": hit_tokens / input_tokens if input_tokens else None,
                    "parents": [
                        self._revision_source_payload(source, index)
                        for index, source in enumerate(sources)
                    ],
                    "managed_status": (
                        managed_bindings[task.task_id].control_state
                        if task.task_id in managed_bindings else None
                    ),
                    "managed_health": (
                        managed_bindings[task.task_id].health_state
                        if task.task_id in managed_bindings else None
                    ),
                })
            return result

    async def ensure_runnable(self, session: AsyncSession, context_id: str) -> str | None:
        revision = await ContextRevisionRepository().current(session, context_id)
        if revision is not None and (
            revision.projection_status.value not in _RUNNABLE_STATUSES
            or not revision.ref.is_runnable
        ):
            raise HTTPException(
                409,
                {
                    "code": "context_revision_blocked",
                    "message": "当前 Context revision 尚不可运行",
                    "revision_id": revision.ref.revision_id,
                    "projection_status": revision.projection_status.value,
                },
            )
        binding = await session.scalar(
            select(PatrolContextBinding)
            .where(PatrolContextBinding.managed_context_id == context_id)
            .with_for_update()
        )
        publishing = (
            await session.scalar(
                select(PatrolContextRevision.revision_id)
                .where(
                    PatrolContextRevision.binding_id == binding.binding_id,
                    PatrolContextRevision.status == "publishing",
                )
                .with_for_update()
                .limit(1)
            )
            if binding is not None
            else None
        )
        if publishing is not None:
            raise HTTPException(409, "受管 Context 正在发布新版本，请稍后再运行")
        if binding is not None:
            if binding.health_state == "blocked":
                raise HTTPException(409, "受管 Context 仍在等待投影决断")
            if binding.health_state in {"preparing", "running"}:
                raise HTTPException(409, "受管 Context 正在更新，请稍后再运行")
            has_main_run = bool(
                await session.scalar(
                    select(DesktopRun.run_id)
                    .where(
                        DesktopRun.task_id == context_id,
                        DesktopRun.kind == "main",
                    )
                    .limit(1)
                )
            )
            return revision.ref.checkpoint_id if revision is not None and not has_main_run else None
        # 普通派生 Context 沿用自身 thread 的最新合法 checkpoint；由调用方的
        # checkpoint recovery 统一处理降级。
        return None

    async def _make_state_graph(self):
        graph = await make_lead_agent(
            tools=[], system_prompt="", middlewares=[], app_config=self.app_config
        )
        graph.checkpointer = self.checkpointer
        return graph

    async def make_state_graph(self):
        return await self._make_state_graph()

    async def _resolve_chat_root(self, session: AsyncSession, context_id: str) -> str:
        current = context_id
        visited: set[str] = set()
        while True:
            if current in visited:
                raise HTTPException(409, "Context 第一父链存在循环")
            visited.add(current)
            task = await session.get(DesktopThread, current)
            if task is None or task.deleted_at is not None:
                raise HTTPException(404, "Context 第一父链包含不存在或已删除节点")
            revision = await self.evolution.current(session, current)
            sources = await self._external_sources(session, revision)
            if not sources:
                return current
            current = sources[0].context_id

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
        """经 current revision 的版本化来源边 BFS 求全部派生 Context identity。"""
        task = await session.get(DesktopThread, context_id)
        if task is None:
            return []
        tasks = list(
            (
                await session.scalars(
                    select(DesktopThread).where(
                        DesktopThread.workspace_id == task.workspace_id
                    )
                )
            ).all()
        )
        children_by_source: dict[str, set[str]] = {}
        for candidate in tasks:
            revision = await self.evolution.current(session, candidate.task_id)
            for source in await self._external_sources(session, revision):
                children_by_source.setdefault(source.context_id, set()).add(
                    candidate.task_id
                )
        result: list[str] = []
        queue = [context_id]
        seen = {context_id}
        while queue:
            current = queue.pop(0)
            children = sorted(children_by_source.get(current, set()))
            for cid in children:
                if cid not in seen:
                    seen.add(cid)
                    result.append(cid)
                    queue.append(cid)
        return result

    async def _transition_bindings_for_context(
        self, session: AsyncSession, context_id: str, target: str
    ) -> None:
        bindings = (
            await session.scalars(
                select(PatrolContextBinding)
                .where(
                    (PatrolContextBinding.root_context_id == context_id)
                    | (PatrolContextBinding.managed_context_id == context_id)
                )
                .with_for_update()
            )
        ).all()
        for binding in bindings:
            if binding.control_state == "stopped":
                continue
            binding.control_state = target
            binding.health_state = "idle"
            binding.revision += 1

    async def _hard_delete_threads(
        self, session: AsyncSession, ids: list[str], to_clean: list[str]
    ) -> None:
        plan = await self.revision_retention.plan(session, ids)
        for cid in plan.tombstones:
            row = await session.get(DesktopThread, cid)
            if row is not None:
                await self._tombstone_deleted(session, row, to_clean)
        hard = list(plan.hard_delete)
        if not hard:
            return
        await self.single_lane_programs.delete_for_contexts(session, hard)
        ordered = list(reversed(hard))
        for cid in ordered:
            row = await session.get(DesktopThread, cid)
            if row is None:
                continue
            to_clean.append(row.thread_id)
            await session.execute(delete(DesktopThread).where(DesktopThread.task_id == cid))

    async def _tombstone_deleted(
        self, session: AsyncSession, task: DesktopThread, to_clean: list[str]
    ) -> None:
        cid = task.task_id
        if task.deleted_at is not None:
            return
        await self.evolution.publish_tombstone(session, cid, origin_id=cid)
        task.deleted_at = datetime.now(timezone.utc)
        await session.execute(delete(DesktopRun).where(DesktopRun.task_id == cid))
        await session.execute(delete(DesktopMaterial).where(DesktopMaterial.task_id == cid))
        await session.execute(delete(PatrolDraft).where(PatrolDraft.task_id == cid))
        await session.execute(
            delete(PatrolAgent).where(PatrolAgent.task_id == cid, PatrolAgent.mode == "standard")
        )
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

    async def _external_sources(
        self,
        session: AsyncSession,
        revision: ContextRevisionContract | None,
    ) -> tuple[ContextRevisionRef, ...]:
        if revision is None:
            return ()
        repository = ContextRevisionRepository()
        found: list[ContextRevisionRef] = []
        visited: set[str] = set()

        async def visit(candidate: ContextRevisionContract) -> None:
            if candidate.ref.revision_id in visited:
                return
            visited.add(candidate.ref.revision_id)
            for edge in candidate.sources:
                if edge.source.context_id != revision.ref.context_id:
                    if edge.source.revision_id not in {item.revision_id for item in found}:
                        found.append(edge.source)
                    continue
                await visit(await repository.get(session, edge.source))

        await visit(revision)
        return tuple(found)

    @staticmethod
    def _revision_source_payload(
        source: ContextRevisionRef, position: int
    ) -> dict[str, Any]:
        return {
            "context_id": source.context_id,
            "checkpoint_id": source.checkpoint_id,
            "revision_id": source.revision_id,
            "position": position,
        }

    @staticmethod
    def _revision_definition_payload(
        revision: ContextRevisionContract,
    ) -> dict[str, Any]:
        decision = None
        if revision.projection_status.value == "approved":
            decision = "accept"
        elif revision.projection_status.value == "rejected":
            decision = "reject"
        return {
            "projection_status": revision.projection_status.value,
            "authored_messages": deepcopy(list(revision.authored_messages)),
            "execution_messages": deepcopy(list(revision.execution_messages)),
            "repair_manifest": deepcopy(list(revision.repair_manifest)),
            "issues": deepcopy(list(revision.issues)),
            "definition_hash": revision.definition_hash,
            "projection_hash": revision.projection_hash,
            "initial_checkpoint_id": revision.ref.checkpoint_id,
            "decision": decision,
        }

    @staticmethod
    def _next_definition_sources(
        current: ContextRevisionContract,
    ) -> tuple[ContextRevisionSourceContract, ...]:
        if current.ref.is_runnable:
            return (ContextRevisionSourceContract(source=current.ref, position=0),)
        return current.sources
