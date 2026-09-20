r"""本文件验证 Loop directive 并发认领与直接用户 Run 的统一 workspace 绑定。

输入为真实 PostgreSQL Loop、一个 committed directive、两个并发 Dispatcher 与待启动 DesktopRun；输出为
恰好一次 launch、稳定 directive 状态及同一 lease/anchor 合同。具体工作流为创建最小 Context revision，
经 Kernel 生成 delegated directive，再并发派发并独立绑定直接用户 Run。示例：
`pytest test_agent_loop_dispatch.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
import uuid

from langchain_core.messages import ToolMessage
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import (
    AgentLoopService,
    AgentLoopRecovery,
    LoopCreateRequest,
    LoopCoordinator,
    LoopCoordinatorRuntime,
    LoopKernel,
    LoopRunWorkspaceBinder,
    LoopWaveDispatcher,
    PatrolDecisionIntent,
)
from backend.app.desktop.agent_loop.models import LoopCoordinatorLease
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDirective, LoopDirectiveTransition, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.directive_causality import DirectiveCausalityQuery
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from sqlalchemy import select
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration import RunOutboxConsumer
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceLease


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _CausalCheckpointer:
    async def aget_tuple(self, config):
        checkpoint_id = config["configurable"]["checkpoint_id"]
        return SimpleNamespace(config={"configurable": {"checkpoint_id": checkpoint_id}}, checkpoint={"channel_values": {"messages": [ToolMessage(content="18 passed, 0 failed", tool_call_id="causal-test", name="pytest", id="causal-tool")] }}, metadata={})


def test_runtime_runs_comprehensive_recovery_before_polling() -> None:
    class Coordinator:
        async def recover(self):
            raise AssertionError("comprehensive recovery must own startup reconciliation")

        async def running_loop_ids(self):
            return ()

    class RunEvents:
        async def recover(self):
            raise AssertionError("comprehensive recovery must own outbox reconciliation")

        async def drain(self, _consumer, _handler, *, loop_id=None):
            return 0

    class Recovery:
        def __init__(self):
            self.calls = 0

        async def reconcile(self):
            self.calls += 1

    async def run() -> None:
        recovery = Recovery()
        runtime = LoopCoordinatorRuntime(Coordinator(), RunEvents(), recovery=recovery, poll_seconds=0.001)
        await runtime.start()
        assert recovery.calls == 1
        await runtime.close()

    asyncio.run(run())


def test_dispatch_claims_once_and_direct_user_uses_the_same_workspace_contract(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-dispatch-{suffix}"
        context_id = f"context-dispatch-{suffix}"
        revision_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        workspace_path = tmp_path / workspace_id
        workspace_path.mkdir()
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=str(workspace_path),
                        display_name="dispatch",
                    )
                )
                await session.flush()
                session.add(
                    DesktopThread(
                        task_id=context_id,
                        workspace_id=workspace_id,
                        thread_id=f"thread-{suffix}",
                        title="dispatch",
                    )
                )
                await session.flush()
                ref = ContextRevisionRef(
                    context_id=context_id,
                    revision_id=revision_id,
                    generation=1,
                    execution_thread_id=f"thread-{suffix}",
                    checkpoint_ns="",
                    checkpoint_id="checkpoint-1",
                    payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
                )
                repository = ContextRevisionRepository()
                await repository.insert(
                    session,
                    ContextRevisionContract(
                        ref=ref,
                        content_hash="a" * 64,
                        projection_status=ContextRevisionProjectionStatus.VALID,
                        origin_kind=ContextRevisionOriginKind.ROOT,
                        created_at=datetime.now(UTC),
                    ),
                )
                await repository.switch_current(session, ref, None)
                session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))

            service = AgentLoopService(sessions)
            snapshot = await service.start(
                LoopCreateRequest(
                    loop_id=loop_id,
                    workspace_id=workspace_id,
                    initial_context_id=context_id,
                    initial_run_id=f"initial-{suffix}",
                    holder_id="patrol-1",
                    goal="Finish the task",
                    task_contract="Stay in scope",
                    acceptance_criteria=(
                        {"criterion_id": "done", "text": "work is complete"},
                    ),
                    capabilities=("continue_context", "request_completion"),
                    context_scope=(context_id,),
                    permission_scope=("read",),
                    equipment={"permissions": ["read"]},
                )
            )
            async with sessions() as session:
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"decision-{suffix}",
                loop_id=loop_id,
                loop_revision=snapshot["revision"],
                round_id=snapshot["current_round_id"],
                holder_id="patrol-1",
                grant_id=snapshot["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                observed_frontier_hash=round_row.frontier_hash,
                observed_workspace_revision=round_row.workspace_revision,
                rationale="Continue in the current Context.",
                actions=(
                    {
                        "action": "continue_context",
                        "context_id": context_id,
                        "context_revision_id": revision_id,
                        "message": "Run the focused checks.",
                    },
                ),
            )
            committed = await LoopKernel(sessions).commit(intent)
            calls = []

            async def launch(directive, message, _slot_id):
                calls.append((directive.directive_id, message.id))
                await asyncio.sleep(0.05)
                async with sessions.begin() as session:
                    current = await session.get(
                        LoopDirective,
                        directive.directive_id,
                        with_for_update=True,
                    )
                    assert current.status == "launching"
                    current.status = "launched"
                    current.launched_run_id = "fake-run"
                return "fake-run"

            first, second = await asyncio.gather(
                LoopWaveDispatcher(sessions, launch).dispatch(loop_id, round_row.round_id, 1),
                LoopWaveDispatcher(sessions, launch).dispatch(loop_id, round_row.round_id, 1),
            )
            assert sorted((first, second), key=len) == [(), ("fake-run",)]
            assert len(calls) == 1
            assert calls[0][0] == committed.directive_ids[0]
            async with sessions() as session:
                delivered = await session.get(LoopDirective, committed.directive_ids[0])
                delivery_history = tuple((await session.scalars(select(LoopDirectiveTransition).where(LoopDirectiveTransition.directive_id == delivered.directive_id).order_by(LoopDirectiveTransition.revision))).all())
            assert delivered.lifecycle_state == "run_started"
            assert [item.to_state for item in delivery_history] == ["proposed", "authorized", "delivering", "delivered", "run_started"]
            assert await LoopWaveDispatcher(sessions, launch).dispatch(loop_id, round_row.round_id, 1) == ()
            assert len(calls) == 1

            direct_run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    DesktopRun(
                        run_id=direct_run_id,
                        task_id=context_id,
                        agent_id=f"main:{context_id}",
                        kind="main",
                        status="pending",
                        origin="direct_user",
                        execution_thread_id=f"thread-{suffix}",
                        context_revision_id=revision_id,
                        context_checkpoint_id="checkpoint-1",
                        loop_id=loop_id,
                        round_id=round_row.round_id,
                        equipment={"permissions": ["read"]},
                    )
                )
            body = SimpleNamespace(context={})
            slot, lease = await LoopRunWorkspaceBinder(sessions).bind(
                run_id=direct_run_id,
                loop_id=loop_id,
                body=body,
            )
            async with sessions() as session:
                anchor = await session.get(RunExecutionAnchor, direct_run_id)
                persisted_lease = await session.get(WorkspaceLease, lease.lease_id)
                assert anchor.slot_id == slot.slot_id
                assert anchor.directive_id is None
                assert persisted_lease.mode == "read"
                assert body.context["workspace_lease"]["lease_id"] == lease.lease_id
                assert callable(body.context["workspace_lease_guard"])
                assert callable(body.context["workspace_lease_renew"])
            async with sessions.begin() as session:
                direct_run = await session.get(DesktopRun, direct_run_id, with_for_update=True)
                direct_run.status = "success"
                direct_run.settled_at = datetime.now(UTC)

            recovery_worker_id = uuid.uuid4().hex
            recovery_lease_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                directive = await session.get(
                    LoopDirective,
                    committed.directive_ids[0],
                    with_for_update=True,
                )
                directive.status = "launching"
                directive.launched_run_id = None
                session.add(
                    LoopWorkerRequest(
                        worker_request_id=recovery_worker_id,
                        loop_id=loop_id,
                        round_id=round_row.round_id,
                        kind="lane_curator",
                        scope={"assignment": "recovery"},
                        status="running",
                    )
                )
                session.add(
                    LoopCoordinatorLease(
                        lease_id=recovery_lease_id,
                        round_id=round_row.round_id,
                        owner_id="dead-coordinator",
                        fencing_token=uuid.uuid4().hex,
                        expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    )
                )
            report = await AgentLoopRecovery(
                sessions,
                LoopCoordinator(sessions),
                RunOutboxConsumer(sessions),
            ).reconcile()
            assert report.coordinator_leases == 1
            assert report.worker_attempts == 1
            async with sessions() as session:
                recovered_directive = await session.get(
                    LoopDirective, committed.directive_ids[0]
                )
                recovered_worker = await session.get(
                    LoopWorkerRequest, recovery_worker_id
                )
                recovered_lease = await session.get(
                    LoopCoordinatorLease, recovery_lease_id
                )
                recovered_usage = await session.get(LoopBudgetUsage, loop_id)
                assert recovered_directive.status == "created"
                assert recovered_worker.status == "pending"
                assert recovered_worker.attempt == 2
                assert recovered_usage.retries == 2
                assert recovered_lease is None

            causal_run_id = uuid.uuid4().hex
            causal_revision_id = uuid.uuid4().hex

            async def causal_launch(directive, _message, _slot_id):
                async with sessions.begin() as session:
                    active_round = await session.get(LoopRound, round_row.round_id, with_for_update=True)
                    active_round.status = "running"
                    causal_ref = ContextRevisionRef(context_id=context_id, revision_id=causal_revision_id, generation=2, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id=f"causal-checkpoint-{suffix}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                    await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=causal_ref, content_hash="b" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.RUN_SETTLED, origin_id=causal_run_id, created_at=datetime.now(UTC)))
                    session.add(
                        DesktopRun(
                            run_id=causal_run_id,
                            task_id=context_id,
                            agent_id=f"main:{context_id}",
                            kind="main",
                            status="success",
                            origin="delegated_patrol",
                            execution_thread_id=f"thread-{suffix}",
                            context_revision_id=causal_revision_id,
                            final_checkpoint_id=f"causal-checkpoint-{suffix}",
                            directive_id=directive.directive_id,
                            loop_id=loop_id,
                            round_id=round_row.round_id,
                            workspace_result={"revision": 2, "files_changed": 1},
                            settled_at=datetime.now(UTC),
                        )
                    )
                return causal_run_id

            assert await LoopWaveDispatcher(sessions, causal_launch).dispatch(loop_id, round_row.round_id, 1) == (causal_run_id,)
            event = SimpleNamespace(event_id=uuid.uuid4().hex, run_id=causal_run_id, payload={})
            async with sessions.begin() as session:
                await LoopCoordinator(sessions).handle_run_settled(event, session)
            assert await FactProjector(sessions, _CausalCheckpointer()).project_loop(loop_id) >= 1
            async with sessions() as session:
                chain = await DirectiveCausalityQuery().read(session, committed.directive_ids[0])
            assert [item["state"] for item in chain["transitions"]][-5:] == ["authorized", "delivering", "delivered", "run_started", "settled"]
            assert {item["kind"] for item in chain["events"]} >= {
                "directive.authorized",
                "context.run.started",
                "context.tool.completed",
                "context.run.settled",
                "context.workspace.changed",
                "fact.upserted",
            }
            assert chain["runs"][-1]["run_id"] == causal_run_id

            next_snapshot = await service.get(loop_id)
            async with sessions() as session:
                next_round = await session.get(LoopRound, next_snapshot["current_round_id"])
            failing_intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"delivery-failure-{suffix}",
                loop_id=loop_id,
                loop_revision=next_snapshot["revision"],
                round_id=next_round.round_id,
                holder_id="patrol-1",
                grant_id=next_snapshot["grant"]["grant_id"],
                grant_revision=next_snapshot["authority_revision"],
                goal_revision=next_snapshot["goal_revision"],
                observed_frontier_hash=next_round.frontier_hash,
                observed_workspace_revision=next_round.workspace_revision,
                rationale="Attempt a delivery that will fail before Run start.",
                actions=({"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id, "message": "Continue."},),
            )
            failing = await LoopKernel(sessions).commit(failing_intent)
            async with sessions.begin() as session:
                directive = await session.get(LoopDirective, failing.directive_ids[0], with_for_update=True)
                directive.max_attempts = 1

            async def fail_launch(*_args):
                raise LookupError("target disappeared before Run start")

            with pytest.raises(LookupError):
                await LoopWaveDispatcher(sessions, fail_launch).dispatch(loop_id, next_round.round_id, 1)
            async with sessions() as session:
                failed_directive = await session.get(LoopDirective, failing.directive_ids[0])
                failed_history = tuple((await session.scalars(select(LoopDirectiveTransition).where(LoopDirectiveTransition.directive_id == failed_directive.directive_id).order_by(LoopDirectiveTransition.revision))).all())
            assert failed_directive.status == "blocked"
            assert failed_directive.lifecycle_state == "delivery_failed"
            assert "target disappeared" in failed_directive.terminal_reason
            assert failed_history[-1].to_state == "delivery_failed"
        finally:
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
            if loop is not None and loop.status in {"running", "paused", "waiting_user"}:
                await service.control(loop_id, "stop")
            await engine.dispose()

    asyncio.run(run())
