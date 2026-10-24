"""ConversationService 的业务实现与显式依赖端口。"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import HTTPException
from focus.agents.commitment.middleware import commitment_subgraph_thread_id
from focus.agents.commitment.workflow import _human_payload
from focus.models.telemetry import observe_operation
from focus.runtime.runs.events import serialize_message
from focus.security.approval import APPROVAL_TYPE
from focus.security.policy import AccessMode
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from backend.app.desktop.capabilities.prompts import (
    MAIN_SYSTEM_PROMPT as _MAIN_SYSTEM_PROMPT,
)
from backend.app.desktop.conversations.compression import compression_recovery_payload
from backend.app.desktop.conversations.interrupts import main_pending_interrupt
from backend.app.desktop.conversations.recovery import _checkpoint_commitment_review
from backend.app.desktop.persistence.contracts import _MAIN_RUNTIME_EQUIPMENT_KEY
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)
from backend.app.desktop.runs.estimates import estimate_tokens
from backend.app.desktop.runs.identity import identity_from_main_run, latest_main_run
from backend.app.desktop.runs.must_view_recovery import must_view_recovery_payload
from backend.app.desktop.workspaces.assembly import (
    _ASSEMBLY_THREAD_ID,
    _is_assembly_thread,
)

logger = logging.getLogger(__name__)


class ConversationServicePorts(Protocol):
    async def _get_task_entities(
        self, session: AsyncSession, task_id: str
    ) -> tuple[DesktopThread, DesktopWorkspace]: ...

    @staticmethod
    def _run_payload(run: DesktopRun) -> dict[str, Any]: ...

    _task_attention_cache: Any

    checkpointer: Any
    contexts: Any
    conversation_inputs: Any
    session_factory: Any


class ConversationService:
    def __init__(self, ports: ConversationServicePorts) -> None:
        self._ports = ports

    @observe_operation("task_list")
    async def list_tasks(self) -> list[dict[str, Any]]:
        ports = self._ports
        async with ports.session_factory() as session:
            rows = (
                await session.execute(
                    select(DesktopThread, DesktopWorkspace)
                    .options(
                        load_only(
                            DesktopThread.task_id,
                            DesktopThread.workspace_id,
                            DesktopThread.thread_id,
                            DesktopThread.title,
                            DesktopThread.archived_at,
                            DesktopThread.deleted_at,
                        )
                    )
                    .join(
                        DesktopWorkspace,
                        DesktopThread.workspace_id == DesktopWorkspace.workspace_id,
                    )
                    .order_by(DesktopThread.created_at)
                )
            ).all()
            if not rows:
                return []
            ranked_runs = (
                select(
                    DesktopRun.run_id,
                    func.row_number()
                    .over(
                        partition_by=(
                            DesktopRun.task_id,
                            DesktopRun.status.in_(["pending", "running"]),
                        ),
                        order_by=(
                            DesktopRun.created_at.desc(),
                            DesktopRun.run_id.desc(),
                        ),
                    )
                    .label("position"),
                )
                .where(DesktopRun.agent_id == "main:" + DesktopRun.task_id)
                .subquery()
            )
            runs = (
                await session.execute(
                    select(
                        DesktopRun,
                        DesktopRun.workspace_result["task_attention"].label(
                            "attention"
                        ),
                    )
                    .join(ranked_runs, DesktopRun.run_id == ranked_runs.c.run_id)
                    .where(ranked_runs.c.position == 1)
                    .options(
                        load_only(
                            DesktopRun.run_id,
                            DesktopRun.task_id,
                            DesktopRun.agent_id,
                            DesktopRun.kind,
                            DesktopRun.status,
                            DesktopRun.model_name,
                            DesktopRun.created_at,
                            DesktopRun.updated_at,
                            DesktopRun.settled_at,
                            DesktopRun.execution_thread_id,
                            DesktopRun.checkpoint_ns,
                        )
                    )
                    .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
                )
            ).all()
            active_runs, latest_runs, latest_results, attention_by_run = (
                {},
                {},
                {},
                {},
            )
            for run, attention in runs:
                latest_runs.setdefault(run.task_id, run)
                attention_by_run[run.run_id] = attention
                if run.status in {"pending", "running"}:
                    active_runs[run.task_id] = run
                else:
                    latest_results[run.task_id] = run
            summaries = []
            for task, workspace in rows:
                latest = latest_runs.get(task.task_id)
                summary = self._task_summary(
                    task,
                    workspace,
                    active_runs.get(task.task_id),
                    latest,
                    latest_results.get(task.task_id),
                )
                if latest is not None and summary["active_run"] is None:
                    try:
                        summary["attention"] = await self._task_attention(
                            latest, task, attention_by_run.get(latest.run_id)
                        )
                    except Exception:
                        logger.warning(
                            "任务待处理摘要读取失败: %s", task.task_id, exc_info=True
                        )
                        summary["attention_unavailable"] = True
                summaries.append(summary)
            await self._add_loop_task_summaries(session, summaries)
            return summaries

    async def _task_attention(self, run, task, persisted=None):
        ports = self._ports
        if run.status != "interrupted":
            return []
        if isinstance(persisted, list):
            return [dict(item) for item in persisted]
        from backend.app.desktop.conversations.attention import read_task_attention

        cache = getattr(ports, "_task_attention_cache", None)
        if cache is None:
            ports._task_attention_cache = cache = {}
        key = (run.run_id, run.updated_at)
        if key not in cache:
            if len(cache) > 1000:
                cache.clear()
            cache[key] = await read_task_attention(ports.checkpointer, run, task)
        return [dict(item) for item in cache[key]]

    async def _add_loop_task_summaries(self, session, summaries):
        from backend.app.desktop.patrol.agent_loop.models import (
            AgentLoop,
            LoopContextMembership,
            LoopPendingDecision,
        )
        from backend.app.desktop.patrol.agent_loop.waits.wait_models import (
            LoopWaitRequest,
        )

        associations = (
            select(AgentLoop.loop_id, AgentLoop.initial_context_id.label("task_id"))
            .union(
                select(
                    AgentLoop.loop_id, LoopContextMembership.context_id.label("task_id")
                )
                .join(
                    LoopContextMembership,
                    LoopContextMembership.loop_id == AgentLoop.loop_id,
                )
                .where(AgentLoop.interaction_mode == "workspace_patrol")
            )
            .subquery()
        )
        ranked = (
            select(
                AgentLoop.loop_id,
                func.row_number()
                .over(
                    partition_by=associations.c.task_id,
                    order_by=(AgentLoop.created_at.desc(), AgentLoop.loop_id.desc()),
                )
                .label("position"),
                associations.c.task_id,
            )
            .join(associations, associations.c.loop_id == AgentLoop.loop_id)
            .subquery()
        )
        rows = (
            await session.execute(
                select(
                    ranked.c.task_id,
                    AgentLoop.loop_id,
                    AgentLoop.status,
                    AgentLoop.updated_at,
                    AgentLoop.interaction_mode,
                    AgentLoop.workspace_id,
                    AgentLoop.initial_context_id,
                    AgentLoop.final_result["verification"]["verification_id"].astext,
                    AgentLoop.final_result["verification"]["conclusion"].astext,
                    LoopPendingDecision.pending_decision_id,
                    LoopPendingDecision.kind,
                    LoopWaitRequest.request_id,
                    LoopWaitRequest.kind,
                )
                .join(ranked, AgentLoop.loop_id == ranked.c.loop_id)
                .outerjoin(
                    LoopPendingDecision,
                    (LoopPendingDecision.loop_id == AgentLoop.loop_id)
                    & (LoopPendingDecision.status == "pending"),
                )
                .outerjoin(
                    LoopWaitRequest,
                    (LoopWaitRequest.loop_id == AgentLoop.loop_id)
                    & (LoopWaitRequest.status == "open"),
                )
                .where(ranked.c.position == 1)
            )
        ).all()
        by_task = {item["task_id"]: item for item in summaries}
        for (
            task_id,
            loop_id,
            status,
            updated_at,
            mode,
            workspace_id,
            initial_context_id,
            verification_id,
            conclusion,
            decision_id,
            decision_kind,
            request_id,
            request_kind,
        ) in rows:
            summary = by_task.get(task_id)
            if summary is None:
                continue
            latest_created = (summary.get("latest_run") or {}).get("created_at")
            if (
                mode == "context_loop"
                and status in {"completed", "stopped", "failed"}
                and latest_created
                and (latest_created > updated_at.isoformat())
            ):
                continue
            summary["loop"] = {"loop_id": loop_id, "status": status}
            if mode == "workspace_patrol":
                summary["loop"].update(
                    interaction_mode=mode,
                    workspace_id=workspace_id,
                    initial_context_id=initial_context_id,
                )
                if verification_id and conclusion == "satisfied":
                    summary["loop"]["delivery"] = {
                        "verification_id": verification_id,
                        "conclusion": conclusion,
                    }
                if task_id != initial_context_id:
                    continue
            if status not in {
                "running",
                "waiting_user",
                "paused",
                "pausing",
                "completing",
            }:
                continue
            for identity, kind in (
                (decision_id, decision_kind),
                (request_id, request_kind),
            ):
                if identity and (
                    not any((item["id"] == identity for item in summary["attention"]))
                ):
                    if identity == decision_id:
                        summary["attention"] = [
                            item
                            for item in summary["attention"]
                            if item["kind"] != kind or item.get("loop_id")
                        ]
                    summary["attention"].append(
                        {"id": identity, "kind": kind, "loop_id": loop_id}
                    )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        ports = self._ports
        async with ports.session_factory() as session:
            task, workspace = await ports._get_task_entities(session, task_id)
            payload = await self._task_payload(session, task, workspace)
        snapshot = await ports.contexts.live_conversation(task_id)
        payload["messages"] = snapshot["messages"]
        payload["context"] = await ports.contexts.get(task_id)
        payload["context_usage"] = {
            "estimated_tokens": estimate_tokens(
                _MAIN_SYSTEM_PROMPT, snapshot["messages"], ""
            ),
            "basis": "execution_messages_and_base_prompt",
        }
        return payload

    async def _memory_context_messages(self, context_id: str) -> list[dict[str, Any]]:
        ports = self._ports
        snapshot = await ports.contexts.snapshot(context_id)
        return snapshot["messages"]

    async def save_ui_state(self, task_id: str, ui_state: dict[str, Any]) -> None:
        ports = self._ports
        async with ports.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread)
                .where(DesktopThread.task_id == task_id)
                .with_for_update()
            )
            if not task:
                raise HTTPException(404, "任务不存在")
            persisted = dict(ui_state)
            from backend.app.desktop.browser.service import BINDING_KEY

            persisted.pop(BINDING_KEY, None)
            if BINDING_KEY in (task.ui_state or {}):
                persisted[BINDING_KEY] = task.ui_state[BINDING_KEY]
            persisted.pop("access_mode", None)
            standing_mode = (task.ui_state or {}).get("access_mode")
            if standing_mode is not None:
                persisted["access_mode"] = standing_mode
            runtime_equipment = (task.ui_state or {}).get(_MAIN_RUNTIME_EQUIPMENT_KEY)
            if runtime_equipment is not None:
                persisted[_MAIN_RUNTIME_EQUIPMENT_KEY] = runtime_equipment
            task.ui_state = persisted
            await session.commit()

    async def set_session_access_mode(self, task_id: str, access_mode: str) -> None:
        ports = self._ports
        mode = AccessMode(access_mode)
        async with ports.session_factory() as session:
            task = await session.scalar(
                select(DesktopThread)
                .where(DesktopThread.task_id == task_id)
                .with_for_update()
            )
            if task is None:
                raise HTTPException(404, "任务不存在")
            task.ui_state = {**(task.ui_state or {}), "access_mode": str(mode)}
            await session.commit()

    async def get_checkpoint_messages(
        self, thread_id: str, checkpoint_ns: str
    ) -> list[dict[str, Any]]:
        ports = self._ports
        config = {
            "configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}
        }
        checkpoint = await ports.checkpointer.aget_tuple(config)
        if checkpoint is None:
            return []
        values = checkpoint.checkpoint.get("channel_values", {})
        return [serialize_message(message) for message in values.get("messages", [])]

    async def _task_thread_id(self, task_id: str) -> str:
        ports = self._ports
        async with ports.session_factory() as session:
            task = await session.get(DesktopThread, task_id)
            if task is None:
                raise ValueError("任务不存在")
            return task.thread_id

    def _task_summary(
        self,
        task: DesktopThread,
        workspace: DesktopWorkspace,
        active: DesktopRun | None = None,
        latest: DesktopRun | None = None,
        result: DesktopRun | None = None,
    ) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "workspace_id": task.workspace_id,
            "workspace_path": workspace.path,
            "workspace_name": workspace.display_name,
            "thread_id": task.thread_id,
            "title": task.title,
            "harness_mode": "assembly"
            if _is_assembly_thread(task.thread_id)
            else "workspace",
            "assembly_target": {
                "scope": "global"
                if task.thread_id == _ASSEMBLY_THREAD_ID
                else "workspace",
                "workspace_id": None
                if task.thread_id == _ASSEMBLY_THREAD_ID
                else workspace.workspace_id,
                "path": workspace.path,
            }
            if _is_assembly_thread(task.thread_id)
            else None,
            "lifecycle": "deleted"
            if task.deleted_at is not None
            else "archived"
            if task.archived_at is not None
            else "active",
            "active_run": {
                key: getattr(active, key)
                for key in (
                    "run_id",
                    "task_id",
                    "agent_id",
                    "kind",
                    "status",
                    "model_name",
                )
            }
            if active
            else None,
            "latest_run": {
                "run_id": latest.run_id,
                "status": latest.status,
                "created_at": latest.created_at.isoformat()
                if latest.created_at
                else None,
                "settled_at": latest.settled_at.isoformat()
                if latest.settled_at
                else None,
            }
            if latest
            else None,
            "attention": [],
            "latest_result": {
                "run_id": result.run_id,
                "status": result.status,
                "settled_at": result.settled_at.isoformat()
                if result.settled_at
                else None,
            }
            if result
            else None,
        }

    async def _task_payload(
        self, session: AsyncSession, task: DesktopThread, workspace: DesktopWorkspace
    ) -> dict[str, Any]:
        ports = self._ports
        active = await session.scalar(
            select(DesktopRun)
            .where(
                DesktopRun.task_id == task.task_id,
                DesktopRun.agent_id == f"main:{task.task_id}",
                DesktopRun.status.in_(["pending", "running"]),
            )
            .order_by(DesktopRun.created_at.desc())
        )
        latest_direct_user = await session.scalar(
            select(DesktopRun)
            .where(
                DesktopRun.task_id == task.task_id,
                DesktopRun.agent_id == f"main:{task.task_id}",
                DesktopRun.origin == "direct_user",
            )
            .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
        )
        ui_state = dict(task.ui_state or {})
        ui_state.pop(_MAIN_RUNTIME_EQUIPMENT_KEY, None)
        recovery = await self._commitment_recovery_payload(session, task)
        compression_recovery = await compression_recovery_payload(
            session, task, ports.checkpointer
        )
        must_view_recovery = await must_view_recovery_payload(
            session, task, ports.checkpointer
        )
        access_recovery = await main_pending_interrupt(
            session, task, ports.checkpointer, APPROVAL_TYPE
        )
        latest = await latest_main_run(session, task)
        return {
            **self._task_summary(task, workspace, active, latest),
            "ui_state": ui_state,
            "active_run": ports._run_payload(active) if active else None,
            "latest_main_run": ports._run_payload(latest) if latest else None,
            "latest_direct_user_run": ports._run_payload(latest_direct_user)
            if latest_direct_user
            else None,
            "conversation_inputs": await ports.conversation_inputs.list(task.task_id),
            "pending_commitment_review": recovery["review"]
            if recovery and recovery["status"] == "resumable"
            else None,
            "commitment_recovery": recovery,
            "pending_compression": compression_recovery["request"]
            if compression_recovery
            and compression_recovery["status"] in ("resumable", "orphaned")
            else None,
            "compression_recovery": compression_recovery,
            "pending_must_view_report": must_view_recovery["request"]
            if must_view_recovery
            and must_view_recovery["status"] in ("resumable", "orphaned")
            else None,
            "must_view_recovery": must_view_recovery,
            "pending_access_review": access_recovery["request"]
            if access_recovery
            and access_recovery["status"] in ("resumable", "orphaned")
            else None,
            "access_recovery": access_recovery,
        }

    async def _commitment_recovery_payload(
        self, session: AsyncSession, task: DesktopThread
    ) -> dict[str, Any] | None:
        ports = self._ports
        latest = await latest_main_run(session, task)
        execution_identity = identity_from_main_run(task, latest)
        config = {
            "configurable": {
                "thread_id": commitment_subgraph_thread_id(execution_identity.thread_id)
            }
        }
        try:
            checkpoint = await ports.checkpointer.aget_tuple(config)
        except Exception:
            logger.warning(
                "读取承诺子图 checkpoint 失败: thread_id=%s",
                execution_identity.thread_id,
                exc_info=True,
            )
            return None
        if checkpoint is None:
            return None
        values = dict(checkpoint.checkpoint.get("channel_values", {}))
        try:
            stage = int(values.get("awaiting_human") or 0)
        except (TypeError, ValueError):
            return None
        if stage < 1 or stage > 9:
            return None
        if latest is not None and latest.status in {"pending", "running"}:
            status = "processing"
        elif latest is not None and latest.status == "interrupted":
            status = "resumable"
        else:
            status = "orphaned"
        source_text = str(values.get("source_text") or "").strip()
        review = _checkpoint_commitment_review(checkpoint, stage)
        return {
            "status": status,
            "stage": stage,
            "review": review or _human_payload(stage, values),
            "restart_message": f"/commit {source_text}" if source_text else "/commit ",
        }
