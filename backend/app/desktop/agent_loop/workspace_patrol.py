"""本文件对外提供 WorkspacePatrolService 和 WorkspacePatrolBootstrap。

输入为已有工作区、首条用户输入和共享 Desktop/Context 资源；输出为唯一工作区 Patrol、耐久回执及首个可运行 Revision。
具体工作流为短事务创建首线程身份、Program/Lane/grant/P0 和输入，再由既有 Round 监督调用 shadow publisher
完成首 Revision/Portfolio；恢复使用原身份，不合成 Main Run。示例：await service.submit(workspace_id, input)。
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select

from focus.history import content_hash
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopRound,
)
from backend.app.desktop.agent_loop.mission_contract import LoopMissionContract
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRepository, input_identity
from backend.app.desktop.agent_loop.schemas import LoopBudgetContract
from backend.app.desktop.agent_loop.rounds import create_observation_round
from backend.app.desktop.agent_loop.curation_ownership import CurationOwnershipRepository
from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder
from backend.app.desktop.context_curation.models import (
    CurationProgram,
    CurationLane,
    CurationSourceSubscription,
)
from backend.app.desktop.context_evolution.schemas import ContextRevisionOriginKind
from backend.app.desktop.models import DesktopWorkspace, DesktopThread, bounded_thread_title
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter


class WorkspacePatrolService:
    def __init__(self, sessions, desktop):
        self._sessions = sessions
        self._desktop = desktop
        self._inputs = PatrolInputRepository()

    async def workspaces(self):
        async with self._sessions() as session:
            rows = await session.scalars(
                select(DesktopWorkspace).order_by(DesktopWorkspace.display_name)
            )
            return [
                {
                    "workspace_id": row.workspace_id,
                    "display_name": row.display_name,
                    "path": row.path,
                }
                for row in rows
            ]

    async def current(self, workspace_id):
        async with self._sessions() as session:
            loop = await self._current(session, workspace_id)
            return (
                None
                if loop is None
                else {
                    "loop_id": loop.loop_id,
                    "status": loop.status,
                    "workspace_id": workspace_id,
                    "interaction_mode": loop.interaction_mode,
                }
            )

    async def restart(self, workspace_id):
        from backend.app.desktop.agent_loop.workspace_successor import create_workspace_successor

        async with self._sessions.begin() as session:
            workspace = await session.get(DesktopWorkspace, workspace_id, with_for_update=True)
            if workspace is None or not Path(workspace.path).is_dir():
                raise HTTPException(422, "请先绑定有效工作区")
            prior = await self._current(session, workspace_id, lock=True)
            if prior is None:
                raise HTTPException(409, "先发送首条信息即可启动")
            if prior.status not in {"completed", "stopped", "failed"}:
                return {"loop_id": prior.loop_id, "status": prior.status}
            loop = await create_workspace_successor(session, workspace, prior)
            return {"loop_id": loop.loop_id, "status": loop.status}

    async def submit(self, workspace_id, request):
        async with self._sessions.begin() as session:
            workspace = await session.get(DesktopWorkspace, workspace_id, with_for_update=True)
            if workspace is None or not Path(workspace.path).is_dir():
                raise HTTPException(422, "请先绑定有效工作区")
            key = int(input_identity(request.submission_id)[:15], 16)
            await session.execute(select(func.pg_advisory_xact_lock(key)))
            replay = await self._inputs.replay(session, workspace_id, request)
            if replay is not None:
                return replay
            loop = await self._current(session, workspace_id, lock=True)
            if loop is None:
                if request.request_id:
                    raise HTTPException(409, "工作区尚无可回答的补充请求")
                loop = await self._create(session, workspace, request)
            accepted = await self._inputs.accept(session, loop, request)
            if loop.status == "waiting_user" and loop.waiting_reason == "awaiting_input":
                grant = await session.scalar(
                    select(LoopDelegationGrant).where(
                        LoopDelegationGrant.loop_id == loop.loop_id,
                        LoopDelegationGrant.status == "active",
                    )
                )
                if grant is not None and (
                    grant.expires_at is None or grant.expires_at > datetime.now(UTC)
                ):
                    loop.status, loop.health, loop.waiting_reason = "running", "observing", None
                    prior = await session.get(LoopRound, loop.current_round_id)
                    successor = await create_observation_round(
                        session, loop, prior, content_hash([])
                    )
                    loop.current_round_id = successor.round_id
                    session.add(successor)
                    await LoopLifecycleEventRecorder().record(session, loop)
            return accepted

    async def history(self, workspace_id, before=None, limit=50):
        async with self._sessions() as session:
            if await session.get(DesktopWorkspace, workspace_id) is None:
                raise HTTPException(404, "工作区不存在")
            return await self._inputs.history(session, workspace_id, before=before, limit=limit)

    async def lineage(self, loop_id):
        from backend.app.desktop.context_evolution.committed_lineage import CommittedLineageReader

        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                raise HTTPException(404, "Patrol 不存在")
            members = tuple(
                await session.scalars(
                    select(LoopContextMembership.context_id).where(
                        LoopContextMembership.loop_id == loop_id
                    )
                )
            )
            reader = CommittedLineageReader()
            roots = await reader.published_roots(session, members)
            return await reader.snapshot(session, roots, workspace_id=loop.workspace_id)

    @staticmethod
    async def _current(session, workspace_id, *, lock=False):
        query = (
            select(AgentLoop)
            .where(
                AgentLoop.workspace_id == workspace_id,
                AgentLoop.interaction_mode == "workspace_patrol",
            )
            .order_by(AgentLoop.created_at.desc(), AgentLoop.loop_id.desc())
            .limit(1)
        )
        return await session.scalar(query.with_for_update() if lock else query)

    async def _create(self, session, workspace, request):
        identity = uuid.uuid4().hex
        context = DesktopThread(
            task_id=uuid.uuid4().hex,
            workspace_id=workspace.workspace_id,
            thread_id=uuid.uuid4().hex,
            title=bounded_thread_title(request.content),
            ui_state={"access_mode": request.access_mode},
        )
        program = CurationProgram(
            program_id=uuid.uuid4().hex,
            workspace_id=workspace.workspace_id,
            policy={"owner_loop_id": identity},
            revision=1,
        )
        session.add_all([context, program])
        await session.flush()
        from backend.app.desktop.equipment_policy import activation_equipment

        permissions = (
            ["read"] if request.access_mode == "read-only" else ["read", "write", "host_command"]
        )
        equipment = activation_equipment(
            {},
            {
                "model_name": self._desktop.app_config.resolve_default_model_name(),
                "permissions": permissions,
                "access_mode": request.access_mode,
                "skills": [],
            },
            permissions,
            inherit=False,
        )
        equipment["bootstrap_input_id"] = input_identity(request.submission_id)
        loop = AgentLoop(
            loop_id=identity,
            interaction_mode="workspace_patrol",
            workspace_id=workspace.workspace_id,
            initial_context_id=context.task_id,
            program_id=program.program_id,
            holder_id=f"patrol:{identity}",
            status="running",
            health="preparing",
            revision=1,
            authority_revision=1,
            goal_revision=1,
            equipment=equipment,
        )
        session.add(loop)
        await session.flush()
        lane = CurationLane(
            lane_id=uuid.uuid4().hex,
            program_id=program.program_id,
            managed_context_id=context.task_id,
            purpose="首条工作线程",
            normalized_purpose="首条工作线程",
            lane_policy={},
        )
        session.add(lane)
        await session.flush()
        await CurationOwnershipRepository().record_acquisition(
            session, loop, lane, predecessor=None, reason="workspace_patrol_start"
        )
        capabilities = [
            "continue_context",
            "create_lane",
            "update_lane",
            "merge_contexts",
            "recover_context",
            "pause_lane",
            "discard_membership",
            "request_lane_curator",
            "request_completion_verifier",
            "request_completion",
            "wait_for_user",
            "stop_loop",
            "apply_user_inputs",
        ]
        if "write" in equipment["permissions"]:
            capabilities.append("adopt_workspace_result")
        session.add_all(
            [
                LoopDelegationGrant(
                    grant_id=uuid.uuid4().hex,
                    loop_id=identity,
                    revision=1,
                    holder_id=loop.holder_id,
                    capabilities=capabilities,
                    context_scope=[context.task_id],
                    permission_scope=equipment["permissions"],
                    budgets=LoopBudgetContract().as_grant_budgets(),
                    delegable_gates=[],
                ),
                LoopContextMembership(
                    membership_id=uuid.uuid4().hex,
                    loop_id=identity,
                    context_id=context.task_id,
                    lane_id=lane.lane_id,
                    role="primary",
                ),
                CurationSourceSubscription(
                    subscription_id=uuid.uuid4().hex,
                    program_id=program.program_id,
                    source_context_id=context.task_id,
                    source_role="initial",
                    selection_policy={},
                    position=0,
                ),
                LoopBudgetUsage(
                    loop_id=identity, rounds=0, model_calls=0, input_tokens=0, output_tokens=0
                ),
            ]
        )
        from backend.app.desktop.agent_loop.initialization import initialize_task_state

        await initialize_task_state(session, loop, LoopMissionContract())
        slot = await session.scalar(
            select(WorkspaceSlot).where(
                WorkspaceSlot.workspace_id == workspace.workspace_id,
                WorkspaceSlot.kind == "authoritative",
                WorkspaceSlot.lifecycle == "active",
            )
        )
        if slot is None:
            fingerprint = await asyncio.to_thread(
                WorkspaceFingerprinter().capture, Path(workspace.path)
            )
            slot = WorkspaceSlot(
                slot_id=uuid.uuid4().hex,
                workspace_id=workspace.workspace_id,
                kind="authoritative",
                root_path=workspace.path,
                provider="git" if fingerprint.vcs_revision else "local",
                base_revision=fingerprint.vcs_revision,
                current_fingerprint=fingerprint.digest,
                revision=1,
            )
            session.add(slot)
        round_row = LoopRound(
            round_id=uuid.uuid4().hex,
            loop_id=identity,
            number=1,
            status="observed",
            authority_revision=1,
            goal_revision=1,
            frontier_hash=content_hash([]),
            workspace_revision=slot.revision,
        )
        session.add(round_row)
        loop.current_round_id = round_row.round_id
        await session.flush()
        await LoopLifecycleEventRecorder().record(session, loop)
        return loop


class WorkspacePatrolBootstrap:
    def __init__(self, sessions, contexts):
        self._sessions = sessions
        self._contexts = contexts

    async def prepare(self, loop_id):
        from backend.app.desktop.agent_loop.models import LoopUserIntent
        from backend.app.desktop.agent_loop.initialization import publish_initial_portfolio

        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            if (
                loop is None
                or loop.interaction_mode != "workspace_patrol"
                or loop.status != "running"
            ):
                return
            context = await session.get(DesktopThread, loop.initial_context_id)
            if context.current_revision_id is not None:
                return
            source = await session.get(LoopUserIntent, loop.equipment["bootstrap_input_id"])
            revision = await self._contexts.stage_definition(
                session,
                context.task_id,
                (
                    {
                        "id": source.intent_id,
                        "role": "human",
                        "content": source.content,
                        "additional_kwargs": {
                            "focus_context": {
                                "origin": "direct_user",
                                "scope": "revision",
                                "kind": "message",
                                "source_refs": [
                                    {
                                        "kind": "patrol_input",
                                        "intent_id": source.intent_id,
                                        "input_type": source.request_payload["request"][
                                            "input_type"
                                        ],
                                    }
                                ],
                            }
                        },
                    },
                ),
                (),
                ContextRevisionOriginKind.ROOT,
                source.intent_id,
            )
            if not revision.ref.is_runnable:
                raise ValueError("首线程未形成可运行 Revision")
            lane = await session.scalar(
                select(CurationLane).where(CurationLane.program_id == loop.program_id)
            )
            await publish_initial_portfolio(session, loop, lane, revision)
            loop.health = "observing"
            await LoopLifecycleEventRecorder().record(session, loop)
