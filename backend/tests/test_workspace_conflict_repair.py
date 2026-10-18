"""真实 Git/PostgreSQL 的冲突分类、隔离修复及审查门禁回归。

输入为共享基线的两个隔离成果；输出为原成果保留、目标不被提前改写、新成果经审查采用及未知状态拒绝。
使用确定性文件 Worker 验证物理执行链，不将其作为真实模型语义判断证据。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, select

from backend.app.desktop.agent_loop import LoopKernel, PatrolDecisionIntent
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.dispatch import LoopRunWorkspaceBinder, LoopWaveDispatcher
from backend.app.desktop.agent_loop.directive_equipment import resolve_directive_equipment
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.agent_loop.models import (
    AgentLoop, LoopAction, LoopContextMembership, LoopDelegationGrant, LoopDirective, LoopObservation, LoopRound,
)
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.agent_loop.schemas import WorkspaceRepairSelection
from backend.app.desktop.agent_loop.workspace_adoption import LoopWorkspaceAdoptionService
from backend.app.desktop.agent_loop.workspace_repair import WorkspaceConflictRepair, WorkspaceRepairRejected
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.workspace_coordination import (
    GitWorkspaceResultApplier, WorkspaceAdopter, WorkspaceAdoptionRequest, WorkspaceFingerprinter, WorkspaceLeaseManager,
)
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceAdoption, WorkspaceSlot
from backend.tests.loop_run_boundary_support import freeze_patrol_intent
from backend.tests.test_git_workspace_adoption import _git, _repository
from backend.tests.test_loop_governance_convergence import _loop
from backend.tests.test_loop_workspace_adoption_governance import _seed_adoption


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _conflict(sessions, fixture, tmp_path, *, unknown_target=False):
    decision_id, pending = await _seed_adoption(sessions, fixture, adopted=False)
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"])
        loop.equipment = {**loop.equipment, "permissions": ["read", "write"], "access_mode": "workspace-write"}
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id))
        grant.capabilities = [*grant.capabilities, "isolate_workspace"]
        source = await session.get(WorkspaceSlot, pending.source_slot_id)
        target = await session.get(WorkspaceSlot, pending.target_slot_id)
        root = Path(target.root_path)
        baseline = _repository(root)
        _git(root, "worktree", "add", "--detach", source.root_path, baseline)
        first_root = tmp_path / "first"
        _git(root, "worktree", "add", "--detach", str(first_root), baseline)
        (first_root / "tracked.txt").write_text("feature A\n", encoding="utf-8")
        (Path(source.root_path) / "tracked.txt").write_text("feature B\n", encoding="utf-8")
        source.base_revision = target.base_revision = baseline
        source.current_fingerprint = WorkspaceFingerprinter().capture(Path(source.root_path)).digest
        target.current_fingerprint = WorkspaceFingerprinter().capture(root).digest
        first = WorkspaceSlot(slot_id=uuid.uuid4().hex, workspace_id=target.workspace_id, kind="isolated",
            root_path=str(first_root), provider="git", base_revision=baseline, revision=2,
            current_fingerprint=WorkspaceFingerprinter().capture(first_root).digest, owner_loop_id=loop.loop_id)
        session.add(first)
        directive, provenance = DelegatedDirectiveFactory.create(loop_id=loop.loop_id,
            round_id=fixture["round_id"], decision_id=decision_id, action_id=pending.adoption_id,
            context_id=fixture["context_id"], context_revision_id=fixture["revision_id"],
            content="Preserve feature B alongside feature A", actor_id=loop.holder_id,
            grant_id=grant.grant_id, grant_revision=1, goal_revision=1, idempotency_key=uuid.uuid4().hex)
        directive.status, directive.lifecycle_state = "launched", "settled"
        session.add_all([directive, provenance])
        await session.flush()
        run = DesktopRun(run_id=uuid.uuid4().hex, task_id=fixture["context_id"], agent_id="fixture-source",
            kind="main", status="success", origin="loop", loop_id=loop.loop_id,
            workspace_anchor={"slot_id": source.slot_id, "mode": "write"},
            round_id=fixture["round_id"], directive_id=directive.directive_id, settled_at=datetime.now(UTC))
        session.add(run)
        await session.flush()
        session.add(RunExecutionAnchor(run_id=run.run_id, slot_id=source.slot_id,
            directive_id=directive.directive_id, observed_workspace_revision=1, observed_fingerprint="a" * 64,
            resulting_workspace_revision=source.revision, resulting_fingerprint=source.current_fingerprint))
        target_directive, target_provenance = DelegatedDirectiveFactory.create(loop_id=loop.loop_id,
            round_id=fixture["round_id"], decision_id=decision_id, action_id=pending.adoption_id,
            context_id=fixture["context_id"], context_revision_id=fixture["revision_id"],
            content="Preserve feature A", actor_id=loop.holder_id, grant_id=grant.grant_id,
            grant_revision=1, goal_revision=1, idempotency_key=uuid.uuid4().hex)
        target_directive.status, target_directive.lifecycle_state = "launched", "settled"
        session.add_all([target_directive, target_provenance])
        await session.flush()
        target_run = DesktopRun(run_id=uuid.uuid4().hex, task_id=fixture["context_id"], agent_id="fixture-first",
            kind="main", status="success", origin="loop", loop_id=loop.loop_id,
            workspace_anchor={"slot_id": first.slot_id, "mode": "write"},
            round_id=fixture["round_id"], directive_id=target_directive.directive_id, settled_at=datetime.now(UTC))
        session.add(target_run)
        await session.flush()
        session.add(RunExecutionAnchor(run_id=target_run.run_id, slot_id=first.slot_id,
            directive_id=target_directive.directive_id, observed_workspace_revision=1, observed_fingerprint="a" * 64,
            resulting_workspace_revision=first.revision, resulting_fingerprint=first.current_fingerprint,
            adoption_state="adopted"))
        await session.execute(delete(WorkspaceAdoption).where(WorkspaceAdoption.adoption_id == pending.adoption_id))
    adopted = await WorkspaceAdopter(sessions).adopt(WorkspaceAdoptionRequest(
        adoption_id=uuid.uuid4().hex, source_slot_id=first.slot_id, target_slot_id=target.slot_id,
        source_revision=2, expected_target_revision=1, evidence={"patrol_rationale": "Preserve feature A"}),
        GitWorkspaceResultApplier())
    assert adopted.status == "adopted", adopted.conflict
    async with sessions.begin() as session:
        current = await session.get(LoopRound, fixture["round_id"])
        current.workspace_revision = 2
        if unknown_target:
            (root / "tracked.txt").write_text("user private edit\n", encoding="utf-8")
            # Even a registered fingerprint is not proof of product intent/provenance.
            (await session.get(WorkspaceSlot, target.slot_id)).current_fingerprint = WorkspaceFingerprinter().capture(root).digest
    result = await LoopWorkspaceAdoptionService(sessions).adopt(decision_id)
    async with sessions() as session:
        conflict = await session.get(WorkspaceAdoption, pending.adoption_id)
    if not unknown_target:
        assert result.status == "committed", conflict.conflict
    return result, conflict, source, target


async def _advance(sessions):
    from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
    from backend.tests.test_loop_memory_execution_recovery import ControlledMemory

    memory = ControlledMemory()
    memory.release.set()
    for _ in range(3):
        await TaskProgressRuntime(sessions, None, model_factory=lambda name: memory).drain()
        await LoopCoordinator(sessions).maintain_rounds()


async def _intent(sessions, fixture, action, *, mode="workspace-write"):
    async with sessions() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"])
        current = await session.get(LoopRound, loop.current_round_id)
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
        contexts = list(await session.scalars(select(LoopContextMembership.context_id).where(
            LoopContextMembership.loop_id == loop.loop_id, LoopContextMembership.status == "active")))
        intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
            loop_id=loop.loop_id, round_id=current.round_id, loop_revision=loop.revision,
            holder_id=loop.holder_id, grant_id=grant.grant_id, grant_revision=grant.revision,
            goal_revision=loop.goal_revision, observed_frontier_hash=current.frontier_hash,
            observed_workspace_revision=current.workspace_revision, rationale="Preserve both task behaviors",
            actions=(action,), context_permissions=tuple({"context_id": identity,
                "access_mode": mode if identity == action.get("context_id") else None} for identity in contexts))
    intent = await freeze_patrol_intent(sessions, intent)
    async with sessions.begin() as session:
        observation = await session.get(LoopObservation, intent.observation_id)
        adoption = await session.get(WorkspaceAdoption, action.get("workspace_repair", {}).get("adoption_id")) if action.get("workspace_repair") else None
        if adoption:
            from backend.app.desktop.agent_loop.observation import observation_hash
            from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope

            envelope = {**observation.envelope, "workspace": {**observation.envelope["workspace"], "adoptions": [{
                "adoption_id": adoption.adoption_id,
                "repair_context": WorkspaceConflictRepair.reference(adoption.evidence.get("repair_context"))}]}}
            observation.envelope, observation.envelope_hash = envelope, observation_hash(LoopObservationEnvelope.model_validate(envelope))
            intent = intent.model_copy(update={"observed_envelope_hash": observation.envelope_hash})
    return intent


class _RepairWorker:
    def __init__(self, sessions):
        self.sessions = sessions
        self.roots = []

    async def __call__(self, directive, message, slot_id):
        sessions = self.sessions
        async with sessions.begin() as session:
            loop = await session.get(AgentLoop, directive.loop_id)
            context = await session.get(DesktopThread, directive.target_context_id)
            equipment = await resolve_directive_equipment(session, loop, directive)
            run_id = uuid.uuid4().hex
            admitted = await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, DesktopRun(
                run_id=run_id, task_id=context.task_id, agent_id="repair-file-worker", kind="main", status="pending",
                origin="delegated_patrol", execution_thread_id=context.thread_id, context_revision_id=context.current_revision_id,
                loop_id=loop.loop_id, round_id=directive.round_id, directive_id=directive.directive_id,
                action_id=directive.action_id, equipment=equipment, workspace_anchor={"slot_id": slot_id}))
            assert admitted.created
        body = SimpleNamespace(context={})
        slot, lease = await LoopRunWorkspaceBinder(sessions).bind(run_id=run_id, loop_id=directive.loop_id,
            body=body, slot_id=slot_id, directive_id=directive.directive_id)
        await LoopRunExecutionBoundary(sessions).validate(run_id)
        async with sessions.begin() as session:
            run = await session.get(DesktopRun, run_id)
            run.status = "running"
            dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
            dispatch.status, dispatch.claimed_by = "running", "repair-file-worker"
            dispatch.fencing_token, dispatch.attempt = 1, 1
            dispatch.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            lifecycle = DirectiveLifecycleRepository()
            await lifecycle.transition(session, directive.directive_id, "delivered", run_id=run_id)
            await lifecycle.transition(session, directive.directive_id, "run_started", run_id=run_id)
            (await session.get(LoopDirective, directive.directive_id)).launched_run_id = run_id
        assert slot.kind == "isolated"
        root = Path(slot.root_path)
        self.roots.append(root)
        await body.context["workspace_lease_guard"](lease.lease_id, lease.fencing_token)
        if lease.mode.value == "write":
            assert (root / "tracked.txt").read_text(encoding="utf-8") == "feature A\n"
            (root / "tracked.txt").write_text("feature A\nfeature B\n", encoding="utf-8")
        else:
            assert (root / "tracked.txt").read_text(encoding="utf-8") == "feature A\nfeature B\n"
        fingerprint = WorkspaceFingerprinter().capture(root)
        revision = await WorkspaceFingerprinter(sessions).compare_and_advance(slot.slot_id, slot.revision, fingerprint)
        await WorkspaceLeaseManager(sessions).release(lease.lease_id, lease.fencing_token)
        async with sessions.begin() as session:
            run = await session.get(DesktopRun, run_id)
            run.status, run.settled_at = "success", datetime.now(UTC)
            run.workspace_result = {"slot_id": slot.slot_id, "revision": revision, "fingerprint": fingerprint.digest}
            anchor = await session.get(RunExecutionAnchor, run_id)
            anchor.resulting_workspace_revision, anchor.resulting_fingerprint = revision, fingerprint.digest
            anchor.adoption_state, anchor.settled_at = "pending", datetime.now(UTC)
            (await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))).status = "settled"
            await LoopCoordinator(sessions).handle_run_settled(SimpleNamespace(run_id=run_id,
                event_id=uuid.uuid4().hex, payload={}), session)
        return run_id


def test_conflict_repair_requires_proven_target_history(tmp_path):
    async def run():
        async with _loop(tmp_path, "repair-proven") as (sessions, fixture):
            result, adoption, source, target = await _conflict(sessions, fixture, tmp_path)
            assert result.status == "committed", (result.reason, adoption.conflict, adoption.evidence)
            assert adoption.status == "conflict" and not adoption.application_intent
            context = adoption.evidence["repair_context"]
            assert context["source_intent"] == "Preserve feature B alongside feature A"
            assert context["target_intents"] == ["Preserve feature A"]
            assert "feature A" in context["target_patch"] and "feature B" in context["source_patch"]
            assert (Path(target.root_path) / "tracked.txt").read_text(encoding="utf-8") == "feature A\n"
            assert (Path(source.root_path) / "tracked.txt").read_text(encoding="utf-8") == "feature B\n"
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == "running"
    asyncio.run(run())


def test_registered_user_changes_still_require_user_decision(tmp_path):
    async def run():
        async with _loop(tmp_path, "repair-user") as (sessions, fixture):
            result, adoption, _, target = await _conflict(sessions, fixture, tmp_path, unknown_target=True)
            assert result.status == "rejected" and "repair_context" not in adoption.evidence
            assert (Path(target.root_path) / "tracked.txt").read_text(encoding="utf-8") == "user private edit\n"
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert loop.status == "waiting_user"
                assert "需确认用户修改" in loop.waiting_reason
    asyncio.run(run())


@pytest.mark.parametrize("crash_phase", (None, "before_apply", "after_apply", "after_commit"))
def test_repair_dispatch_and_adoption_review_gate(tmp_path, monkeypatch, crash_phase):
    from backend.app.desktop.agent_loop import workspace_planning

    monkeypatch.setattr(workspace_planning, "global_home", lambda: tmp_path.parent / ("h" + fixture_home))
    fixture_home = uuid.uuid4().hex[:6]

    async def run():
        async with _loop(tmp_path, "repair-flow") as (sessions, fixture):
            _, adoption, source, target = await _conflict(sessions, fixture, tmp_path)
            await _advance(sessions)
            action = {"action": "continue_context", "context_id": fixture["context_id"],
                "context_revision_id": fixture["revision_id"], "message": "Integrate both features and test",
                "workspace_repair": {"adoption_id": adoption.adoption_id, "phase": "repair"}}
            intent = await _intent(sessions, fixture, action)
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "committed", result.reason
            async with sessions() as session:
                directive = await session.get(LoopDirective, result.directive_ids[0])
            repair_service = WorkspaceConflictRepair(sessions)
            prepared = await repair_service.plan(directive)
            await asyncio.sleep(1.1)
            retry = await repair_service.plan(directive)
            assert prepared.slot_id == retry.slot_id
            worker = _RepairWorker(sessions)
            head_before = _git(Path(target.root_path), "rev-parse", "HEAD")
            index_before = _git(Path(target.root_path), "diff", "--cached", "--binary")
            runs = await LoopWaveDispatcher(sessions, worker).dispatch(fixture["loop_id"], intent.round_id, 1)
            assert len(runs) == 1
            async with sessions() as session:
                run_row = await session.get(DesktopRun, runs[0])
                repaired = await session.get(WorkspaceSlot, run_row.workspace_anchor["slot_id"])
                with pytest.raises(WorkspaceRepairRejected, match="独立只读"):
                    await WorkspaceConflictRepair(sessions).validate_result(session, repaired)
            assert worker.roots[0] != Path(target.root_path) and worker.roots[0] != Path(source.root_path)
            assert (Path(target.root_path) / "tracked.txt").read_text(encoding="utf-8") == "feature A\n"
            assert _git(Path(target.root_path), "rev-parse", "HEAD") == head_before
            assert _git(Path(target.root_path), "diff", "--cached", "--binary") == index_before
            await _advance(sessions)
            # Create an independent, read-only review Context under the existing grant.
            from backend.app.desktop.context_evolution import (ContextRevisionRepository, ContextRevisionContract,
                ContextRevisionRef, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionOriginKind)

            review_id, revision_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopThread(task_id=review_id, workspace_id=target.workspace_id, thread_id=review_id, title="review"))
                await session.flush()
                ref = ContextRevisionRef(context_id=review_id, revision_id=revision_id, generation=1,
                    execution_thread_id=review_id, checkpoint_ns="", checkpoint_id="review",
                    payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, content_hash="c" * 64,
                    projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT,
                    created_at=datetime.now(UTC)))
                await ContextRevisionRepository().switch_current(session, ref, None)
                membership = await session.scalar(select(LoopContextMembership).where(
                    LoopContextMembership.loop_id == fixture["loop_id"], LoopContextMembership.context_id == fixture["context_id"]))
                session.add(LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
                    context_id=review_id, lane_id=membership.lane_id, role="support"))
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == fixture["loop_id"]))
                grant.context_scope = [*grant.context_scope, review_id]
            review = {**action, "context_id": review_id, "context_revision_id": revision_id,
                "message": "Verify both behaviors and review all changes", "workspace_repair": {
                    "adoption_id": adoption.adoption_id, "phase": "review", "slot_id": repaired.slot_id, "revision": repaired.revision}}
            review_intent = await _intent(sessions, fixture, review, mode="read-only")
            reviewed = await LoopKernel(sessions).commit(review_intent)
            assert reviewed.status == "committed", reviewed.reason
            assert len(await LoopWaveDispatcher(sessions, worker).dispatch(fixture["loop_id"], review_intent.round_id, 1)) == 1
            async with sessions() as session:
                await WorkspaceConflictRepair(sessions).validate_result(session, repaired)
                review_run = await session.scalar(select(DesktopRun).where(
                    DesktopRun.directive_id == reviewed.directive_ids[0]))
                review_anchor = await session.get(RunExecutionAnchor, review_run.run_id)
                fingerprint = review_anchor.resulting_fingerprint
                review_anchor.resulting_fingerprint = "0" * 64
                with pytest.raises(WorkspaceRepairRejected, match="独立只读"):
                    await WorkspaceConflictRepair(sessions).validate_result(session, repaired)
                review_anchor.resulting_fingerprint = fingerprint
                await session.flush()
            await _advance(sessions)
            adopt_intent = await _intent(sessions, fixture, {"action": "adopt_workspace_result",
                "source_slot_id": repaired.slot_id, "source_revision": repaired.revision,
                "rationale": "Tests and independent review confirm both features"})
            service = LoopWorkspaceAdoptionService(sessions)
            if crash_phase:
                from backend.app.desktop.agent_loop.kernel import KernelCommitResult
                from backend.tests.test_workspace_adoption_recovery import _crash

                class CrashPort:
                    async def adopt(self, decision_id):
                        async with sessions.begin() as session:
                            request = await service._request(session, decision_id)
                        await _crash(request, Path(target.root_path), crash_phase)
                        return KernelCommitResult(decision_id, "adopting", (request.adoption_id,), ())

                interrupted = await LoopKernel(sessions, workspace_adoption=CrashPort()).commit(adopt_intent)
                assert interrupted.status == "adopting", interrupted.reason
                await service.recover()
                await service.recover()
            final = await LoopKernel(sessions, workspace_adoption=service).commit(adopt_intent)
            assert final.status == "committed", final.reason
            assert (Path(target.root_path) / "tracked.txt").read_text(encoding="utf-8") == "feature A\nfeature B\n"
            assert (Path(source.root_path) / "tracked.txt").read_text(encoding="utf-8") == "feature B\n"
            # Adoption replay cannot apply twice or increment the target twice.
            replay = await LoopKernel(sessions, workspace_adoption=LoopWorkspaceAdoptionService(sessions)).commit(adopt_intent)
            assert replay.status == "committed"
            async with sessions() as session:
                assert (await session.get(WorkspaceSlot, target.slot_id)).revision == 3
                original = await session.get(WorkspaceAdoption, adoption.adoption_id)
                assert original.status == "conflict" and original.evidence["repaired_by"]
                old_anchor = await session.scalar(select(RunExecutionAnchor).where(RunExecutionAnchor.slot_id == source.slot_id))
                assert old_anchor.adoption_state == "repaired"
    asyncio.run(run())


@pytest.mark.parametrize("variant", ("source_edit", "target_edit", "target_revision", "authority", "uncertain"))
def test_repair_rechecks_evidence_before_dispatch(tmp_path, monkeypatch, variant):
    from backend.app.desktop.agent_loop import workspace_planning

    monkeypatch.setattr(workspace_planning, "global_home", lambda: tmp_path.parent / ("h" + uuid.uuid4().hex[:6]))

    async def run():
        async with _loop(tmp_path, "repair-recheck") as (sessions, fixture):
            _, adoption, source, target = await _conflict(sessions, fixture, tmp_path)
            await _advance(sessions)
            action = {"action": "continue_context", "context_id": fixture["context_id"],
                "context_revision_id": fixture["revision_id"], "message": "Preserve both features",
                "workspace_repair": {"adoption_id": adoption.adoption_id, "phase": "repair"}}
            intent = await _intent(sessions, fixture, action)
            if variant in {"authority", "target_revision", "uncertain"}:
                async with sessions.begin() as session:
                    if variant == "authority":
                        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == fixture["loop_id"]))
                        grant.capabilities = [item for item in grant.capabilities if item != "isolate_workspace"]
                    elif variant == "target_revision":
                        (await session.get(WorkspaceSlot, target.slot_id)).revision += 1
                    else:
                        row = await session.get(WorkspaceAdoption, adoption.adoption_id)
                        row.application_intent = {"unknown": True}
                result = await LoopKernel(sessions).commit(intent)
                assert result.status == "rejected", result.reason
            else:
                result = await LoopKernel(sessions).commit(intent)
                assert result.status == "committed", result.reason
                root = Path(source.root_path if variant == "source_edit" else target.root_path)
                (root / "user.txt").write_text("unconfirmed user edit", encoding="utf-8")

                async def must_not_launch(*args):
                    pytest.fail("drift must be rejected before Run launch")

                with pytest.raises(WorkspaceRepairRejected, match="未确认修改"):
                    await LoopWaveDispatcher(sessions, must_not_launch).dispatch(fixture["loop_id"], intent.round_id, 1)
                assert (root / "user.txt").read_text(encoding="utf-8") == "unconfirmed user edit"
            assert (Path(target.root_path) / "tracked.txt").read_text(encoding="utf-8") == "feature A\n"
    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"adoption_id": "a", "phase": "review"},
    {"adoption_id": "a", "phase": "repair", "slot_id": "s"},
    {"adoption_id": "a", "phase": "repair", "revision": 2},
])
def test_repair_selection_rejects_incomplete_or_forged_slot(payload):
    with pytest.raises(ValueError):
        WorkspaceRepairSelection.model_validate(payload)
