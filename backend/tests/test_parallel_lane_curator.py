r"""本文件验证可选并行 Lane Curator 的 scope、attempt 留痕、无权输出和 Patrol 最终判断。

输入为 Patrol 主动分配的三个单 Lane assignment，其中两个合法、一个夹带 commit 请求；输出为
并行执行、两个 compiled candidate、一个错误 attempt，以及 accept/reject/retry 审查结果。具体工作流
为 Worker 只返回严格 proposal，Runner 持久化计算事实，Patrol review 决定是否采用但不提交状态。
示例：`pytest backend/tests/test_parallel_lane_curator.py`。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_curation import (
    CurationAttempt,
    CurationLane,
    CurationProgram,
    CurationProgramRepository,
    LaneCuratorAssignment,
    LaneCuratorRunner,
    PatrolCurationEngine,
    PatrolCurationError,
    PortfolioLaneAction,
    PortfolioLaneCandidate,
    PortfolioRepository,
    PortfolioRevision,
    WorkerReviewVerdict,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPayloadMode,
    ContextRevisionRef,
)
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Backend:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def curate(self, assignment: LaneCuratorAssignment) -> dict:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.03)
        self.active -= 1
        source = assignment.source_frontier[0].model_dump(mode="json")
        payload = {
            "request_id": assignment.request_id,
            "rationale": f"Curate {assignment.purpose}",
            "plan": {
                "action": "create",
                "lane_id": None,
                "purpose": assignment.purpose,
                "source_frontier": [source],
                "items": [
                    {
                        "type": "copy_message",
                        "source": {"source": source, "message_id": "goal"},
                    }
                ],
            },
        }
        if assignment.purpose == "Malicious":
            payload["publish_portfolio"] = True
        return payload


def _source() -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id="source-context",
        revision_id=uuid.uuid4().hex,
        generation=1,
        execution_thread_id="thread-source-context",
        checkpoint_ns="",
        checkpoint_id="checkpoint-source-1",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _assignment(
    candidate_id: str,
    request_id: str,
    purpose: str,
    source: ContextRevisionRef,
) -> LaneCuratorAssignment:
    source_payload = source.model_dump(mode="json")
    return LaneCuratorAssignment.model_validate(
        {
            "request_id": request_id,
            "candidate_id": candidate_id,
            "attempt_number": 1,
            "program_id": "program-placeholder",
            "portfolio_revision_id": "portfolio-placeholder",
            "action": "create",
            "purpose": purpose,
            "source_frontier": [source_payload],
            "evidence": {
                "sources": [
                    {
                        "source": source_payload,
                        "projection_hash": "a" * 64,
                        "content_hash": "b" * 64,
                        "messages": [
                            {
                                "ref": {"source": source_payload, "message_id": "goal"},
                                "role": "human",
                                "content": "Finish the task.",
                            }
                        ],
                    }
                ]
            },
            "lane_policy": {"retain": "goal"},
            "budget": {
                "max_source_messages": 10,
                "max_input_chars": 50_000,
                "max_output_chars": 20_000,
            },
        }
    )


def test_parallel_workers_return_candidates_while_patrol_owns_acceptance() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        programs = CurationProgramRepository()
        portfolios = PortfolioRepository()
        backend = _Backend()
        runner = LaneCuratorRunner(sessions, portfolios, backend, "curator-model")
        patrol = PatrolCurationEngine()
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-workers-{suffix}"
        source_context_id = "source-context"
        program_id = uuid.uuid4().hex
        portfolio_id = uuid.uuid4().hex
        purposes = ("Testing", "Architecture", "Malicious")
        lane_ids = [uuid.uuid4().hex for _ in purposes]
        candidate_ids = [uuid.uuid4().hex for _ in purposes]
        source = _source()
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="parallel workers",
                    )
                )
                await session.flush()
                session.add(
                    DesktopThread(
                        task_id=source_context_id,
                        workspace_id=workspace_id,
                        thread_id="thread-source-context",
                        title="Source",
                    )
                )
                await programs.create(session, workspace_id, program_id=program_id)
                portfolio = await portfolios.create_revision(
                    session,
                    program_id,
                    1,
                    [{"context_id": source_context_id, "revision_id": source.revision_id}],
                    "f" * 64,
                    0,
                    portfolio_revision_id=portfolio_id,
                )
                for lane_id, candidate_id, purpose in zip(
                    lane_ids, candidate_ids, purposes
                ):
                    lane = await programs.add_lane(
                        session,
                        program_id,
                        purpose,
                        lane_id=lane_id,
                    )
                    await portfolios.add_candidate(
                        session,
                        portfolio.portfolio_revision_id,
                        lane.lane_id,
                        PortfolioLaneAction.CREATE,
                        purpose,
                        candidate_id=candidate_id,
                    )

            assignments = tuple(
                _assignment(candidate_id, f"request-{index}", purpose, source).model_copy(
                    update={
                        "program_id": program_id,
                        "portfolio_revision_id": portfolio_id,
                    }
                )
                for index, (candidate_id, purpose) in enumerate(
                    zip(candidate_ids, purposes)
                )
            )
            outcomes = await runner.run_many(assignments)
            assert backend.max_active >= 2
            assert [outcome.candidate is not None for outcome in outcomes] == [True, True, False]
            assert outcomes[2].error is not None

            accepted = patrol.review_worker(
                outcomes[0], WorkerReviewVerdict.ACCEPT, "Use the testing candidate."
            )
            rejected = patrol.review_worker(
                outcomes[1], WorkerReviewVerdict.REJECT, "Architecture is premature."
            )
            retry = patrol.review_worker(
                outcomes[2], WorkerReviewVerdict.RETRY, "Remove unauthorized fields."
            )
            assert accepted.candidate is outcomes[0].candidate
            assert rejected.candidate is None
            assert retry.candidate is None
            with pytest.raises(PatrolCurationError):
                patrol.review_worker(
                    outcomes[2], WorkerReviewVerdict.ACCEPT, "Cannot accept an error."
                )

            async with sessions() as session:
                attempts = list(
                    (
                        await session.scalars(
                            select(CurationAttempt).where(
                                CurationAttempt.candidate_id.in_(candidate_ids)
                            )
                        )
                    ).all()
                )
                assert sorted(attempt.status for attempt in attempts) == [
                    "error",
                    "success",
                    "success",
                ]
                malicious = next(attempt for attempt in attempts if attempt.status == "error")
                assert malicious.raw_output["publish_portfolio"] is True
                assert "extra_forbidden" in (malicious.error or "")
                assert (await session.get(CurationProgram, program_id)).revision == 0
                lanes = list(
                    (
                        await session.scalars(
                            select(CurationLane).where(CurationLane.lane_id.in_(lane_ids))
                        )
                    ).all()
                )
                assert len(lanes) == 3
                assert all(lane.lifecycle == "active" for lane in lanes)
        finally:
            async with sessions.begin() as session:
                await session.execute(
                    delete(CurationAttempt).where(
                        CurationAttempt.candidate_id.in_(candidate_ids)
                    )
                )
                await session.execute(
                    delete(PortfolioLaneCandidate).where(
                        PortfolioLaneCandidate.candidate_id.in_(candidate_ids)
                    )
                )
                await session.execute(
                    delete(PortfolioRevision).where(
                        PortfolioRevision.portfolio_revision_id == portfolio_id
                    )
                )
                await session.execute(
                    delete(CurationLane).where(CurationLane.lane_id.in_(lane_ids))
                )
                await session.execute(
                    delete(CurationProgram).where(CurationProgram.program_id == program_id)
                )
                await session.execute(
                    delete(DesktopThread).where(
                        DesktopThread.task_id == source_context_id
                    )
                )
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id == workspace_id
                    )
                )
            await engine.dispose()

    asyncio.run(run())
