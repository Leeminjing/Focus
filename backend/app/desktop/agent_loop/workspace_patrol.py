"""本文件对外提供 WorkspacePatrolService 和 WorkspacePatrolBootstrap。

输入为已有工作区、首条用户输入和共享 Desktop/Context 资源；输出为唯一工作区 Patrol、耐久回执及首个可运行 Revision。
工作区由原 Desktop 用例登记系统选择的本地目录；本文件不提供历史目录选择列表。
具体工作流为事务外准备内容与文件指纹，一致短事务冻结输入前局面、原文和同轮新版本；NO KEY UPDATE 工作区锁允许 bootstrap 的 FK KEY SHARE。
同身份重放先只读核验原载荷并返回耐久回执，不依赖当前 checkpoint；新输入在受理事务中再次核验，防止并发重复。
首条信息创建 Context/Program/Lane/grant/P0，由既有 Round 监督调用 shadow publisher
完成首 Revision/Portfolio；恢复使用原身份，不合成 Main Run。示例：await service.submit(workspace_id, input)。
工作区附件与原文在同一事务受理，图像能力在受理前核验；只有材料时保持空用户正文并使用中性 Context 标题。
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from focus.history import content_hash
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopRound,
    LoopUserIntent,
    LoopEventOutbox,
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
        from backend.app.desktop.agent_loop.observation_capture import LoopObservationService

        self._observations = LoopObservationService(sessions, getattr(desktop, "checkpointer", None))

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
            workspace = await session.scalar(select(DesktopWorkspace).where(
                DesktopWorkspace.workspace_id == workspace_id).with_for_update(key_share=True))
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
        from backend.app.desktop.agent_loop.observation_capture import _PreparedViewsChanged
        from backend.app.desktop.agent_loop.cognition import notify_preemption

        for attempt in range(3):
            async with self._sessions() as session:
                workspace = await session.get(DesktopWorkspace, workspace_id)
                if workspace is None or not Path(workspace.path).is_dir():
                    raise HTTPException(422, "请先绑定有效工作区")
                replay = await self._inputs.replay(session, workspace_id, request)
                if replay is not None:
                    return replay
                existing = await self._current(session, workspace_id)
                loop_id, workspace_path = existing.loop_id if existing else None, workspace.path
            views = await self._observations.prepare_views(loop_id) if loop_id else {}
            fingerprint = await asyncio.to_thread(WorkspaceFingerprinter().capture, Path(workspace_path)) if loop_id is None else None
            try:
                accepted, preemption = await self._submit_once(workspace_id, request, views, fingerprint)
                if preemption is not None:
                    await notify_preemption(preemption)
                return accepted
            except _PreparedViewsChanged:
                if attempt == 2:
                    raise
            except DBAPIError as exc:
                state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
                original = getattr(exc.orig, "__cause__", None)
                constraint = getattr(original, "constraint_name", None)
                identity_race = state == "23505" and (constraint == "uq_workspace_patrol_active"
                    or getattr(original, "table_name", None) == "loop_user_intents")
                if (state not in {"40001", "40P01"} and not identity_race) or attempt == 2:
                    raise
            await asyncio.sleep(0.02 * (attempt + 1))
        raise RuntimeError("用户输入受理重试耗尽")

    async def _submit_once(self, workspace_id, request, views, fingerprint):
        async with self._sessions.begin() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            workspace = await session.scalar(select(DesktopWorkspace).where(
                DesktopWorkspace.workspace_id == workspace_id).with_for_update(key_share=True))
            if workspace is None or not Path(workspace.path).is_dir():
                raise HTTPException(422, "请先绑定有效工作区")
            from backend.app.desktop.material_ownership import lock_workspace_submission

            await lock_workspace_submission(session, request.submission_id)
            replay = await self._inputs.replay(session, workspace_id, request)
            if replay is not None:
                return replay, None
            loop = await self._current(session, workspace_id, lock=True)
            if loop is None:
                if request.request_id:
                    raise HTTPException(409, "工作区尚无可回答的补充请求")
                loop = await self._create(session, workspace, request, fingerprint)
            round_row = await session.scalar(select(LoopRound).where(
                LoopRound.loop_id == loop.loop_id, LoopRound.decision_id.is_(None),
                LoopRound.status.in_(("observed", "curated", "waiting_workers"))
            ).with_for_update().execution_options(populate_existing=True))
            if round_row is None:
                prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
                round_row = await create_observation_round(session, loop, prior, content_hash([]))
                session.add(round_row)
                await session.flush()
            envelope = await self._observations.prepare_state(session, loop, round_row, views,
                visible_context=request.visible_context, intake=True)
            await self._inputs.accept(session, loop, request)
            intent = await session.get(LoopUserIntent, input_identity(request.submission_id))
            attachments = intent.request_payload.get("attachments") or []
            if any(item["material_kind"] == "image" for item in attachments):
                config = self._desktop.app_config.get_model(loop.equipment.get("model_name") or self._desktop.app_config.resolve_default_model_name())
                if not config.supports_image_input:
                    raise HTTPException(422, f"当前 Patrol 模型 {config.name} 未声明支持图像输入")
            round_row.trigger_kind, round_row.status = "user_input", "observed"
            round_row.authority_revision, round_row.goal_revision = loop.authority_revision, loop.goal_revision
            loop.current_round_id = round_row.round_id
            observation = await self._observations.freeze_version(session, loop, round_row, envelope, intent=intent)
            if loop.status == "waiting_user" and loop.waiting_reason == "awaiting_input":
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
                if grant is not None and (grant.expires_at is None or grant.expires_at > datetime.now(UTC)):
                    loop.status, loop.health, loop.waiting_reason = "running", "deciding", None
            await LoopLifecycleEventRecorder().record(session, loop)
            preemption = await session.scalar(select(LoopEventOutbox.payload).where(
                LoopEventOutbox.idempotency_key == f"cognition-preempted:{observation.observation_id}"))
            return self._inputs.view(intent), preemption

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

    async def _create(self, session, workspace, request, fingerprint):
        identity = uuid.uuid4().hex
        context = DesktopThread(
            task_id=uuid.uuid4().hex,
            workspace_id=workspace.workspace_id,
            thread_id=uuid.uuid4().hex,
            title=bounded_thread_title(request.content or "参考材料"),
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
            if fingerprint is None:
                from backend.app.desktop.agent_loop.observation_capture import _PreparedViewsChanged

                raise _PreparedViewsChanged("工作区初始化状态变化，需要重新准备")
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
