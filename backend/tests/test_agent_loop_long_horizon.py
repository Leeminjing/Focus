r"""本文件对外提供 Context-Governed Agent Loop 长时程确定性模拟回归。

输入为真实 PostgreSQL 中的 root/side Context revisions、用户委托、Patrol 决策、可选 Lane Curator
结果和独立 Completion Verifier 证据；输出为可重放的 round、directive、many-to-many revision、
用户改向、未采用分支与最终完成路径断言。具体工作流为先让 Patrol 针对重复失败发出普通
HumanMessage，再由用户提升权力版本并取消旧方向，随后保留多源探索、接收无权 Worker 结果，
最后只采用当前已发布 Portfolio 中的 Context 完成。示例：运行本测试并按 event cursor 重放轨迹。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import (
    AgentLoopService,
    CompletionEvidenceService,
    CompletionVerificationContract,
    CriterionVerification,
    LoopCreateRequest,
    LoopKernel,
    PatrolDecisionIntent,
)
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    LoopDirective,
    LoopRound,
    LoopWorkerRequest,
    MessageProvenance,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_long_horizon_trace_is_replayable_and_preserves_unadopted_synthesis(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-horizon-{suffix}"
        root_id = f"root-horizon-{suffix}"
        side_id = f"side-horizon-{suffix}"
        synthesis_id = f"synthesis-horizon-{suffix}"
        loop_id = uuid.uuid4().hex
        workspace_path = tmp_path / workspace_id
        workspace_path.mkdir()
        repository = ContextRevisionRepository()
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=str(workspace_path),
                        display_name="long-horizon",
                    )
                )
                await session.flush()
                refs = {}
                for context_id, purpose in (
                    (root_id, "primary implementation"),
                    (side_id, "independent failure analysis"),
                ):
                    thread_id = f"thread-{context_id}"
                    session.add(
                        DesktopThread(
                            task_id=context_id,
                            workspace_id=workspace_id,
                            thread_id=thread_id,
                            title=purpose,
                        )
                    )
                    await session.flush()
                    ref = ContextRevisionRef(
                        context_id=context_id,
                        revision_id=uuid.uuid4().hex,
                        generation=1,
                        execution_thread_id=thread_id,
                        checkpoint_ns="",
                        checkpoint_id=f"checkpoint-{context_id}",
                        payload_mode=ContextRevisionPayloadMode.DEFINITION,
                    )
                    refs[context_id] = ref
                    await repository.insert(
                        session,
                        ContextRevisionContract(
                            ref=ref,
                            authored_messages=(
                                {"role": "human", "content": purpose, "id": f"message-{context_id}"},
                            ),
                            execution_messages=(
                                {"role": "human", "content": purpose, "id": f"message-{context_id}"},
                            ),
                            content_hash=("a" if context_id == root_id else "b") * 64,
                            projection_status=ContextRevisionProjectionStatus.VALID,
                            origin_kind=ContextRevisionOriginKind.ROOT,
                            created_at=datetime.now(UTC),
                        ),
                    )
                    await repository.switch_current(session, ref, None)
                session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=root_id, agent_id=f"main:{root_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{root_id}", context_revision_id=refs[root_id].revision_id, settled_at=datetime.now(UTC)))

            service = AgentLoopService(sessions)
            started = await service.start(
                LoopCreateRequest(
                    loop_id=loop_id,
                    workspace_id=workspace_id,
                    initial_context_id=root_id,
                    initial_run_id=f"initial-{suffix}",
                    holder_id=f"patrol-{suffix}",
                    goal="Deliver the scoped change safely",
                    task_contract="Correct drift, investigate repeated failures, and pass tests",
                    acceptance_criteria=(
                        {
                            "criterion_id": "tests",
                            "text": "required tests pass",
                            "required": True,
                        },
                    ),
                    capabilities=(
                        "continue_context",
                        "request_lane_curator",
                        "request_completion_verifier",
                        "request_completion",
                    ),
                    context_scope=(root_id, side_id),
                    permission_scope=("read", "write"),
                    budgets={"max_rounds": 20, "max_contexts": 8, "max_providers": 3},
                )
            )
            first_round = await _round(sessions, started["current_round_id"])
            repeated_failure_intent = _intent(
                snapshot=started,
                round_row=first_round,
                rationale="Repeated fixes have not changed the failure; analyze before editing.",
                actions=(
                    {
                        "action": "continue_context",
                        "context_id": root_id,
                        "context_revision_id": refs[root_id].revision_id,
                        "message": "停止重复修改。只分析过去几轮为何没有改变失败结果。",
                    },
                ),
                key=f"failure-analysis-{suffix}",
            )
            first_result = await LoopKernel(sessions).commit(repeated_failure_intent)
            assert first_result.status == "committed"

            reprioritized = await service.override(
                loop_id,
                "Prioritize correctness over speculative optimization",
                "Keep the implementation lane authoritative and isolate alternative analysis",
                [{"criterion_id": "tests", "text": "required tests pass", "required": True}],
            )
            async with sessions.begin() as session:
                first_directive = await session.get(
                    LoopDirective,
                    first_result.directive_ids[0],
                )
                assert first_directive.status == "cancelled"
                side_membership = LoopContextMembership(
                    membership_id=uuid.uuid4().hex,
                    loop_id=loop_id,
                    context_id=side_id,
                    lane_id=None,
                    role="side_investigation",
                    required_barrier=False,
                )
                session.add(side_membership)
                session.add(
                    DesktopThread(
                        task_id=synthesis_id,
                        workspace_id=workspace_id,
                        thread_id=f"thread-{synthesis_id}",
                        title="many-to-many synthesis",
                    )
                )
                await session.flush()
                synthesis_ref = ContextRevisionRef(
                    context_id=synthesis_id,
                    revision_id=uuid.uuid4().hex,
                    generation=1,
                    execution_thread_id=f"thread-{synthesis_id}",
                    checkpoint_ns="",
                    checkpoint_id=f"checkpoint-{synthesis_id}",
                    payload_mode=ContextRevisionPayloadMode.DEFINITION,
                )
                await repository.insert(
                    session,
                    ContextRevisionContract(
                        ref=synthesis_ref,
                        sources=(
                            ContextRevisionSourceContract(source=refs[root_id], position=0),
                            ContextRevisionSourceContract(source=refs[side_id], position=1),
                        ),
                        authored_messages=(
                            {
                                "role": "human",
                                "content": "Synthesize implementation and independent failure analysis.",
                                "id": f"message-{synthesis_id}",
                            },
                        ),
                        execution_messages=(
                            {
                                "role": "human",
                                "content": "Synthesize implementation and independent failure analysis.",
                                "id": f"message-{synthesis_id}",
                            },
                        ),
                        content_hash="c" * 64,
                        projection_status=ContextRevisionProjectionStatus.VALID,
                        origin_kind=ContextRevisionOriginKind.CURATION,
                        created_at=datetime.now(UTC),
                    ),
                )
                await repository.switch_current(session, synthesis_ref, None)
                session.add(
                    LoopContextMembership(
                        membership_id=uuid.uuid4().hex,
                        loop_id=loop_id,
                        context_id=synthesis_id,
                        lane_id=None,
                        role="unadopted_synthesis",
                        required_barrier=False,
                    )
                )
                for position in range(2):
                    session.add(
                        LoopWorkerRequest(
                            worker_request_id=uuid.uuid4().hex,
                            loop_id=loop_id,
                            round_id=reprioritized["current_round_id"],
                            kind="lane_curator",
                            scope={"assignment": position},
                            status="success",
                            result={"proposal": f"candidate-{position}", "mutation": False},
                            completed_at=datetime.now(UTC),
                        )
                    )

            current_round = await _round(sessions, reprioritized["current_round_id"])
            verifier_id = uuid.uuid4().hex
            verification_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    LoopWorkerRequest(
                        worker_request_id=verifier_id,
                        loop_id=loop_id,
                        round_id=current_round.round_id,
                        kind="completion_verifier",
                        scope={"candidate_context_ids": [root_id]},
                    )
                )
            await CompletionEvidenceService(sessions).record(
                CompletionVerificationContract(
                    verification_id=verification_id,
                    loop_id=loop_id,
                    round_id=current_round.round_id,
                    goal_revision=reprioritized["goal_revision"],
                    frontier_hash=current_round.frontier_hash,
                    workspace_revision=current_round.workspace_revision,
                    criteria=(
                        CriterionVerification(
                            criterion_id="tests",
                            status="satisfied",
                            evidence=({"kind": "fact", "source_id": f"initial-{suffix}", "summary": "The deterministic suite passed."},),
                            explanation="The required deterministic suite passed.",
                        ),
                    ),
                    conclusion="satisfied",
                ),
                verifier_id,
            )
            async with sessions() as session:
                slot = await session.scalar(
                    select(WorkspaceSlot).where(
                        WorkspaceSlot.workspace_id == workspace_id,
                        WorkspaceSlot.kind == "authoritative",
                    )
                )
            completion = _intent(
                snapshot=reprioritized,
                round_row=current_round,
                rationale="Independent evidence satisfies the contract; side synthesis remains unadopted.",
                actions=(
                    {
                        "action": "request_completion",
                        "verification_id": verification_id,
                        "final_context_ids": [root_id],
                        "final_slot_id": slot.slot_id,
                    },
                ),
                key=f"complete-{suffix}",
            )
            completed = await LoopKernel(sessions).commit(completion)
            assert completed.status == "committed"

            events = await service.events(loop_id)
            assert [event["cursor"] for event in events] == list(range(1, len(events) + 1))
            assert len({event["event_id"] for event in events}) == len(events)
            assert {"LoopStarted", "MissionRevisionActivated", "LoopDecisionCommitted"}.issubset(
                {event["type"] for event in events}
            )
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                provenance = list(
                    (
                        await session.scalars(
                            select(MessageProvenance)
                            .join(
                                LoopDirective,
                                LoopDirective.directive_id == MessageProvenance.directive_id,
                            )
                            .where(LoopDirective.loop_id == loop_id)
                        )
                    ).all()
                )
                workers = list(
                    (
                        await session.scalars(
                            select(LoopWorkerRequest).where(
                                LoopWorkerRequest.loop_id == loop_id
                            )
                        )
                    ).all()
                )
                assert loop.status == "completed"
                assert loop.final_result["final_path"][0]["context_id"] == root_id
                assert synthesis_id in {
                    item["context_id"] for item in loop.final_result["unadopted_lanes"]
                }
                assert len(provenance) == 1
                assert provenance[0].source_kind == "delegated_patrol"
                assert len([worker for worker in workers if worker.kind == "lane_curator"]) == 2
                assert len([worker for worker in workers if worker.kind == "completion_verifier"]) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _round(sessions, round_id: str) -> LoopRound:
    async with sessions() as session:
        return await session.get(LoopRound, round_id)


def _intent(
    *,
    snapshot: dict,
    round_row: LoopRound,
    rationale: str,
    actions: tuple[dict, ...],
    key: str,
) -> PatrolDecisionIntent:
    return PatrolDecisionIntent(
        decision_id=uuid.uuid4().hex,
        idempotency_key=key,
        loop_id=snapshot["loop_id"],
        loop_revision=snapshot["revision"],
        round_id=round_row.round_id,
        holder_id=snapshot["holder_id"],
        grant_id=snapshot["grant"]["grant_id"],
        grant_revision=snapshot["authority_revision"],
        goal_revision=snapshot["goal_revision"],
        observed_frontier_hash=round_row.frontier_hash,
        observed_workspace_revision=round_row.workspace_revision,
        rationale=rationale,
        actions=actions,
    )
