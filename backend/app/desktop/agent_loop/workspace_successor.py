"""本文件对外提供 create_workspace_successor 的显式工作区重新授权端口。

输入为已锁工作区、终态 predecessor 及原有 Context/Progress/Mission/grant；输出为有独立身份的后继 Loop。
具体工作流为核对真实执行结算与记忆就绪，复用 ownership 接管既有 Context，继承有限授权和预算，
以既有 Portfolio/P0 初始化端口登记当前已发布来源；旧 Loop、Run、用户输入和历史均不改写。
示例：await create_workspace_successor(session, workspace, prior)。
有限旧授权内的 active/retained 隔离 Slot 只转移当前 Loop/Lane 所有权并追加审计，版本、指纹、保留期限和旧 Run/Anchor/Observation 不变。
"""

from datetime import UTC, datetime
from copy import deepcopy
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopBudgetUsage,
    LoopRound,
)
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_contract import LoopMissionContract
from backend.app.desktop.agent_loop.task_progress.repository import (
    TaskProgressRepository,
    ProgressNotReady,
)
from backend.app.desktop.agent_loop.task_progress.models import LoopProgressReceipt
from backend.app.desktop.agent_loop.task_progress.contracts import TaskProgressDocument
from backend.app.desktop.agent_loop.curation_ownership import (
    CurationOwnershipRepository,
    CurationOwnershipConflict,
)
from backend.app.desktop.agent_loop.initialization import publish_initial_portfolio
from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder
from backend.app.desktop.context_curation.models import (
    CurationProgram,
    CurationLane,
    CurationSourceSubscription,
)
from backend.app.desktop.context_evolution.repository import ContextRevisionRepository
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from focus.history import content_hash


async def create_workspace_successor(session, workspace, prior):
    grant = await _grant_for_restart(session, prior)
    members = tuple(
        await session.scalars(
            select(LoopContextMembership)
            .where(
                LoopContextMembership.loop_id == prior.loop_id,
                LoopContextMembership.status != "discarded",
            )
            .order_by(LoopContextMembership.created_at)
        )
    )
    ownership = CurationOwnershipRepository()
    predecessors = {}
    try:
        for member in sorted(members, key=lambda item: item.context_id):
            predecessors[member.context_id] = await ownership.prepare_successor(
                session, member.context_id, reason="workspace_patrol_restart"
            )
    except CurationOwnershipConflict as exc:
        raise HTTPException(409, exc.detail()) from exc
    identity = uuid.uuid4().hex
    program = CurationProgram(
        program_id=uuid.uuid4().hex,
        workspace_id=workspace.workspace_id,
        policy={"owner_loop_id": identity},
        revision=1,
    )
    session.add(program)
    await session.flush()
    loop = AgentLoop(
        loop_id=identity,
        interaction_mode="workspace_patrol",
        workspace_id=workspace.workspace_id,
        initial_context_id=prior.initial_context_id,
        program_id=program.program_id,
        holder_id=f"patrol:{identity}",
        status="running",
        health="observing",
        goal_revision=prior.goal_revision,
        equipment={**prior.equipment, "predecessor_loop_id": prior.loop_id},
    )
    session.add(loop)
    await session.flush()
    pairs = await _copy_members(session, loop, members, predecessors)
    await _transfer_slots(session, loop, prior)
    session.add_all(
        [
            LoopDelegationGrant(
                grant_id=uuid.uuid4().hex,
                loop_id=identity,
                revision=1,
                holder_id=loop.holder_id,
                capabilities=deepcopy(grant.capabilities),
                context_scope=deepcopy(grant.context_scope),
                permission_scope=deepcopy(grant.permission_scope),
                budgets=deepcopy(grant.budgets),
                delegable_gates=deepcopy(grant.delegable_gates),
                compression_policy=deepcopy(grant.compression_policy),
                expires_at=grant.expires_at,
            ),
            LoopBudgetUsage(
                loop_id=identity, rounds=0, model_calls=0, input_tokens=0, output_tokens=0
            ),
        ]
    )
    await _inherit_task_state(session, loop, prior)
    if pairs:
        await publish_initial_portfolio(session, loop, *pairs[0], additional=tuple(pairs[1:]))
    slot = await session.scalar(
        select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == workspace.workspace_id,
            WorkspaceSlot.kind == "authoritative",
            WorkspaceSlot.lifecycle != "deleted",
        )
    )
    round_row = LoopRound(
        round_id=uuid.uuid4().hex,
        loop_id=identity,
        number=1,
        status="observed",
        authority_revision=1,
        goal_revision=loop.goal_revision,
        frontier_hash=content_hash([]),
        workspace_revision=slot.revision,
    )
    session.add(round_row)
    loop.current_round_id = round_row.round_id
    await session.flush()
    await LoopLifecycleEventRecorder().record(session, loop)
    return loop


async def _grant_for_restart(session, prior):
    active = await session.scalar(
        select(DesktopRun.run_id)
        .where(
            DesktopRun.loop_id == prior.loop_id,
            (DesktopRun.status.in_(("pending", "running"))) | (DesktopRun.settled_at.is_(None)),
        )
        .limit(1)
    )
    if active:
        raise HTTPException(409, "前序执行尚未安全结算，请等待结算后重新启用")
    progress = TaskProgressRepository()
    try:
        await progress.require_ready(session, prior.loop_id)
    except ProgressNotReady as exc:
        raise HTTPException(409, str(exc)) from exc
    grant = await session.scalar(
        select(LoopDelegationGrant)
        .where(LoopDelegationGrant.loop_id == prior.loop_id)
        .order_by(LoopDelegationGrant.revision.desc())
        .limit(1)
    )
    if grant is None or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)):
        raise HTTPException(409, "原授权缺失或过期，需要先明确新的授权")
    return grant


async def _copy_members(session, loop, members, predecessors):
    ownership = CurationOwnershipRepository()
    identity, program = loop.loop_id, await session.get(CurationProgram, loop.program_id)
    pairs = []
    for position, member in enumerate(members):
        old = await session.get(CurationLane, member.lane_id)
        lane = CurationLane(
            lane_id=uuid.uuid4().hex,
            program_id=program.program_id,
            managed_context_id=member.context_id,
            purpose=old.purpose,
            normalized_purpose=old.normalized_purpose,
            lane_policy=deepcopy(old.lane_policy),
        )
        session.add(lane)
        await session.flush()
        await ownership.record_acquisition(
            session,
            loop,
            lane,
            predecessor=predecessors[member.context_id],
            reason="workspace_patrol_restart",
        )
        session.add_all(
            [
                LoopContextMembership(
                    membership_id=uuid.uuid4().hex,
                    loop_id=identity,
                    context_id=member.context_id,
                    lane_id=lane.lane_id,
                    role=member.role,
                    status=member.status,
                    required_barrier=member.required_barrier,
                ),
                CurationSourceSubscription(
                    subscription_id=uuid.uuid4().hex,
                    program_id=program.program_id,
                    source_context_id=member.context_id,
                    source_role=member.role,
                    selection_policy={},
                    position=position,
                ),
            ]
        )
        revision = await ContextRevisionRepository().current(session, member.context_id)
        if revision is not None:
            pairs.append((lane, revision))
    return pairs


async def _inherit_task_state(session, loop, prior):
    progress = TaskProgressRepository()
    mission = await session.scalar(
        select(LoopMissionRevision).where(
            LoopMissionRevision.loop_id == prior.loop_id,
            LoopMissionRevision.revision == prior.goal_revision,
        )
    )
    current = await progress.current(session, prior.loop_id)
    keys = tuple(
        await session.scalars(
            select(LoopProgressReceipt.source_key).where(
                LoopProgressReceipt.loop_id == prior.loop_id
            )
        )
    )
    from backend.app.desktop.agent_loop.initialization import initialize_task_state

    await initialize_task_state(
        session,
        loop,
        LoopMissionContract(
            outcome=mission.outcome,
            boundaries=mission.boundaries,
            completion_checks=mission.completion_checks,
        ),
        document=TaskProgressDocument.model_validate(current.document),
        input_sources=deepcopy(mission.input_sources),
        source_keys=keys,
    )


async def _transfer_slots(session, loop, prior):
    from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
    from backend.app.desktop.agent_loop.event_journal import LoopEventJournal

    old_members = tuple(
        await session.scalars(
            select(LoopContextMembership).where(LoopContextMembership.loop_id == prior.loop_id)
        )
    )
    new_members = tuple(
        await session.scalars(
            select(LoopContextMembership).where(LoopContextMembership.loop_id == loop.loop_id)
        )
    )
    new_lanes = {member.context_id: member.lane_id for member in new_members}
    mapping = {
        member.lane_id: new_lanes[member.context_id]
        for member in old_members
        if member.context_id in new_lanes
    }
    slots = tuple(
        await session.scalars(
            select(WorkspaceSlot)
            .where(
                WorkspaceSlot.owner_loop_id == prior.loop_id,
                WorkspaceSlot.workspace_id == loop.workspace_id,
                WorkspaceSlot.kind == "isolated",
                WorkspaceSlot.owner_lane_id.in_(mapping),
                WorkspaceSlot.lifecycle.in_(("active", "retained")),
            )
            .with_for_update()
        )
    )
    for slot in slots:
        slot.owner_loop_id = loop.loop_id
        slot.owner_lane_id = mapping[slot.owner_lane_id]
        await LoopEventJournal().append(
            session,
            loop.loop_id,
            CanonicalEventDraft(
                kind="workspace.ownership.transferred",
                entity_type="loop_activity",
                entity_id=slot.slot_id,
                entity_revision=1,
                idempotency_key=f"workspace-slot:{slot.slot_id}:successor:{loop.loop_id}",
                payload={
                    "slot_id": slot.slot_id,
                    "predecessor_loop_id": prior.loop_id,
                    "successor_loop_id": loop.loop_id,
                },
            ),
        )
