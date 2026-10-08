r"""本文件对外提供有界 Run 波次、真实 Git worktree 补位和部分失败回归。

输入为 PostgreSQL 中由 Kernel 授权的 Context/Lane、临时 Git 基线与事件屏障；输出为实际活动
Run、独立 HEAD/index/root、执行锚点、lease、等待及成果保留断言。流程复用 RunAdmission、
WorkspaceBinder 和 Coordinator，测试 Worker 仅执行确定性文件操作，不调用模型或模拟物理隔离。
完整阶段由普通 Run 串行准备已授权空项目 baseline，再派发三个模块、重建 Run pool、串行采用及集成；
集成使用只读 Run 受理、lease、执行锚点与结算，重放最后事件后仍只有一个后继 Round。
真实模型只验证合成阶段的认知判断，不代替这里的物理 Run/数据库验证。
示例：pytest backend/tests/test_loop_worktree_parallel_runs.py。Patrol 判断另由认知合同及真实模型演练验证。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import uuid
from datetime import UTC, datetime
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.test_loop_runtime_pools import _create_wave
from backend.tests.test_git_workspace_adoption import _git
from backend.app.desktop.agent_loop import ContextRunPool, LoopCoordinator, LoopKernel, PatrolDecisionIntent
from backend.app.desktop.agent_loop import workspace_planning
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim
from backend.app.desktop.agent_loop.dispatch import LoopRunWorkspaceBinder, LoopWaveDispatcher
from backend.app.desktop.agent_loop.directive_equipment import resolve_directive_equipment
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.directive_causality import DirectiveCausalityRecorder
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopRound
from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.agent_loop.workspace_adoption import LoopWorkspaceAdoptionService
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService, _PreparedViewsChanged
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.workspace_coordination import WorkspaceFingerprinter, WorkspaceLeaseManager
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceSlot

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.fixture(autouse=True)
def _managed_worktrees(tmp_path, monkeypatch):
    task_home = tmp_path.parent / ("h" + uuid.uuid4().hex[:6])
    monkeypatch.setattr(workspace_planning, "global_home", lambda: task_home)


class _FileWorker:
    def __init__(self, sessions, *, baseline_on_first=False, fail_index=None, shared_file=False, expected_files=None):
        self._sessions = sessions
        self._baseline_on_first = baseline_on_first
        self._fail_index = fail_index
        self._shared_file = shared_file
        self._expected_files = expected_files
        size = 4 if baseline_on_first else 3
        self.started = [asyncio.Event() for _ in range(size)]
        self.release = [asyncio.Event() for _ in range(size)]
        self.done = [asyncio.Event() for _ in range(size)]
        self.runs = []
        self.tasks = []
        self.roots = []

    async def __call__(self, directive, message, slot_id):
        run_id = uuid.uuid4().hex
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, directive.loop_id)
            context = await session.get(DesktopThread, directive.target_context_id)
            equipment = await resolve_directive_equipment(session, loop, directive)
            result = await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, DesktopRun(
                run_id=run_id, task_id=context.task_id, agent_id=f"main:{context.task_id}", kind="main",
                status="pending", origin="delegated_patrol", execution_thread_id=context.thread_id,
                context_revision_id=context.current_revision_id, context_checkpoint_id="fixture-checkpoint",
                directive_id=directive.directive_id, loop_id=loop.loop_id, round_id=directive.round_id,
                idempotency_key=f"test:{directive.directive_id}", equipment=equipment,
                workspace_anchor={"slot_id": slot_id}, input_messages=[]))
            assert result.created
        body = SimpleNamespace(context={})
        slot, lease = await LoopRunWorkspaceBinder(self._sessions).bind(
            run_id=run_id, loop_id=directive.loop_id, body=body, slot_id=slot_id,
            directive_id=directive.directive_id)
        await LoopRunExecutionBoundary(self._sessions).validate(run_id)
        async with self._sessions.begin() as session:
            await session.get(AgentLoop, directive.loop_id, with_for_update=True)
            run = await session.get(DesktopRun, run_id, with_for_update=True)
            run.status = "running"
            dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
            dispatch.status, dispatch.claimed_by = "running", "file-worker-fixture"
            dispatch.attempt, dispatch.fencing_token = 1, 1
            dispatch.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            lifecycle = DirectiveLifecycleRepository()
            await lifecycle.transition(session, directive.directive_id, "delivered", run_id=run_id)
            current = await lifecycle.transition(session, directive.directive_id, "run_started", run_id=run_id)
            await DirectiveCausalityRecorder().run_started(session, current, run_id)
        index = len(self.runs)
        self.runs.append(run_id)
        self.roots.append(Path(slot.root_path))
        self.tasks.append(asyncio.create_task(self._work(index, run_id, slot, lease, body)))
        return run_id

    async def _work(self, index, run_id, slot, lease, body):
        await body.context["workspace_lease_guard"](lease.lease_id, lease.fencing_token)
        if self._expected_files is not None:
            assert slot.kind == "authoritative" and lease.mode.value == "read"
            for name, content in self._expected_files.items():
                assert (Path(slot.root_path) / name).read_text(encoding="utf-8") == content
        elif self._baseline_on_first and index == 0:
            from backend.tests.test_git_workspace_adoption import _repository

            _repository(Path(slot.root_path))
        else:
            (Path(slot.root_path) / f"module-{index}.txt").write_text(f"result-{index}", encoding="utf-8")
            if self._shared_file:
                (Path(slot.root_path) / "tracked.txt").write_text(f"shared-result-{index}\n", encoding="utf-8")
        self.started[index].set()
        await self.release[index].wait()
        await body.context["workspace_lease_guard"](lease.lease_id, lease.fencing_token)
        fingerprint = WorkspaceFingerprinter().capture(Path(slot.root_path))
        revision = await WorkspaceFingerprinter(self._sessions).compare_and_advance(slot.slot_id, slot.revision, fingerprint)
        await WorkspaceLeaseManager(self._sessions).release(lease.lease_id, lease.fencing_token)
        async with self._sessions.begin() as session:
            identity = await session.get(DesktopRun, run_id)
            await session.get(AgentLoop, identity.loop_id, with_for_update=True)
            run = await session.get(DesktopRun, run_id, with_for_update=True, populate_existing=True)
            run.status, run.settled_at = "error" if index == self._fail_index else "success", datetime.now(UTC)
            run.error = "file worker failed after partial effect" if index == self._fail_index else None
            run.workspace_result = {"slot_id": slot.slot_id, "revision": revision, "fingerprint": fingerprint.digest}
            anchor = await session.get(RunExecutionAnchor, run_id)
            anchor.resulting_workspace_revision = revision
            anchor.resulting_fingerprint = fingerprint.digest
            anchor.adoption_state = "pending" if slot.kind == "isolated" else "not_required"
            dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
            dispatch.status = "settled"
            await LoopCoordinator(self._sessions).handle_run_settled(
                SimpleNamespace(run_id=run_id, event_id=uuid.uuid4().hex, payload={}), session)
        self.done[index].set()


async def _decision(sessions, loop_id, action, *, expected_status="committed"):
    async with sessions() as session:
        loop = await session.get(AgentLoop, loop_id)
        round_row = await session.get(LoopRound, loop.current_round_id)
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.revision == loop.authority_revision))
        intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
            loop_id=loop_id, loop_revision=loop.revision, round_id=round_row.round_id, holder_id=loop.holder_id,
            grant_id=grant.grant_id, grant_revision=grant.revision, goal_revision=loop.goal_revision,
            observed_frontier_hash=round_row.frontier_hash, observed_workspace_revision=round_row.workspace_revision,
            rationale="Verify independently completed module", actions=tuple(action) if isinstance(action, list) else (action,))
    result = await LoopKernel(sessions, workspace_adoption=LoopWorkspaceAdoptionService(sessions)).commit(intent)
    assert result.status == expected_status, result.reason
    return intent, result


def test_git_writers_overlap_and_running_round_refills_without_touching_authority(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, contexts, foundation_ids = await _create_wave(
            sessions, tmp_path, count=4, writing=True, action_count=1)
        worker = _FileWorker(sessions, baseline_on_first=True)
        integrator = None
        pool = ContextRunPool(sessions, LoopCoordinator(sessions), LoopWaveDispatcher(sessions, worker), concurrency=2)
        try:
            assert await pool.drain(snapshot["loop_id"]) == 1
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            await asyncio.wait_for(worker.started[0].wait(), 10)
            assert len(worker.runs) == 1
            worker.release[0].set()
            await asyncio.wait_for(worker.done[0].wait(), 10)
            async with sessions() as session:
                authority = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
                assert worker.roots[0] == Path(authority.root_path) and authority.revision == 2
                assert (await session.get(LoopDirective, foundation_ids[0])).lifecycle_state == "settled"
            assert not _git(Path(authority.root_path), "status", "--porcelain")
            module_intent, modules = await _decision(sessions, snapshot["loop_id"], [
                {"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id,
                    "message": "Implement independently testable module on verified baseline"}
                for context_id, revision_id in contexts[1:]])
            directives = modules.directive_ids
            module_round_id = module_intent.round_id
            assert await pool.drain(snapshot["loop_id"]) == 1
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            async with sessions() as session:
                rows = (await session.scalars(select(LoopDirective).where(LoopDirective.directive_id.in_(directives)))).all()
                assert len(worker.runs) == 3, [(row.status, row.queued_reason) for row in rows]
            await asyncio.wait_for(asyncio.gather(worker.started[1].wait(), worker.started[2].wait()), 10)
            assert not worker.done[1].is_set() and not worker.done[2].is_set()
            async with sessions() as session:
                live = await LoopLiveSnapshotProjector().project(session, snapshot["loop_id"])
                assert {live.runs[run_id].state["status"] for run_id in worker.runs[1:]} == {"running"}
            assert await pool.drain(snapshot["loop_id"]) == 0
            await pool.close()
            pool = ContextRunPool(sessions, LoopCoordinator(sessions), LoopWaveDispatcher(sessions, worker), concurrency=2)
            assert await pool.drain(snapshot["loop_id"]) == 0
            async with sessions() as session:
                authority = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
                assert (await session.get(LoopRound, module_round_id)).status == "running"
            baseline = _git(Path(authority.root_path), "rev-parse", "HEAD")
            worker.release[2].set()
            await asyncio.wait_for(worker.done[2].wait(), 10)
            assert not worker.done[1].is_set()
            assert await pool.drain(snapshot["loop_id"]) == 1
            await asyncio.wait_for(worker.started[3].wait(), 10)
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert len(set(worker.roots[1:])) == 3
            assert len({_git(root, "rev-parse", "--git-path", "index") for root in worker.roots[1:]}) == 3
            for index, root in enumerate(worker.roots[1:], start=1):
                assert root != Path(authority.root_path)
                assert _git(root, "rev-parse", "HEAD") == baseline
                assert (root / f"module-{index}.txt").is_file()
                assert not list(Path(authority.root_path).glob("module-*.txt"))
                assert len(list(root.glob("module-*.txt"))) == 1
            worker.release[1].set()
            worker.release[3].set()
            await asyncio.wait_for(asyncio.gather(worker.done[1].wait(), worker.done[3].wait()), 10)
            async with sessions() as session:
                rows = (await session.scalars(select(LoopDirective).where(LoopDirective.directive_id.in_(directives)))).all()
                assert {row.lifecycle_state for row in rows} == {"settled"}
                anchors = (await session.scalars(select(RunExecutionAnchor).where(RunExecutionAnchor.run_id.in_(worker.runs[1:])))).all()
                assert len(anchors) == 3 and {row.adoption_state for row in anchors} == {"pending"}
                assert (await session.get(LoopRound, module_round_id)).status == "settled"
                live = await LoopLiveSnapshotProjector().rebuild(session, snapshot["loop_id"])
                assert {live.runs[run_id].state["status"] for run_id in worker.runs[1:]} == {"success"}
            for index, anchor in enumerate(anchors):
                intent, result = await _decision(sessions, snapshot["loop_id"], {
                    "action": "adopt_workspace_result", "source_slot_id": anchor.slot_id,
                    "source_revision": anchor.resulting_workspace_revision, "rationale": "Verified module file"})
                assert (await LoopKernel(sessions).commit(intent)).decision_id == result.decision_id
                async with sessions() as session:
                    current = await session.get(WorkspaceSlot, authority.slot_id)
                    assert current.revision == 3 + index
                    assert (await session.get(RunExecutionAnchor, anchor.run_id)).adoption_state == "adopted"
            assert [ (Path(authority.root_path) / f"module-{index}.txt").read_text(encoding="utf-8")
                    for index in range(1, 4)] == [f"result-{index}" for index in range(1, 4)]
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, snapshot["loop_id"], with_for_update=True)
                loop.equipment = {**loop.equipment, "permissions": ["read"]}
            integration_intent, integration = await _decision(sessions, snapshot["loop_id"], {
                "action": "continue_context", "context_id": contexts[0][0],
                "context_revision_id": contexts[0][1],
                "message": "Verify all adopted module files in authoritative workspace"})
            integrator = _FileWorker(sessions, expected_files={f"module-{index}.txt": f"result-{index}" for index in range(1, 4)})
            integration_runs = await LoopCoordinator(sessions).dispatch_ready(
                CoordinatorClaim("test", snapshot["loop_id"], integration_intent.round_id, "0"),
                LoopWaveDispatcher(sessions, integrator), 1)
            await asyncio.wait_for(integrator.started[0].wait(), 10)
            integration_run_id = integration_runs[0]
            assert integrator.roots == [Path(authority.root_path)]
            async with sessions() as session:
                assert (await session.get(DesktopRun, integration_run_id)).status == "running"
                anchor = await session.get(RunExecutionAnchor, integration_run_id)
                assert anchor.slot_id == authority.slot_id and anchor.observed_workspace_revision == 5
                assert anchor.lease_id is not None
            integrator.release[0].set()
            await asyncio.wait_for(integrator.done[0].wait(), 10)
            await asyncio.gather(*integrator.tasks)
            async with sessions() as session:
                assert (await session.get(DesktopRun, integration_run_id)).status == "success"
                assert (await session.get(LoopDirective, integration.directive_ids[0])).lifecycle_state == "settled"
                assert (await session.get(LoopRound, integration_intent.round_id)).status == "settled"
                anchor = await session.get(RunExecutionAnchor, integration_run_id)
                assert anchor.resulting_workspace_revision == 5 and anchor.adoption_state == "not_required"
                assert (await session.scalar(select(RunDispatch).where(RunDispatch.run_id == integration_run_id))).status == "settled"
                successor_id = (await session.get(AgentLoop, snapshot["loop_id"])).current_round_id
                round_count = await session.scalar(select(func.count()).select_from(LoopRound).where(LoopRound.loop_id == snapshot["loop_id"]))
            async with sessions.begin() as session:
                await LoopCoordinator(sessions).handle_run_settled(
                    SimpleNamespace(run_id=integration_run_id, event_id=uuid.uuid4().hex, payload={}), session)
            async with sessions() as session:
                assert (await session.get(AgentLoop, snapshot["loop_id"])).current_round_id == successor_id
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(LoopRound.loop_id == snapshot["loop_id"])) == round_count
        finally:
            if integrator is not None:
                for release in integrator.release:
                    release.set()
                await asyncio.gather(*integrator.tasks, return_exceptions=True)
            for release in worker.release:
                release.set()
            await pool.close()
            await asyncio.gather(*worker.tasks, return_exceptions=True)
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["retained_result", "external_edit", "new_baseline"])
def test_worktree_reuse_preserves_results_and_checks_physical_baseline(tmp_path, change):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, contexts, directives = await _create_wave(sessions, tmp_path, writing=True, git=True)
        planner = workspace_planning.WorkspaceRunPlanner(sessions)
        try:
            plans = await planner.plan_wave(snapshot["loop_id"], directives, 3)
            assert len(plans) == 3
            assert await planner.plan_wave(snapshot["loop_id"], directives, 3) == plans
            async with sessions.begin() as session:
                slot = await session.get(WorkspaceSlot, plans[0].slot_id)
                authority = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
                previous_root = Path(slot.root_path)
                if change == "retained_result":
                    (previous_root / "unadopted.txt").write_text("keep result", encoding="utf-8")
                    slot.lifecycle = "retained"
                elif change == "external_edit":
                    (previous_root / "external.txt").write_text("keep user edit", encoding="utf-8")
                else:
                    root = Path(authority.root_path)
                    (root / "interface.txt").write_text("verified v2", encoding="utf-8")
                    _git(root, "add", "interface.txt")
                    _git(root, "commit", "-m", "verified baseline v2")
                    authority.current_fingerprint = WorkspaceFingerprinter().capture(root).digest
                    authority.revision += 1
            if change == "new_baseline":
                assert await planner.plan_wave(snapshot["loop_id"], directives, 3) == ()
                await service.control(snapshot["loop_id"], "pause")
                await service.control(snapshot["loop_id"], "resume")
                _, new_wave = await _decision(sessions, snapshot["loop_id"], [
                    {"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id,
                        "message": "Use freshly confirmed interface baseline"} for context_id, revision_id in contexts])
                directives = new_wave.directive_ids
            next_plans = await planner.plan_wave(snapshot["loop_id"], directives, 3)
            assert len(next_plans) == 3
            assert next_plans[0].slot_id != plans[0].slot_id
            assert previous_root.is_dir()
            assert (previous_root / "unadopted.txt").is_file() if change == "retained_result" else True
            assert (previous_root / "external.txt").is_file() if change == "external_edit" else True
            if change == "new_baseline":
                async with sessions() as session:
                    new_slot = await session.get(WorkspaceSlot, next_plans[0].slot_id)
                    assert new_slot.base_revision != slot.base_revision
                    assert (Path(new_slot.root_path) / "interface.txt").read_text(encoding="utf-8") == "verified v2"
        finally:
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


def test_partial_delivery_failure_preserves_started_run_and_releases_untried_claims(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, directives = await _create_wave(sessions, tmp_path, writing=True, git=True)
        worker = _FileWorker(sessions)
        async def launch(directive, message, slot_id):
            if worker.runs:
                raise RuntimeError("delivery failed")
            return await worker(directive, message, slot_id)
        dispatcher = LoopWaveDispatcher(sessions, launch)
        try:
            with pytest.raises(RuntimeError, match="delivery failed"):
                await LoopCoordinator(sessions).dispatch_ready(
                    CoordinatorClaim("test", snapshot["loop_id"], snapshot["current_round_id"], "0"), dispatcher, 3)
            await asyncio.wait_for(worker.started[0].wait(), 10)
            async with sessions() as session:
                rows = (await session.scalars(select(LoopDirective).where(LoopDirective.directive_id.in_(directives)))).all()
                assert sorted(row.status for row in rows) == ["created", "created", "launched"]
                assert sorted(row.attempt for row in rows) == [0, 1, 1]
                assert (await session.get(LoopRound, snapshot["current_round_id"])).status == "running"
            await service.control(snapshot["loop_id"], "pause")
            assert await dispatcher.dispatch(snapshot["loop_id"], snapshot["current_round_id"], 3) == ()
            with pytest.raises(ValueError):
                async with sessions.begin() as session:
                    await RunOwnershipPolicy().assert_live(session, await session.get(DesktopRun, worker.runs[0]))
            assert (worker.roots[0] / "module-0.txt").read_text(encoding="utf-8") == "result-0"
        finally:
            for release in worker.release:
                release.set()
            await asyncio.gather(*worker.tasks, return_exceptions=True)
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


def test_frozen_workspace_facts_recheck_slot_version_and_expired_grant(tmp_path):
    from backend.tests.test_round_task_progress import _Checkpointer

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, directives = await _create_wave(sessions, tmp_path, writing=True, git=True)
        capture = LoopObservationService(sessions, _Checkpointer())
        try:
            plans = await workspace_planning.WorkspaceRunPlanner(sessions).plan_wave(snapshot["loop_id"], directives, 3)
            views = await capture._prepare_views(snapshot["loop_id"])
            async with sessions.begin() as session:
                authority = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
                authority.revision += 1
            with pytest.raises(_PreparedViewsChanged, match="工作区"):
                await capture._capture_once(snapshot["loop_id"], snapshot["current_round_id"], views)
            frozen = await capture.capture(snapshot["loop_id"], snapshot["current_round_id"])
            assert frozen.workspace["git_revision"] == _git(Path(authority.root_path), "rev-parse", "HEAD")
            assert frozen.workspace["git_dirty"] is False and frozen.workspace["isolation_authorized"] is True
            assert {slot["slot_id"] for slot in frozen.workspace["isolated_slots"]} == {plan.slot_id for plan in plans}
            async with sessions.begin() as session:
                grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
                grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            (Path(authority.root_path) / "user-edit.txt").write_text("keep unknown user edit", encoding="utf-8")
            restored = await capture.capture(snapshot["loop_id"], snapshot["current_round_id"])
            assert restored.model_dump(mode="json") == frozen.model_dump(mode="json")
            async def forbidden_launch(*_):
                pytest.fail("expired authority launched a Run")
            assert await LoopWaveDispatcher(sessions, forbidden_launch).dispatch(
                snapshot["loop_id"], snapshot["current_round_id"], 3) == ()
            assert (Path(authority.root_path) / "user-edit.txt").read_text(encoding="utf-8") == "keep unknown user edit"
            assert _git(Path(authority.root_path), "status", "--porcelain")
        finally:
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["run_error", "adoption_conflict"])
def test_branch_failure_and_shared_file_conflict_preserve_other_results(tmp_path, failure):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, _ = await _create_wave(sessions, tmp_path, count=2, writing=True, git=True)
        worker = _FileWorker(sessions, fail_index=1 if failure == "run_error" else None,
                             shared_file=failure == "adoption_conflict")
        pool = ContextRunPool(sessions, LoopCoordinator(sessions), LoopWaveDispatcher(sessions, worker), concurrency=2)
        try:
            await pool.drain(snapshot["loop_id"])
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            await asyncio.wait_for(asyncio.gather(worker.started[0].wait(), worker.started[1].wait()), 10)
            worker.release[1].set()
            await asyncio.wait_for(worker.done[1].wait(), 10)
            assert not worker.done[0].is_set()
            worker.release[0].set()
            await asyncio.wait_for(worker.done[0].wait(), 10)
            async with sessions() as session:
                authority = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
                anchors = [await session.get(RunExecutionAnchor, run_id) for run_id in worker.runs]
                statuses = [(await session.get(DesktopRun, run_id)).status for run_id in worker.runs]
                assert statuses == (["success", "error"] if failure == "run_error" else ["success", "success"])
            await _decision(sessions, snapshot["loop_id"], {"action": "adopt_workspace_result",
                "source_slot_id": anchors[0].slot_id, "source_revision": anchors[0].resulting_workspace_revision,
                "rationale": "First verified independent result"})
            if failure == "adoption_conflict":
                await _decision(sessions, snapshot["loop_id"], {"action": "adopt_workspace_result",
                    "source_slot_id": anchors[1].slot_id, "source_revision": anchors[1].resulting_workspace_revision,
                    "rationale": "Verify patch against updated target"}, expected_status="rejected")
                assert (Path(authority.root_path) / "tracked.txt").read_text(encoding="utf-8") == "shared-result-0\n"
                async with sessions() as session:
                    assert (await session.get(RunExecutionAnchor, worker.runs[1])).adoption_state == "conflict"
                    assert (await session.get(AgentLoop, snapshot["loop_id"])).status == "waiting_user"
            assert (Path(authority.root_path) / "module-0.txt").read_text(encoding="utf-8") == "result-0"
            assert not (Path(authority.root_path) / "module-1.txt").exists()
            assert (worker.roots[1] / "module-1.txt").read_text(encoding="utf-8") == "result-1"
            assert worker.roots[0].is_dir()
        finally:
            for release in worker.release:
                release.set()
            await pool.close()
            await asyncio.gather(*worker.tasks, return_exceptions=True)
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("cause", ["workspace_not_git", "workspace_isolation_not_authorized", "workspace_isolation_quota"])
def test_resource_wait_does_not_exhaust_delivery_attempts(tmp_path, monkeypatch, cause):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, directives = await _create_wave(sessions, tmp_path, writing=True, git=cause != "workspace_not_git")
        if cause == "workspace_not_git":
            async with sessions() as session:
                authority = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == snapshot["workspace_id"], WorkspaceSlot.kind == "authoritative"))
            (Path(authority.root_path) / "user-original.txt").write_text("unknown user material", encoding="utf-8")
        if cause == "workspace_isolation_not_authorized":
            async with sessions.begin() as session:
                grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
                grant.capabilities = [item for item in grant.capabilities if item != "isolate_workspace"]
        if cause == "workspace_isolation_quota":
            provider = workspace_planning.GitWorktreeIsolationProvider
            monkeypatch.setattr(workspace_planning, "GitWorktreeIsolationProvider", lambda root: provider(root, quota=0))
        worker = _FileWorker(sessions)
        dispatcher = LoopWaveDispatcher(sessions, worker)
        coordinator = LoopCoordinator(sessions)
        claim = CoordinatorClaim("test", snapshot["loop_id"], snapshot["current_round_id"], "0")
        try:
            for _ in range(5):
                await coordinator.dispatch_ready(claim, dispatcher, 3)
            async with sessions() as session:
                rows = (await session.scalars(select(LoopDirective).where(LoopDirective.directive_id.in_(directives)))).all()
                waiting = [row for row in rows if row.status == "created"]
                if cause == "workspace_isolation_quota":
                    assert len(waiting) == 2 and len(worker.runs) == 1
                    assert all(row.attempt == 0 and row.queued_reason == "authoritative_writer_active" for row in waiting)
                else:
                    assert len(worker.runs) == 1 and len(waiting) == 2
                    assert all(row.attempt == 0 and row.queued_reason == "authoritative_writer_active" for row in waiting)
            for index in range(3):
                worker.release[index].set()
                await asyncio.wait_for(worker.done[index].wait(), 10)
                if index < 2:
                    await coordinator.dispatch_ready(claim, dispatcher, 3)
            assert len(worker.runs) == 3
            if cause == "workspace_not_git":
                assert (Path(authority.root_path) / "user-original.txt").read_text(encoding="utf-8") == "unknown user material"
                assert not (Path(authority.root_path) / ".git").exists()
        finally:
            for release in worker.release:
                release.set()
            await asyncio.gather(*worker.tasks, return_exceptions=True)
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


def test_partial_preparation_is_tracked_and_claims_recover(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, directives = await _create_wave(sessions, tmp_path, writing=True, git=True)
        dispatcher = LoopWaveDispatcher(sessions, lambda *_: None)
        original = workspace_planning.GitWorktreeIsolationProvider.prepare
        prepared = []
        async def prepare(provider, request):
            if prepared:
                raise RuntimeError("second preparation failed")
            result = await original(provider, request)
            prepared.append(result)
            return result
        monkeypatch.setattr(workspace_planning.GitWorktreeIsolationProvider, "prepare", prepare)
        try:
            with pytest.raises(RuntimeError, match="second preparation"):
                await dispatcher.dispatch(snapshot["loop_id"], snapshot["current_round_id"], 3)
            async with sessions() as session:
                slots = (await session.scalars(select(WorkspaceSlot).where(WorkspaceSlot.owner_loop_id == snapshot["loop_id"]))).all()
                assert len(slots) == 1 and Path(slots[0].root_path).is_dir()
                rows = (await session.scalars(select(LoopDirective).where(LoopDirective.directive_id.in_(directives)))).all()
                assert {row.status for row in rows} == {"created"}
            monkeypatch.setattr(workspace_planning.GitWorktreeIsolationProvider, "prepare", original)
            async def launch(*_):
                return uuid.uuid4().hex
            dispatcher._launcher = launch
            assert len(await dispatcher.dispatch(snapshot["loop_id"], snapshot["current_round_id"], 3)) == 3
            async with sessions() as session:
                slots = (await session.scalars(select(WorkspaceSlot).where(WorkspaceSlot.owner_loop_id == snapshot["loop_id"]))).all()
                assert len(slots) == 3 and prepared[0].root_path in {Path(slot.root_path) for slot in slots}
        finally:
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())
