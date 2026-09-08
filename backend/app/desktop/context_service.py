"""
本文件对外提供 ContextService，负责桌面 Context 的快照、根解析、派生、lineage、执行
投影审批、受管版本发布与独立 checkpoint 初始化。

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
from typing import Any, Literal
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
    PatrolContextBinding,
    PatrolContextRevision,
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
        # 未指定 checkpoint 时读取该 Context 的最新运行态。initial_checkpoint_id
        # 只是派生/策展投影的起点，不是永久固定的读取指针；Context 一旦被执行，
        # 后续用户与 AI 消息都存在同一 thread 的更新 checkpoint 中。
        effective_checkpoint_id = checkpoint_id
        config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
        if effective_checkpoint_id:
            config["configurable"]["checkpoint_id"] = effective_checkpoint_id
        checkpoint = await self.checkpointer.aget_tuple(config)
        if checkpoint is None:
            if effective_checkpoint_id:
                raise HTTPException(404, "Context checkpoint 不存在")
            messages = deepcopy(definition.authored_messages) if definition else []
            return {"context_id": context_id, "checkpoint_id": None, "messages": messages}
        actual_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if effective_checkpoint_id and actual_id != effective_checkpoint_id:
            raise HTTPException(404, "Context checkpoint 不属于该 Context")
        values = checkpoint.checkpoint.get("channel_values", {})
        runtime_messages = [serialize_message(message) for message in values.get("messages", [])]
        messages = (
            self._display_messages(definition, runtime_messages)
            if definition and actual_id == definition.initial_checkpoint_id
            else runtime_messages
        )
        return {"context_id": context_id, "checkpoint_id": actual_id, "messages": messages}

    async def resolve_chat_root(self, context_id: str) -> str:
        async with self.session_factory() as session:
            return await self._resolve_chat_root(session, context_id)

    async def current_checkpoint_id(self, context_id: str) -> str | None:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            if task is None:
                raise HTTPException(404, "Context 不存在")
            definition = await session.get(DesktopContextDefinition, context_id)
        checkpoint = await self.checkpointer.aget_tuple(
            {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
        )
        return (
            checkpoint.config.get("configurable", {}).get("checkpoint_id")
            if checkpoint is not None
            else definition.initial_checkpoint_id if definition is not None else None
        )

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
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.managed_context_id == context_id)
                .with_for_update()
            )
            if binding and binding.control_state != "stopped":
                binding.control_state = "stopped"
                binding.health_state = "idle"
                binding.revision += 1
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
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.managed_context_id == context_id)
                .with_for_update()
            )
            definition = await session.scalar(
                select(DesktopContextDefinition)
                .where(DesktopContextDefinition.context_id == context_id)
                .with_for_update()
            )
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
            if binding is not None:
                await self._complete_managed_approval(binding.binding_id)
        elif binding is not None:
            await self._reject_managed_approval(binding.binding_id)
        return await self.get(context_id)

    async def publish_managed_revision(
        self,
        revision_id: str,
        messages: list[dict[str, Any]],
        dispositions: list[dict[str, Any]],
        *,
        outcome: Literal["replace", "no_change"] = "replace",
    ) -> str:
        """以绑定游标 CAS 发布整版 Context 或确认无变化。"""
        if outcome == "no_change":
            if messages:
                raise ValueError("no_change 不得携带 authored messages")
            return await self._acknowledge_unchanged_revision(revision_id, dispositions)
        projection = compile_context_messages(messages)
        if projection.status != "valid":
            return await self._reject_invalid_managed_projection(
                revision_id, projection, dispositions
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
            if binding is None:
                raise HTTPException(404, "策展绑定不存在")
            if binding.managed_context_id:
                await session.scalar(
                    select(DesktopContextDefinition)
                    .where(DesktopContextDefinition.context_id == binding.managed_context_id)
                    .with_for_update()
                )
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == revision_id)
                .with_for_update()
            )
            if not self._revision_is_current(binding, revision):
                revision.status = "superseded"
                revision.completed_at = datetime.now(timezone.utc)
                await session.commit()
                return "superseded"

            task, definition = await self._ensure_managed_rows(
                session, binding, projection, revision.source_checkpoint_id
            )
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
                await session.commit()
                return "deferred"
            has_main_run = bool(
                await session.scalar(
                    select(DesktopRun.run_id)
                    .where(DesktopRun.task_id == task.task_id, DesktopRun.kind == "main")
                    .limit(1)
                )
            )
            revision.authored_messages = deepcopy(projection.authored_messages)
            revision.execution_messages = deepcopy(projection.execution_messages)
            revision.disposition_manifest = deepcopy(dispositions)
            revision.repair_manifest = deepcopy(projection.repair_manifest)
            revision.issues = deepcopy(projection.issues)
            revision.definition_hash = projection.definition_hash
            revision.projection_hash = projection.projection_hash
            revision.projection_status = projection.status
            revision.status = "publishing"
            thread_id = task.thread_id
            base_checkpoint_id = definition.initial_checkpoint_id
            previous_initial_message_ids = set(definition.initial_message_ids or [])
            await session.commit()

        try:
            suffix_messages: list[dict[str, Any]] = []
            if has_main_run:
                latest = await self.checkpointer.aget_tuple(
                    {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                )
                if latest is not None:
                    base_checkpoint_id = latest.config.get("configurable", {}).get(
                        "checkpoint_id"
                    )
                    suffix_messages = [
                        serialize_message(message)
                        for message in latest.checkpoint.get("channel_values", {}).get(
                            "messages", []
                        )
                        if message.id not in previous_initial_message_ids
                    ]
            checkpoint_id, initial_ids = await self._write_projection_checkpoint(
                thread_id,
                projection.execution_messages,
                base_checkpoint_id,
                suffix_messages=suffix_messages,
            )
        except Exception as exc:
            await self.fail_managed_revision(revision_id, str(exc))
            return "error"

        async with self.session_factory() as session:
            revision_probe = await session.get(PatrolContextRevision, revision_id)
            if revision_probe is None:
                return "superseded"
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision_probe.binding_id)
                .with_for_update()
            )
            definition = await session.scalar(
                select(DesktopContextDefinition)
                .where(DesktopContextDefinition.context_id == binding.managed_context_id)
                .with_for_update()
            )
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == revision_id)
                .with_for_update()
            )
            if not self._revision_is_current(binding, revision) or revision.status != "publishing":
                revision.status = "superseded"
                revision.completed_at = datetime.now(timezone.utc)
                await session.commit()
                return "superseded"
            active_main_run = await session.scalar(
                select(DesktopRun.run_id)
                .where(
                    DesktopRun.task_id == binding.managed_context_id,
                    DesktopRun.kind == "main",
                    DesktopRun.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
            if active_main_run:
                revision.status = "observed"
                binding.health_state = "idle"
                await session.commit()
                return "deferred"

            self._apply_projection(definition, projection)
            definition.initial_checkpoint_id = checkpoint_id
            definition.initial_message_ids = initial_ids
            definition.projection_status = projection.status
            source = await session.scalar(
                select(DesktopContextSource)
                .where(DesktopContextSource.context_id == binding.managed_context_id)
                .order_by(DesktopContextSource.position)
                .with_for_update()
            )
            if source is not None:
                source.source_checkpoint_id = revision.source_checkpoint_id
            binding.published_checkpoint_id = revision.source_checkpoint_id
            binding.prepared_checkpoint_id = revision.source_checkpoint_id
            binding.revision += 1
            binding.health_state = "idle"
            binding.last_error = None
            revision.status = "published"
            revision.published_context_checkpoint_id = checkpoint_id
            revision.completed_at = datetime.now(timezone.utc)
            revision.published_at = datetime.now(timezone.utc)
            await session.commit()
        return "published"

    async def _acknowledge_unchanged_revision(
        self,
        revision_id: str,
        dispositions: list[dict[str, Any]],
    ) -> str:
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
            revision.authored_messages = []
            revision.execution_messages = []
            revision.disposition_manifest = deepcopy(dispositions)
            revision.repair_manifest = []
            revision.issues = []
            revision.projection_status = "unchanged"
            revision.status = "unchanged"
            revision.error = None
            revision.completed_at = datetime.now(timezone.utc)
            revision.published_at = datetime.now(timezone.utc)
            binding.published_checkpoint_id = revision.source_checkpoint_id
            binding.prepared_checkpoint_id = revision.source_checkpoint_id
            binding.revision += 1
            binding.health_state = "idle"
            binding.last_error = None
            await session.commit()
        return "unchanged"

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
            definition = (
                await session.scalar(
                    select(DesktopContextDefinition)
                    .where(
                        DesktopContextDefinition.context_id
                        == binding.managed_context_id
                    )
                    .with_for_update()
                )
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
                    and definition is not None
                    and definition.initial_checkpoint_id is None
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

    async def _reject_managed_approval(self, binding_id: str) -> None:
        """拒绝当前候选并恢复上一成功发布版本；首次候选则等待新的根版本。"""
        async with self.session_factory() as session:
            binding_probe = await session.get(PatrolContextBinding, binding_id)
            if binding_probe is None or binding_probe.health_state != "blocked":
                return
            task = await session.get(DesktopThread, binding_probe.managed_context_id)
            previous = await session.scalar(
                select(PatrolContextRevision)
                .where(
                    PatrolContextRevision.binding_id == binding_id,
                    PatrolContextRevision.status == "published",
                )
                .order_by(PatrolContextRevision.published_at.desc())
            )
        initial_ids: list[str] = []
        if previous is not None and previous.published_context_checkpoint_id and task is not None:
            checkpoint = await self.checkpointer.aget_tuple({
                "configurable": {
                    "thread_id": task.thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": previous.published_context_checkpoint_id,
                }
            })
            if checkpoint is not None:
                initial_ids = [
                    message.id
                    for message in checkpoint.checkpoint.get("channel_values", {}).get(
                        "messages", []
                    )
                    if message.id
                ]

        async with self.session_factory() as session:
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == binding_id)
                .with_for_update()
            )
            if binding is None or binding.health_state != "blocked":
                return
            definition = await session.scalar(
                select(DesktopContextDefinition)
                .where(DesktopContextDefinition.context_id == binding.managed_context_id)
                .with_for_update()
            )
            rejected = await session.scalar(
                select(PatrolContextRevision)
                .where(
                    PatrolContextRevision.binding_id == binding_id,
                    PatrolContextRevision.status == "approval_required",
                )
                .order_by(PatrolContextRevision.created_at.desc())
                .with_for_update()
            )
            if definition is None or rejected is None:
                return
            previous = await session.scalar(
                select(PatrolContextRevision)
                .where(
                    PatrolContextRevision.binding_id == binding_id,
                    PatrolContextRevision.status == "published",
                )
                .order_by(PatrolContextRevision.published_at.desc())
            )
            rejected.status = "superseded"
            rejected.completed_at = datetime.now(timezone.utc)
            binding.health_state = "idle"
            binding.revision += 1
            binding.last_error = None
            if previous is not None:
                definition.authored_messages = deepcopy(previous.authored_messages)
                definition.execution_messages = deepcopy(previous.execution_messages)
                definition.repair_manifest = deepcopy(previous.repair_manifest)
                definition.issues = deepcopy(previous.issues)
                definition.definition_hash = previous.definition_hash
                definition.projection_hash = previous.projection_hash
                definition.projection_status = previous.projection_status
                definition.initial_checkpoint_id = previous.published_context_checkpoint_id
                definition.initial_message_ids = initial_ids
            source = await session.scalar(
                select(DesktopContextSource)
                .where(DesktopContextSource.context_id == binding.managed_context_id)
                .order_by(DesktopContextSource.position)
                .with_for_update()
            )
            if source is not None and binding.published_checkpoint_id:
                source.source_checkpoint_id = binding.published_checkpoint_id
            pending = await session.scalar(
                select(PatrolContextRevision).where(
                    PatrolContextRevision.binding_id == binding_id,
                    PatrolContextRevision.source_checkpoint_id
                    == binding.desired_checkpoint_id,
                    PatrolContextRevision.status == "observed",
                )
            )
            if pending is not None:
                pending.base_binding_revision = binding.revision
                pending.source_payload = {
                    **(pending.source_payload or {}),
                    "base_binding_revision": binding.revision,
                }
            await session.commit()

    async def _ensure_managed_rows(
        self,
        session: AsyncSession,
        binding: PatrolContextBinding,
        projection: ContextProjection,
        source_checkpoint_id: str,
    ) -> tuple[DesktopThread, DesktopContextDefinition]:
        if binding.managed_context_id is not None:
            task = await session.get(DesktopThread, binding.managed_context_id)
            definition = await session.get(DesktopContextDefinition, binding.managed_context_id)
            if task is None or definition is None:
                raise HTTPException(409, "受管 Context 已不存在")
            return task, definition

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
        definition = DesktopContextDefinition(
            context_id=context_id,
            authored_messages=deepcopy(projection.authored_messages),
            execution_messages=deepcopy(projection.execution_messages),
            repair_manifest=deepcopy(projection.repair_manifest),
            issues=deepcopy(projection.issues),
            definition_hash=projection.definition_hash,
            projection_hash=projection.projection_hash,
            projection_status="initializing",
            initial_message_ids=[],
        )
        source = DesktopContextSource(
            source_id=_new_id(),
            context_id=context_id,
            parent_context_id=binding.root_context_id,
            source_checkpoint_id=source_checkpoint_id,
            position=0,
        )
        session.add(task)
        await session.flush()
        session.add_all([definition, source])
        await session.flush()
        binding.managed_context_id = context_id
        return task, definition

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

    async def _complete_managed_approval(self, binding_id: str) -> None:
        async with self.session_factory() as session:
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == binding_id)
                .with_for_update()
            )
            if binding is None or binding.health_state != "blocked":
                return
            definition = await session.scalar(
                select(DesktopContextDefinition)
                .where(DesktopContextDefinition.context_id == binding.managed_context_id)
                .with_for_update()
            )
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(
                    PatrolContextRevision.binding_id == binding_id,
                    PatrolContextRevision.status == "approval_required",
                )
                .order_by(PatrolContextRevision.created_at.desc())
                .with_for_update()
            )
            if definition is None or revision is None or definition.initial_checkpoint_id is None:
                return
            source = await session.scalar(
                select(DesktopContextSource)
                .where(DesktopContextSource.context_id == binding.managed_context_id)
                .order_by(DesktopContextSource.position)
                .with_for_update()
            )
            if source is not None:
                source.source_checkpoint_id = revision.source_checkpoint_id
            binding.published_checkpoint_id = revision.source_checkpoint_id
            binding.prepared_checkpoint_id = revision.source_checkpoint_id
            binding.revision += 1
            binding.health_state = "idle"
            binding.last_error = None
            revision.status = "published"
            revision.published_context_checkpoint_id = definition.initial_checkpoint_id
            revision.completed_at = datetime.now(timezone.utc)
            revision.published_at = datetime.now(timezone.utc)
            pending = await session.scalar(
                select(PatrolContextRevision).where(
                    PatrolContextRevision.binding_id == binding_id,
                    PatrolContextRevision.source_checkpoint_id
                    == binding.desired_checkpoint_id,
                    PatrolContextRevision.status == "observed",
                )
            )
            if pending is not None:
                pending.base_binding_revision = binding.revision
                pending.source_payload = {
                    **(pending.source_payload or {}),
                    "base_binding_revision": binding.revision,
                }
            await session.commit()

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
            binding = await session.scalar(
                select(PatrolContextBinding).where(
                    PatrolContextBinding.managed_context_id == context_id
                )
            )
            return self._payload(
                task,
                definition,
                sources,
                editable=bool(definition) and not has_main_run,
                managed_status=binding.control_state if binding else None,
            )

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
        binding = await session.scalar(
            select(PatrolContextBinding)
            .where(PatrolContextBinding.managed_context_id == context_id)
            .with_for_update()
        )
        definition = await session.scalar(
            select(DesktopContextDefinition)
            .where(DesktopContextDefinition.context_id == context_id)
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
        if definition and definition.projection_status not in _RUNNABLE_STATUSES:
            raise HTTPException(
                409,
                {
                    "code": "context_projection_blocked",
                    "message": "Context 执行投影尚未得到安全确认",
                    "context": self._definition_payload(definition),
                },
            )
        if definition and definition.initial_checkpoint_id is None:
            raise HTTPException(409, "Context 尚无完整可运行 checkpoint")
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
            return definition.initial_checkpoint_id if not has_main_run else None
        # 普通派生 Context 沿用自身 thread 的最新合法 checkpoint；由调用方的
        # checkpoint recovery 统一处理降级。
        return None

    async def _initialize(self, context_id: str, ready_status: str) -> None:
        async with self.session_factory() as session:
            task = await session.get(DesktopThread, context_id)
            definition = await session.get(DesktopContextDefinition, context_id)
            if not task or not definition:
                raise HTTPException(404, "Context 不存在")
            execution_messages = deepcopy(definition.execution_messages)
            base_checkpoint_id = definition.initial_checkpoint_id
            expected_definition_hash = definition.definition_hash
            expected_projection_hash = definition.projection_hash
            definition.projection_status = "initializing"
            await session.commit()
        try:
            checkpoint_id, initial_ids = await self._write_projection_checkpoint(
                task.thread_id, execution_messages, base_checkpoint_id
            )
        except Exception as exc:
            async with self.session_factory() as session:
                definition = await session.get(DesktopContextDefinition, context_id)
                if definition:
                    definition.projection_status = "initialization_failed"
                    definition.issues = [*definition.issues, {"index": None, "reason": str(exc)}]
                    await session.commit()
            return
        async with self.session_factory() as session:
            definition = await session.scalar(
                select(DesktopContextDefinition)
                .where(DesktopContextDefinition.context_id == context_id)
                .with_for_update()
            )
            if not definition:
                return
            if (
                definition.definition_hash != expected_definition_hash
                or definition.projection_hash != expected_projection_hash
            ):
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

    async def _write_projection_checkpoint(
        self,
        thread_id: str,
        execution_messages: list[dict[str, Any]],
        base_checkpoint_id: str | None,
        *,
        suffix_messages: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[str]]:
        graph = await self._make_state_graph()
        config: dict[str, Any] = {
            "configurable": {"thread_id": thread_id, "checkpoint_ns": ""}
        }
        if base_checkpoint_id:
            config["configurable"]["checkpoint_id"] = base_checkpoint_id
        existing = await self.checkpointer.aget_tuple(config)
        projected_messages = deserialize_messages(deepcopy(execution_messages))
        for raw, message in zip(execution_messages, projected_messages):
            if raw.get("curation_synthetic"):
                message.additional_kwargs["curation_synthetic"] = True
        preserved_messages = deserialize_messages(deepcopy(suffix_messages or []))
        messages = [*projected_messages, *preserved_messages]
        update = (
            [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
            if existing is not None
            else messages
        )
        updated_config = await graph.aupdate_state(
            config,
            {"messages": update},
            as_node="model" if existing is not None else None,
        )
        state = await graph.aget_state(updated_config)
        checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            raise RuntimeError("写入 Context checkpoint 后未返回 checkpoint_id")
        initial_ids = [
            message.id
            for message in state.values.get("messages", [])[: len(projected_messages)]
            if message.id
        ]
        return checkpoint_id, initial_ids

    async def _require_checkpoint(self, thread_id: str, checkpoint_id: str) -> None:
        checkpoint = await self.checkpointer.aget_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id}}
        )
        actual = checkpoint.config.get("configurable", {}).get("checkpoint_id") if checkpoint else None
        if actual != checkpoint_id:
            raise HTTPException(404, "来源 checkpoint 不存在或不属于指定 Context")

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
            source = await session.scalar(
                select(DesktopContextSource)
                .where(DesktopContextSource.context_id == current)
                .order_by(DesktopContextSource.position)
            )
            if source is None:
                return current
            current = source.parent_context_id

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
        """自底向上（叶子优先）物理删除多个会话线程行，确保 parent RESTRICT 不报错。

        删除线程行会经 FK CASCADE 清掉其 definition/sources/runs 等关联记录；
        将被删线程的 thread_id 追加到 to_clean，供提交后清理 LangGraph checkpoint。
        """
        await session.execute(
            delete(DesktopContextSource).where(
                DesktopContextSource.context_id.in_(ids)
            )
        )
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
        managed_status: str | None = None,
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
            "managed_status": managed_status,
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
