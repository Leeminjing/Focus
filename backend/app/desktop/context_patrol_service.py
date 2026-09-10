r"""
本文件对外提供 ContextPatrolService，编排 Context 策展 Patrol 的耐久观察与单次模型调用。

输入为数据库 session factory、ContextService、StreamBridge、RunManager 与 AppConfig；输出为
部署、稳定 checkpoint 通知、控制状态、审计详情和启动/关闭方法。具体工作流为先用短事务
提交 root checkpoint 事实，再依次执行安全来源投影、CurationEngine、CuratedContextCompiler
和受管 Context CAS 发布；Revision 表示来源版本，Attempt 表示可重复的模型调用。
示例：`await service.notify_stable_context_checkpoint(context_id)`。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_curator import (
    CurationContractError,
    CurationEngine,
    CurationEngineError,
    CurationSourceProjector,
    CurationSourceSnapshot,
    build_curation_input,
    compile_curated_context,
    estimate_curation_tokens,
    require_curation_model,
)
from backend.app.desktop.context_service import ContextService
from backend.app.desktop.models import (
    ContextCurationPolicy,
    DesktopContextDefinition,
    DesktopRun,
    DesktopThread,
    PatrolAgent,
    PatrolContextAttempt,
    PatrolContextBinding,
    PatrolContextRevision,
    PatrolDraft,
)
from focus.config.app_config import AppConfig
from focus.runtime.runs.events import build_envelope
from focus.runtime.runs.manager import RunManager, RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus
from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent


logger = logging.getLogger(__name__)
_ACTIVE_ATTEMPT_STATUSES = frozenset({"pending", "running"})
_TERMINAL_RUN_STATUSES = frozenset({"success", "error", "interrupted", "timeout"})
_TRANSIENT_RETRY_DELAYS = (1.0, 5.0)


def _new_id() -> str:
    return uuid.uuid4().hex


class ContextPatrolService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        contexts: ContextService,
        checkpointer: Any,
        store: Any,
        bridge: StreamBridge,
        run_manager: RunManager,
        app_config: AppConfig,
    ) -> None:
        self.session_factory = session_factory
        self.contexts = contexts
        self.checkpointer = checkpointer
        self.store = store
        self.bridge = bridge
        self.run_manager = run_manager
        self.app_config = app_config
        self.projector = CurationSourceProjector()
        self.engine = CurationEngine(app_config)
        self._event = asyncio.Event()
        self._coordinator_task: asyncio.Task | None = None
        self._run_tasks: set[asyncio.Task] = set()
        self._closed = False

    async def start(self) -> None:
        if self._coordinator_task is not None:
            return
        self._closed = False
        await self._reconcile()
        self._coordinator_task = asyncio.create_task(self._coordinate())
        self._event.set()

    async def close(self) -> None:
        self._closed = True
        self._event.set()
        tasks = list(self._run_tasks)
        if self._coordinator_task is not None:
            self._coordinator_task.cancel()
            tasks.append(self._coordinator_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._run_tasks.clear()
        self._coordinator_task = None

    def wake(self) -> None:
        self._event.set()

    async def deploy(self, draft_id: str, deployment_id: str) -> DesktopRun:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(DesktopRun).where(DesktopRun.deployment_id == deployment_id)
            )
            if existing is not None:
                return existing
            draft = await session.scalar(
                select(PatrolDraft).where(PatrolDraft.draft_id == draft_id).with_for_update()
            )
            if draft is None or draft.status != "editing" or draft.mode != "context_curator":
                raise HTTPException(404, "可投放的 Context 策展草稿不存在")
            try:
                self.engine.validate_model(draft.equipment.get("model_name"))
            except CurationEngineError as exc:
                raise HTTPException(422, {"code": "curation_model_incompatible", "message": str(exc)}) from exc
            draft_task_id = draft.task_id

        root_context_id = await self.contexts.resolve_chat_root(draft_task_id)
        source_checkpoint_id = await self.contexts.current_checkpoint_id(root_context_id)
        if source_checkpoint_id is None:
            raise HTTPException(409, "根 Context 尚无稳定 checkpoint")

        agent_id = _new_id()
        binding_id = _new_id()
        revision_id = _new_id()
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(DesktopRun).where(DesktopRun.deployment_id == deployment_id)
            )
            if existing is not None:
                return existing
            draft = await session.scalar(
                select(PatrolDraft).where(PatrolDraft.draft_id == draft_id).with_for_update()
            )
            if draft is None or draft.status != "editing" or draft.mode != "context_curator":
                raise HTTPException(409, "草稿状态已经变化")
            policy = ContextCurationPolicy.model_validate(draft.curation_policy or {})
            agent = PatrolAgent(
                agent_id=agent_id,
                task_id=draft.task_id,
                checkpoint_ns=f"patrol:{agent_id}",
                system_prompt="",
                frozen_messages=[],
                equipment={
                    "model_name": draft.equipment.get("model_name"),
                    "permissions": ["read"],
                    "skills": [],
                },
                source_checkpoint_id=source_checkpoint_id,
                mode="context_curator",
                curation_policy=policy.model_dump(mode="json"),
            )
            binding = PatrolContextBinding(
                binding_id=binding_id,
                agent_id=agent_id,
                root_context_id=root_context_id,
                control_state="following",
                health_state="idle",
                observed_checkpoint_id=source_checkpoint_id,
                desired_checkpoint_id=source_checkpoint_id,
                revision=0,
            )
            revision = PatrolContextRevision(
                revision_id=revision_id,
                binding_id=binding_id,
                source_checkpoint_id=source_checkpoint_id,
                base_binding_revision=0,
                status="observed",
            )
            draft.status = "deployed"
            session.add_all([agent, binding, revision])
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                winner = await session.scalar(
                    select(DesktopRun).where(DesktopRun.deployment_id == deployment_id)
                )
                if winner is not None:
                    return winner
                raise

        run = await self._schedule_revision(revision_id, deployment_id=deployment_id)
        if run is None:
            raise HTTPException(409, "策展修订未能进入执行队列")
        return run

    async def notify_stable_context_checkpoint(self, context_id: str) -> None:
        root_context_id = await self.contexts.resolve_chat_root(context_id)
        async with self.session_factory() as session:
            active = await session.scalar(
                select(DesktopRun.run_id)
                .where(
                    DesktopRun.task_id == root_context_id,
                    DesktopRun.kind == "main",
                    DesktopRun.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
            if active:
                return
        checkpoint_id = await self.contexts.current_checkpoint_id(root_context_id)
        if checkpoint_id is None:
            return

        should_wake = False
        async with self.session_factory() as session:
            bindings = list((await session.scalars(
                select(PatrolContextBinding)
                .where(
                    PatrolContextBinding.root_context_id == root_context_id,
                    PatrolContextBinding.control_state.in_(["following", "paused"]),
                )
                .with_for_update()
            )).all())
            for binding in bindings:
                existing = await session.scalar(
                    select(PatrolContextRevision).where(
                        PatrolContextRevision.binding_id == binding.binding_id,
                        PatrolContextRevision.source_checkpoint_id == checkpoint_id,
                    )
                )
                cursor_changed = (
                    binding.observed_checkpoint_id != checkpoint_id
                    or binding.desired_checkpoint_id != checkpoint_id
                )
                if cursor_changed:
                    binding.observed_checkpoint_id = checkpoint_id
                    binding.desired_checkpoint_id = checkpoint_id
                if existing is None:
                    existing = PatrolContextRevision(
                        revision_id=_new_id(),
                        binding_id=binding.binding_id,
                        source_checkpoint_id=checkpoint_id,
                        base_binding_revision=binding.revision,
                        status="observed",
                    )
                    session.add(existing)
                should_wake = should_wake or (
                    binding.control_state == "following"
                    and binding.health_state != "blocked"
                    and existing.status == "observed"
                )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
        if should_wake:
            self._event.set()

    async def set_tracking_state(self, agent_id: str, target: str) -> dict[str, Any]:
        if target not in {"following", "paused", "stopped"}:
            raise HTTPException(422, "非法跟踪状态")
        async with self.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if agent is None or agent.mode != "context_curator":
                raise HTTPException(404, "Context 策展 Patrol 不存在")
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.agent_id == agent_id)
                .with_for_update()
            )
            if binding is None:
                raise HTTPException(404, "Context 策展绑定不存在")
            allowed = {
                "following": {"following", "paused", "stopped"},
                "paused": {"paused", "following", "stopped"},
                "stopped": {"stopped"},
            }
            if target not in allowed[binding.control_state]:
                raise HTTPException(409, f"不能从 {binding.control_state} 转换到 {target}")
            if target != binding.control_state:
                binding.control_state = target
                binding.revision += 1
            if target == "following":
                binding.health_state = "idle"
                latest = await session.scalar(
                    select(PatrolContextRevision).where(
                        PatrolContextRevision.binding_id == binding.binding_id,
                        PatrolContextRevision.source_checkpoint_id == binding.desired_checkpoint_id,
                    )
                )
                if latest is not None and latest.status in {"error", "interrupted"}:
                    latest.status = "observed"
                    latest.error = None
                    latest.base_binding_revision = binding.revision
            elif target in {"paused", "stopped"}:
                binding.health_state = "idle"
            await session.commit()
        if target == "following":
            self._event.set()
        return await self.detail(agent_id)

    async def detail(
        self, agent_id: str, *, limit: int = 50, before: str | None = None
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        async with self.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if agent is None or agent.mode != "context_curator":
                raise HTTPException(404, "Context 策展 Patrol 不存在")
            binding = await session.scalar(
                select(PatrolContextBinding).where(PatrolContextBinding.agent_id == agent_id)
            )
            if binding is None:
                raise HTTPException(404, "Context 策展绑定不存在")
            query = (
                select(PatrolContextRevision)
                .where(PatrolContextRevision.binding_id == binding.binding_id)
                .order_by(PatrolContextRevision.created_at.desc())
                .limit(limit + 1)
            )
            if before:
                anchor = await session.get(PatrolContextRevision, before)
                if anchor is not None and anchor.binding_id == binding.binding_id:
                    query = query.where(PatrolContextRevision.created_at < anchor.created_at)
            revisions = list((await session.scalars(query)).all())
            has_more = len(revisions) > limit
            revisions = revisions[:limit]
            attempts: dict[str, list[PatrolContextAttempt]] = {}
            for revision in revisions:
                attempts[revision.revision_id] = list((await session.scalars(
                    select(PatrolContextAttempt)
                    .where(PatrolContextAttempt.revision_id == revision.revision_id)
                    .order_by(PatrolContextAttempt.attempt_number.desc())
                )).all())
            root = await session.get(DesktopThread, binding.root_context_id)
            managed = (
                await session.get(DesktopThread, binding.managed_context_id)
                if binding.managed_context_id else None
            )
            latest_run = await session.scalar(
                select(DesktopRun)
                .where(DesktopRun.agent_id == agent_id)
                .order_by(DesktopRun.created_at.desc())
            )
            return self._binding_payload(
                binding, root, managed, revisions, attempts, latest_run, has_more=has_more
            )

    async def history(self, agent_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            binding = await session.scalar(
                select(PatrolContextBinding).where(PatrolContextBinding.agent_id == agent_id)
            )
            if binding is None:
                raise HTTPException(404, "Context 策展绑定不存在")
            attempt = await session.scalar(
                select(PatrolContextAttempt)
                .join(PatrolContextRevision)
                .where(PatrolContextRevision.binding_id == binding.binding_id)
                .order_by(PatrolContextAttempt.created_at.desc())
            )
            if attempt is None:
                return []
            revision = await session.get(PatrolContextRevision, attempt.revision_id)
            return [
                {"role": "human", "content": deepcopy(revision.source_payload)},
                {"role": "ai", "content": deepcopy(attempt.raw_response)},
            ]

    async def _coordinate(self) -> None:
        while not self._closed:
            await self._event.wait()
            self._event.clear()
            while not self._closed:
                revision_id = await self._next_revision_id()
                if revision_id is None:
                    break
                await self._schedule_revision(revision_id)

    async def _next_revision_id(self) -> str | None:
        async with self.session_factory() as session:
            candidates = list((await session.execute(
                select(PatrolContextRevision, PatrolContextBinding)
                .join(PatrolContextBinding)
                .where(
                    PatrolContextRevision.status == "observed",
                    PatrolContextBinding.control_state == "following",
                    PatrolContextBinding.health_state != "blocked",
                    PatrolContextRevision.source_checkpoint_id == PatrolContextBinding.desired_checkpoint_id,
                )
                .order_by(PatrolContextRevision.created_at)
                .limit(32)
            )).all())
            for candidate, binding in candidates:
                if await self._managed_context_has_active_main_run(session, binding):
                    continue
                busy = await session.scalar(
                    select(PatrolContextAttempt.attempt_id)
                    .join(PatrolContextRevision)
                    .where(
                        PatrolContextRevision.binding_id == candidate.binding_id,
                        PatrolContextAttempt.status.in_(list(_ACTIVE_ATTEMPT_STATUSES)),
                    )
                    .limit(1)
                )
                if busy is None:
                    return candidate.revision_id
        return None

    async def _schedule_revision(
        self, revision_id: str, *, deployment_id: str | None = None
    ) -> DesktopRun | None:
        async with self.session_factory() as session:
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == revision_id)
                .with_for_update()
            )
            if revision is None or revision.status != "observed":
                return None
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision.binding_id)
                .with_for_update()
            )
            if (
                binding is None or binding.control_state != "following"
                or binding.health_state == "blocked"
                or binding.desired_checkpoint_id != revision.source_checkpoint_id
            ):
                return None
            if await self._managed_context_has_active_main_run(session, binding):
                return None
            busy = await session.scalar(
                select(PatrolContextAttempt.attempt_id)
                .join(PatrolContextRevision)
                .where(
                    PatrolContextRevision.binding_id == binding.binding_id,
                    PatrolContextAttempt.status.in_(list(_ACTIVE_ATTEMPT_STATUSES)),
                )
                .limit(1)
            )
            if busy is not None:
                return None
            stale = list((await session.scalars(
                select(PatrolContextRevision).where(
                    PatrolContextRevision.binding_id == binding.binding_id,
                    PatrolContextRevision.revision_id != revision.revision_id,
                    PatrolContextRevision.status == "observed",
                )
            )).all())
            for item in stale:
                item.status = "superseded"
                item.completed_at = datetime.now(timezone.utc)
            agent = await session.get(PatrolAgent, binding.agent_id)
            root = await session.get(DesktopThread, binding.root_context_id)
            model = self._model_config(agent.equipment.get("model_name"))
            try:
                method = require_curation_model(model)
            except CurationEngineError as exc:
                revision.status = "error"
                revision.error = str(exc)
                binding.health_state = "blocked"
                binding.last_error = str(exc)
                await session.commit()
                return None
            number = int(await session.scalar(
                select(func.coalesce(func.max(PatrolContextAttempt.attempt_number), 0)).where(
                    PatrolContextAttempt.revision_id == revision_id
                )
            ) or 0) + 1
            run = DesktopRun(
                run_id=_new_id(),
                task_id=agent.task_id,
                agent_id=agent.agent_id,
                deployment_id=deployment_id,
                kind="patrol",
                status="pending",
                input_messages=[],
                model_name=model.name,
            )
            attempt = PatrolContextAttempt(
                attempt_id=_new_id(),
                revision_id=revision_id,
                attempt_number=number,
                run_id=run.run_id,
                model_name=model.name,
                output_method=method,
                status="pending",
            )
            revision.status = "preparing"
            revision.base_binding_revision = binding.revision
            binding.health_state = "preparing"
            session.add_all([run, attempt])
            await session.commit()
            attempt_id = attempt.attempt_id
            run_id = run.run_id
            thread_id = root.thread_id

        self.run_manager.create(
            thread_id=thread_id,
            run_id=run_id,
            on_disconnect=DisconnectMode.continue_,
            model_name=model.name,
        )
        await self._prepare_attempt(attempt_id)
        async with self.session_factory() as session:
            return await session.get(DesktopRun, run.run_id)

    async def _prepare_attempt(self, attempt_id: str) -> None:
        async with self.session_factory() as session:
            attempt = await session.get(PatrolContextAttempt, attempt_id)
            revision = await session.get(PatrolContextRevision, attempt.revision_id)
            binding = await session.get(PatrolContextBinding, revision.binding_id)
            agent = await session.get(PatrolAgent, binding.agent_id)
            definition = (
                await session.get(DesktopContextDefinition, binding.managed_context_id)
                if binding.managed_context_id else None
            )
            published = deepcopy(definition.authored_messages) if definition else []
            policy = ContextCurationPolicy.model_validate(agent.curation_policy or {})
            model_name = attempt.model_name
            checkpoint_id = revision.source_checkpoint_id
            base_revision = revision.base_binding_revision
        try:
            root_snapshot = await self.contexts.snapshot(binding.root_context_id, checkpoint_id)
            source = self.projector.project(checkpoint_id, root_snapshot["messages"])
            payload = build_curation_input(source, base_revision, policy, published)
            self._require_model_window(model_name, payload)
        except Exception as exc:
            await self._record_attempt_error(attempt_id, self._error_kind(exc), str(exc))
            return

        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(PatrolContextAttempt)
                .where(PatrolContextAttempt.attempt_id == attempt_id)
                .with_for_update()
            )
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == attempt.revision_id)
                .with_for_update()
            )
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision.binding_id)
                .with_for_update()
            )
            if (
                binding.control_state != "following"
                or binding.desired_checkpoint_id != revision.source_checkpoint_id
            ):
                attempt.status = "superseded"
                attempt.completed_at = datetime.now(timezone.utc)
                revision.status = "superseded"
                binding.health_state = "idle"
                await session.commit()
                return
            revision.source_payload = payload
            revision.source_projection_hash = source.projection_hash
            revision.status = "ready"
            revision.error = None
            revision.completed_at = None
            revision.prepared_at = datetime.now(timezone.utc)
            binding.prepared_checkpoint_id = revision.source_checkpoint_id
            binding.health_state = "running"
            binding.last_error = None
            attempt.status = "running"
            attempt.started_at = datetime.now(timezone.utc)
            run = await session.get(DesktopRun, attempt.run_id)
            run.input_messages = [{"role": "human", "content": payload}]
            await session.commit()
        self._launch_attempt(attempt_id)

    def _launch_attempt(self, attempt_id: str) -> None:
        task = asyncio.create_task(self._run_attempt(attempt_id))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)

    async def _run_attempt(self, attempt_id: str) -> None:
        async with self.session_factory() as session:
            attempt = await session.get(PatrolContextAttempt, attempt_id)
            revision = await session.get(PatrolContextRevision, attempt.revision_id)
            binding = await session.get(PatrolContextBinding, revision.binding_id)
            agent = await session.get(PatrolAgent, binding.agent_id)
            root = await session.get(DesktopThread, binding.root_context_id)
            run = await session.get(DesktopRun, attempt.run_id)
            payload = deepcopy(revision.source_payload)
        record = self.run_manager.get(run.run_id)
        if record is None:
            record = self.run_manager.create(
                thread_id=root.thread_id,
                run_id=run.run_id,
                on_disconnect=DisconnectMode.continue_,
                model_name=run.model_name,
            )
        current_task = asyncio.current_task()
        record.task = current_task
        envelope = {
            "workspace_id": root.workspace_id,
            "thread_id": root.thread_id,
            "agent_id": agent.agent_id,
        }
        self.run_manager.update(record.run_id, status=RunStatus.running)
        self.bridge.publish(record.run_id, StreamEvent(
            id="",
            event="metadata",
            data=build_envelope(
                envelope["workspace_id"], envelope["thread_id"], envelope["agent_id"],
                record.run_id, "metadata", {"status": "running", "mode": "context_curator"},
            ),
        ))
        result = None
        try:
            result = await self.engine.curate(run.model_name, payload)
            if record.abort_event.is_set():
                raise asyncio.CancelledError
            compiled = compile_curated_context(
                result.plan, self._source_snapshot_from_payload(payload)
            )
            record.prompt_input_tokens = result.prompt_input_tokens
            record.prompt_cache_hit_tokens = result.prompt_cache_hit_tokens
            publish_outcome = await self._record_attempt_success(attempt_id, result, compiled)
            if publish_outcome in {"invalid", "error"}:
                self.run_manager.update(
                    record.run_id,
                    status=RunStatus.error,
                    error=(
                        "策展候选不是可直接发布的合法 Context"
                        if publish_outcome == "invalid"
                        else "策展 Context 发布失败"
                    ),
                )
            else:
                self.run_manager.update(record.run_id, status=RunStatus.success)
        except asyncio.CancelledError:
            self.run_manager.update(record.run_id, status=RunStatus.interrupted)
            await self._record_attempt_error(attempt_id, "cancelled", "策展运行已取消", interrupted=True)
        except CurationEngineError as exc:
            self.run_manager.update(record.run_id, status=RunStatus.error, error=str(exc))
            await self._record_attempt_error(
                attempt_id, exc.kind, str(exc), raw_response=exc.raw_response
            )
        except Exception as exc:
            logger.exception("Context 策展 attempt 失败: %s", attempt_id)
            self.run_manager.update(record.run_id, status=RunStatus.error, error=str(exc))
            await self._record_attempt_error(
                attempt_id,
                "content" if isinstance(exc, CurationContractError) else "publication",
                str(exc),
                raw_response=result.raw_response if result is not None else None,
                parsed_response=(
                    result.plan.model_dump(mode="json") if result is not None else None
                ),
            )
        finally:
            await self._sync_run(record)
            self.bridge.publish_end(record.run_id)
            self.bridge.cleanup(record.run_id, delay=300)
            self.run_manager._cleanup_later(record.run_id)
            self._event.set()

    async def _record_attempt_success(self, attempt_id: str, result: Any, compiled: Any) -> str:
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(PatrolContextAttempt)
                .where(PatrolContextAttempt.attempt_id == attempt_id)
                .with_for_update()
            )
            revision_id = attempt.revision_id
            attempt.raw_response = deepcopy(result.raw_response)
            attempt.parsed_response = result.plan.model_dump(mode="json")
            await session.commit()
        outcome = await self.contexts.publish_managed_revision(
            revision_id,
            compiled.authored_messages,
            compiled.disposition_manifest,
            outcome=compiled.outcome,
        )
        async with self.session_factory() as session:
            attempt = await session.get(PatrolContextAttempt, attempt_id)
            if attempt is not None:
                attempt.completed_at = datetime.now(timezone.utc)
                if outcome == "superseded":
                    attempt.status = "superseded"
                elif outcome == "invalid":
                    attempt.status = "error"
                    attempt.error_kind = "content"
                    attempt.error = "策展候选不是可直接发布的合法 Context"
                elif outcome == "error":
                    attempt.status = "error"
                    attempt.error_kind = "publication"
                    attempt.error = "策展 Context 发布失败"
                else:
                    attempt.status = "success"
                    attempt.error_kind = None
                    attempt.error = None
                await session.commit()
        return outcome

    @staticmethod
    async def _managed_context_has_active_main_run(
        session: AsyncSession,
        binding: PatrolContextBinding,
    ) -> bool:
        if binding.managed_context_id is None:
            return False
        return bool(
            await session.scalar(
                select(DesktopRun.run_id)
                .where(
                    DesktopRun.task_id == binding.managed_context_id,
                    DesktopRun.kind == "main",
                    DesktopRun.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
        )

    async def _record_attempt_error(
        self,
        attempt_id: str,
        kind: str,
        message: str,
        *,
        raw_response: dict[str, Any] | None = None,
        parsed_response: dict[str, Any] | None = None,
        interrupted: bool = False,
    ) -> None:
        retry_delay: float | None = None
        run_id = None
        async with self.session_factory() as session:
            attempt = await session.scalar(
                select(PatrolContextAttempt)
                .where(PatrolContextAttempt.attempt_id == attempt_id)
                .with_for_update()
            )
            if attempt is None:
                return
            revision = await session.scalar(
                select(PatrolContextRevision)
                .where(PatrolContextRevision.revision_id == attempt.revision_id)
                .with_for_update()
            )
            binding = await session.scalar(
                select(PatrolContextBinding)
                .where(PatrolContextBinding.binding_id == revision.binding_id)
                .with_for_update()
            )
            attempt.status = "interrupted" if interrupted else "error"
            attempt.error_kind = kind
            attempt.error = message
            attempt.raw_response = deepcopy(raw_response or {})
            attempt.parsed_response = deepcopy(parsed_response or {})
            attempt.completed_at = datetime.now(timezone.utc)
            run_id = attempt.run_id
            run = await session.get(DesktopRun, attempt.run_id)
            if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
                run.status = "interrupted" if interrupted else "error"
                run.error = message
            revision.status = "interrupted" if interrupted else "error"
            revision.error = message
            revision.completed_at = datetime.now(timezone.utc)
            binding.last_error = message
            binding.health_state = "blocked" if kind == "capability" else "degraded"
            if (
                kind == "provider"
                and attempt.attempt_number <= len(_TRANSIENT_RETRY_DELAYS)
                and binding.control_state == "following"
                and binding.desired_checkpoint_id == revision.source_checkpoint_id
            ):
                revision.status = "observed"
                retry_delay = _TRANSIENT_RETRY_DELAYS[attempt.attempt_number - 1]
            await session.commit()
        record = self.run_manager.get(run_id) if run_id else None
        if record is not None and record.task is None:
            self.run_manager.update(
                run_id,
                status=RunStatus.interrupted if interrupted else RunStatus.error,
                error=message,
            )
            self.bridge.publish_end(run_id)
            self.bridge.cleanup(run_id, delay=300)
            self.run_manager._cleanup_later(run_id)
        if retry_delay is not None:
            asyncio.get_running_loop().call_later(retry_delay, self._event.set)

    async def _sync_run(self, record: RunRecord) -> None:
        async with self.session_factory() as session:
            run = await session.get(DesktopRun, record.run_id)
            if run is None:
                return
            run.status = record.status.value
            run.error = record.error
            run.prompt_input_tokens = record.prompt_input_tokens
            run.prompt_cache_hit_tokens = record.prompt_cache_hit_tokens
            await session.commit()

    async def _reconcile(self) -> None:
        async with self.session_factory() as session:
            attempts = list((await session.scalars(
                select(PatrolContextAttempt)
                .where(PatrolContextAttempt.status.in_(list(_ACTIVE_ATTEMPT_STATUSES)))
                .with_for_update()
            )).all())
            for attempt in attempts:
                attempt.status = "interrupted"
                attempt.error_kind = "restart"
                attempt.error = "桌面后端重启，策展运行已中断"
                attempt.completed_at = datetime.now(timezone.utc)
                run = await session.get(DesktopRun, attempt.run_id)
                if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
                    run.status = "interrupted"
                    run.error = attempt.error
                revision = await session.get(PatrolContextRevision, attempt.revision_id)
                if revision is not None and revision.status in {"preparing", "ready"}:
                    revision.status = "observed"
            bindings = list((await session.scalars(
                select(PatrolContextBinding).where(
                    PatrolContextBinding.control_state == "following"
                )
            )).all())
            roots = [binding.root_context_id for binding in bindings]
            for binding in bindings:
                if binding.health_state in {"preparing", "running"}:
                    binding.health_state = "idle"
            await session.commit()
        for root_context_id in dict.fromkeys(roots):
            try:
                await self.notify_stable_context_checkpoint(root_context_id)
            except Exception:
                logger.exception("Context 策展启动对账失败: root_context_id=%s", root_context_id)

    def _require_model_window(self, model_name: str | None, payload: dict[str, Any]) -> None:
        model = self._model_config(model_name)
        estimate = estimate_curation_tokens(payload)
        output_reserve = model.curation_max_output_tokens
        if model.context_window is not None and estimate + output_reserve > model.context_window:
            raise HTTPException(422, {
                "code": "context_window_exceeded",
                "estimate": estimate,
                "limit": model.context_window,
                "output_reserve": output_reserve,
            })

    def _model_config(self, model_name: str | None):
        if model_name:
            return self.app_config.get_model(model_name)
        try:
            return self.app_config.get_model(self.app_config.resolve_default_model_name())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @staticmethod
    def _source_snapshot_from_payload(payload: dict[str, Any]) -> CurationSourceSnapshot:
        return CurationSourceSnapshot.model_validate(payload["source_snapshot"])

    @staticmethod
    def _error_kind(exc: Exception) -> str:
        if isinstance(exc, CurationEngineError):
            return exc.kind
        if isinstance(exc, HTTPException):
            detail = exc.detail
            if isinstance(detail, dict) and detail.get("code") == "context_window_exceeded":
                return "window"
        return "projection"

    @staticmethod
    def _attempt_payload(attempt: PatrolContextAttempt) -> dict[str, Any]:
        return {
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt.attempt_number,
            "run_id": attempt.run_id,
            "model_name": attempt.model_name,
            "output_method": attempt.output_method,
            "status": attempt.status,
            "raw_response": deepcopy(attempt.raw_response),
            "parsed_response": deepcopy(attempt.parsed_response),
            "error_kind": attempt.error_kind,
            "error": attempt.error,
            "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
            "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
            "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
        }

    @classmethod
    def _revision_payload(
        cls, revision: PatrolContextRevision, attempts: list[PatrolContextAttempt]
    ) -> dict[str, Any]:
        return {
            "revision_id": revision.revision_id,
            "source_checkpoint_id": revision.source_checkpoint_id,
            "base_binding_revision": revision.base_binding_revision,
            "status": revision.status,
            "source_projection_hash": revision.source_projection_hash,
            "source_payload": deepcopy(revision.source_payload),
            "projection_status": revision.projection_status,
            "authored_messages": deepcopy(revision.authored_messages),
            "disposition_manifest": deepcopy(revision.disposition_manifest),
            "repair_manifest": deepcopy(revision.repair_manifest),
            "issues": deepcopy(revision.issues),
            "definition_hash": revision.definition_hash,
            "projection_hash": revision.projection_hash,
            "published_context_checkpoint_id": revision.published_context_checkpoint_id,
            "error": revision.error,
            "attempts": [cls._attempt_payload(item) for item in attempts],
            "created_at": revision.created_at.isoformat() if revision.created_at else None,
            "prepared_at": revision.prepared_at.isoformat() if revision.prepared_at else None,
            "completed_at": revision.completed_at.isoformat() if revision.completed_at else None,
            "published_at": revision.published_at.isoformat() if revision.published_at else None,
        }

    @classmethod
    def _binding_payload(
        cls,
        binding: PatrolContextBinding,
        root: DesktopThread | None,
        managed: DesktopThread | None,
        revisions: list[PatrolContextRevision],
        attempts: dict[str, list[PatrolContextAttempt]],
        latest_run: DesktopRun | None,
        *,
        has_more: bool,
    ) -> dict[str, Any]:
        revision_payloads = [
            cls._revision_payload(item, attempts.get(item.revision_id, []))
            for item in revisions
        ]
        return {
            "agent_id": binding.agent_id,
            "control_state": binding.control_state,
            "health_state": binding.health_state,
            "root_context": (
                {"context_id": root.task_id, "title": root.title} if root is not None else None
            ),
            "managed_context": (
                {"context_id": managed.task_id, "title": managed.title}
                if managed is not None else None
            ),
            "observed_checkpoint_id": binding.observed_checkpoint_id,
            "desired_checkpoint_id": binding.desired_checkpoint_id,
            "prepared_checkpoint_id": binding.prepared_checkpoint_id,
            "published_checkpoint_id": binding.published_checkpoint_id,
            "binding_revision": binding.revision,
            "last_error": binding.last_error,
            "latest_run": (
                {"run_id": latest_run.run_id, "status": latest_run.status, "error": latest_run.error}
                if latest_run is not None else None
            ),
            "latest_revision": revision_payloads[0] if revision_payloads else None,
            "revisions": revision_payloads,
            "next_cursor": revisions[-1].revision_id if has_more and revisions else None,
        }
