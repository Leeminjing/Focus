"""Canonical Workspace Patrol state for focused kernel and runtime tests.

These fixtures seed an already prepared workspace and reuse the production Mission,
ownership and Portfolio repositories. They do not expose or emulate a Loop start API.
Workspace intake/bootstrap itself is covered by its dedicated integration tests.
"""

from datetime import UTC, datetime
import hashlib
import uuid

from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDelegationGrant,
    LoopEventOutbox, LoopGoalRevision, LoopRound,
)
from backend.app.desktop.patrol.agent_loop.schemas import LoopBudgetContract
from backend.app.desktop.patrol.agent_loop.service import AgentLoopService
from backend.app.desktop.patrol.agent_loop.missions.mission_contract import (
    LegacyMissionAdapter, LoopMissionContract, CompletionCheckDefinition, ExecutionBoundaries,
)
from backend.app.desktop.patrol.agent_loop.lifecycle.initialization import initialize_task_state, publish_initial_portfolio
from backend.app.desktop.patrol.agent_loop.curation.curation_ownership import CurationOwnershipRepository
from backend.app.desktop.contexts.curation.models import CurationLane, CurationProgram, CurationSourceSubscription
from backend.app.desktop.contexts.evolution import (
    ContextRevisionRepository, ContextRevisionRef, ContextRevisionPayloadMode,
    ContextRevisionContract, ContextRevisionProjectionStatus, ContextRevisionOriginKind,
)
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.capabilities.equipment import activation_equipment
from backend.app.desktop.workspaces.coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspaces.coordination.models import WorkspaceSlot
from pathlib import Path


async def seed_workspace_patrol_loop(
    sessions, *, workspace_id, initial_context_id, loop_id, holder_id,
    initial_run_id=None, mission=None, goal=None, task_contract=None,
    acceptance_criteria=None, capabilities=("continue_context", "request_completion"),
    context_scope=None, permission_scope=("read",), delegable_gates=(),
    equipment=None, budgets=None, compression_policy=None,
    inherit_initial_equipment=False, expires_at=None,
):
    """Seed prepared Workspace Patrol state around a test's existing Context."""
    contract = mission or (LegacyMissionAdapter.convert(
        goal=goal or "", task_contract=task_contract or "",
        acceptance_criteria=acceptance_criteria or (),
    ) if goal is not None else LoopMissionContract())
    legacy = LegacyMissionAdapter.export(contract)
    budget = budgets if isinstance(budgets, LoopBudgetContract) else LoopBudgetContract.model_validate(budgets or {})
    async with sessions.begin() as session:
        workspace = await session.get(DesktopWorkspace, workspace_id)
        context = await session.get(DesktopThread, initial_context_id)
        initial = await session.get(DesktopRun, initial_run_id) if initial_run_id else None
        resolved_equipment = activation_equipment(
            initial.equipment or {} if initial else {}, equipment or {}, permission_scope,
            inherit=inherit_initial_equipment,
        )
        resolved_equipment.setdefault("access_mode", "workspace-write" if "write" in permission_scope else "read-only")
        program = CurationProgram(program_id=uuid.uuid4().hex, workspace_id=workspace_id,
                                  policy={"owner_loop_id": loop_id}, revision=1)
        session.add(program)
        await session.flush()
        loop = AgentLoop(loop_id=loop_id, workspace_id=workspace_id,
            initial_context_id=initial_context_id, program_id=program.program_id,
            holder_id=holder_id, interaction_mode="workspace_patrol",
            status="running", health="observing", equipment=resolved_equipment)
        session.add(loop)
        await session.flush()
        goal_row = LoopGoalRevision(goal_revision_id=uuid.uuid4().hex, loop_id=loop_id,
            revision=1, goal=legacy["goal"], task_contract=legacy["task_contract"],
            acceptance_criteria=legacy["acceptance_criteria"], authored_by="user")
        session.add(goal_row)
        await session.flush()
        await initialize_task_state(session, loop, contract, legacy_goal_revision_id=goal_row.goal_revision_id)
        lane = CurationLane(lane_id=uuid.uuid4().hex, program_id=program.program_id,
            managed_context_id=initial_context_id, purpose="Primary execution",
            normalized_purpose="primary execution", lane_policy={})
        session.add(lane)
        await session.flush()
        await CurationOwnershipRepository().record_acquisition(
            session, loop, lane, predecessor=None, reason="workspace_patrol_test_fixture")
        policy = compression_policy.model_dump(mode="json") if hasattr(compression_policy, "model_dump") else compression_policy or {}
        session.add_all([
            LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=1,
                holder_id=holder_id, capabilities=list(capabilities),
                context_scope=list(context_scope or (initial_context_id,)),
                permission_scope=list(permission_scope), budgets=budget.as_grant_budgets(),
                delegable_gates=list(delegable_gates), compression_policy=policy,
                expires_at=datetime.fromisoformat(expires_at) if expires_at else None),
            LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=loop_id,
                context_id=initial_context_id, lane_id=lane.lane_id, role="primary"),
            CurationSourceSubscription(subscription_id=uuid.uuid4().hex, program_id=program.program_id,
                source_context_id=initial_context_id, source_role="initial", selection_policy={}, position=0),
            LoopBudgetUsage(loop_id=loop_id, rounds=0, model_calls=0, input_tokens=0, output_tokens=0),
        ])
        revision = await ContextRevisionRepository().get_by_id(session, context.current_revision_id)
        await publish_initial_portfolio(session, loop, lane, revision)
        slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == workspace_id,
            WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle != "deleted"))
        if slot is None:
            fingerprint = WorkspaceFingerprinter().capture(Path(workspace.path))
            slot = WorkspaceSlot(slot_id=uuid.uuid4().hex, workspace_id=workspace_id,
                kind="authoritative", root_path=workspace.path,
                provider="git" if fingerprint.vcs_revision else "local",
                base_revision=fingerprint.vcs_revision, current_fingerprint=fingerprint.digest, revision=1)
            session.add(slot)
        frontier = [{"context_id": initial_context_id, "revision_id": revision.ref.revision_id,
                     "checkpoint_id": revision.ref.checkpoint_id}]
        round_row = LoopRound(round_id=uuid.uuid4().hex, loop_id=loop_id, number=1,
            status="observed", authority_revision=1, goal_revision=1,
            frontier_hash=hashlib.sha256(repr(frontier).encode("utf-8")).hexdigest(), workspace_revision=slot.revision)
        session.add(round_row)
        loop.current_round_id = round_row.round_id
        if initial is not None:
            initial.loop_id = loop_id
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop_id,
            sequence=1, event_type="LoopStarted", payload={"round_id": round_row.round_id},
            idempotency_key=f"loop:{loop_id}:started"))
    return await AgentLoopService(sessions).get(loop_id)


async def advance_workspace_mission(sessions, loop_id, goal=None, task_contract=None,
                                    acceptance_criteria=None, *, mission=None):
    """Arrange a later Mission for tests of version fences and runtime convergence."""
    from backend.app.desktop.patrol.agent_loop.missions.mission_service import MissionRevisionService
    from backend.app.desktop.patrol.agent_loop.lifecycle.runtime_convergence import LoopRuntimeConvergence
    from backend.app.desktop.patrol.agent_loop.compression_authority.repository import CompressionAuthorityRepository
    from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import create_observation_round

    contract = mission or LegacyMissionAdapter.convert(goal=goal or "", task_contract=task_contract or "",
                                                       acceptance_criteria=acceptance_criteria or ())
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, loop_id, with_for_update=True)
        prior = await session.get(LoopRound, loop.current_round_id)
        loop.revision += 1
        loop.goal_revision += 1
        loop.status, loop.health = "running", "observing"
        await LoopRuntimeConvergence().converge(session, loop, "mission_revision")
        await CompressionAuthorityRepository().supersede(session, loop_id, "mission_revision")
        await MissionRevisionService().record(session, loop_id=loop_id,
            revision=loop.goal_revision, contract=contract, authored_by="user")
        round_row = await create_observation_round(session, loop, prior, prior.frontier_hash)
        session.add(round_row)
        loop.current_round_id = round_row.round_id
    return await AgentLoopService(sessions).get(loop_id)


async def _seed_greeting_loop(sessions, tmp_path: Path) -> dict:
    workspace_id = uuid.uuid4().hex
    context_id = uuid.uuid4().hex
    revision_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    vault = tmp_path / f"test-vault-{loop_id}"
    vault.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(vault), display_name="test Vault"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="greeting"))
        await session.flush()
        ref = ContextRevisionRef(
            context_id=context_id,
            revision_id=revision_id,
            generation=1,
            execution_thread_id=f"thread-{context_id}",
            checkpoint_ns="",
            checkpoint_id=f"checkpoint-{context_id}",
            payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        )
        revisions = ContextRevisionRepository()
        await revisions.insert(session, ContextRevisionContract(
            ref=ref,
            content_hash="a" * 64,
            projection_status=ContextRevisionProjectionStatus.VALID,
            origin_kind=ContextRevisionOriginKind.ROOT,
            created_at=datetime.now(UTC),
        ))
        await revisions.switch_current(session, ref, None)
        session.add(DesktopRun(
            run_id=run_id,
            task_id=context_id,
            agent_id=f"main:{context_id}",
            kind="main",
            status="success",
            origin="direct_user",
            execution_thread_id=f"thread-{context_id}",
            context_revision_id=revision_id,
            input_messages=[{"role": "user", "content": "你好"}],
            settled_at=datetime.now(UTC),
        ))
    mission = LoopMissionContract(
        outcome="交付可运行的 Obsidian 桌面宠物 Agent 插件",
        boundaries=ExecutionBoundaries(
            in_scope=("测试 Vault",),
            prohibited_actions=("修改真实 Vault",),
        ),
        completion_checks=(
            CompletionCheckDefinition(check_id="build", claim="生产构建通过", expected_evidence_kinds=("test",)),
            CompletionCheckDefinition(check_id="vault", claim="测试 Vault 工具通过", expected_evidence_kinds=("test",)),
        ),
    )
    service = AgentLoopService(sessions)
    snapshot = await seed_workspace_patrol_loop(sessions, loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=run_id, holder_id="patrol-test", mission=mission, capabilities=("continue_context", "request_completion", "wait_for_user"), context_scope=(context_id,), permission_scope=("read", "write"))
    return {"service": service, "snapshot": snapshot, "loop_id": loop_id, "context_id": context_id, "revision_id": revision_id, "run_id": run_id, "mission": mission}

class _FrozenCheckpointer:
    async def aget_tuple(self, config):
        from types import SimpleNamespace
        return SimpleNamespace(config=config, checkpoint={"channel_values": {"messages": []}}, metadata={})


def _script_first_patrol(orchestrator, sessions, fixture):
    from backend.app.desktop.patrol.agent_loop.schemas import PatrolDecisionIntent, ContextPermission
    from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import resolve_context_equipment
    from focus.runtime.runs.usage import ModelUsage

    class Model:
        usage = ModelUsage(model_calls=1, input_tokens=20, output_tokens=5)

        async def __call__(self, observation):
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, observation.round_id)
                permissions = tuple([
                    ContextPermission(context_id=item["context_id"],
                        access_mode=(await resolve_context_equipment(session, loop, item["context_id"]))["access_mode"])
                    for item in observation.portfolio_frontier
                ])
            return PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex, idempotency_key=f"scripted-patrol:{observation.round_id}",
                loop_id=loop.loop_id, loop_revision=loop.revision, round_id=round_row.round_id,
                observation_id=observation.observation_id or observation.decision_inputs_ref,
                observation_generation=observation.generation,
                context_permissions=permissions,
                holder_id=loop.holder_id, grant_id=observation.grant["grant_id"],
                grant_revision=loop.authority_revision, goal_revision=loop.goal_revision,
                observed_frontier_hash=round_row.frontier_hash,
                observed_workspace_revision=round_row.workspace_revision,
                observed_projection_sequence=observation.projection_sequence,
                base_entity_revisions=observation.base_entity_revisions,
                rationale="执行并验证所有必要检查",
                actions=({"action": "continue_context", "context_id": fixture["context_id"],
                          "context_revision_id": fixture["revision_id"], "message": "执行并验证所有必要检查"},),
            )

    orchestrator._decision_model = lambda _name: Model()
    orchestrator._curators.scopes = lambda _observation: ()
