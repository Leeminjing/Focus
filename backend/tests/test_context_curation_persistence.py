r"""本文件验证多 Context 策展迁移、仓储、单发布者和多读者数据库约束。

输入为隔离 PostgreSQL 中的两个 Program、共享来源、稳定 Lane、Portfolio candidate 与 attempt；
输出为迁移结构、允许多订阅、拒绝双发布者、释放后再认领及 CAS 断言。具体工作流为经仓储创建
完整聚合，并在 savepoint 中触发 partial unique constraint。示例：
`pytest backend/tests/test_context_curation_persistence.py`。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine, delete, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_curation import (
    CurationAttempt,
    CurationLane,
    CurationLaneLifecycle,
    CurationProgram,
    CurationProgramRepository,
    CurationSourceSubscription,
    CurationWorkerKind,
    PortfolioLaneAction,
    PortfolioLaneCandidate,
    PortfolioRepository,
    PortfolioRevision,
    StaleCurationWrite,
)
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_multi_context_curation_forward_migration() -> None:
    url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "curation_programs",
            "curation_source_subscriptions",
            "curation_lanes",
            "curation_portfolio_revisions",
            "curation_portfolio_lane_candidates",
            "curation_attempts",
        } <= tables
        lane_indexes = {item["name"]: item for item in inspector.get_indexes("curation_lanes")}
        lane_columns = {item["name"] for item in inspector.get_columns("curation_lanes")}
        candidate_columns = {
            item["name"]
            for item in inspector.get_columns("curation_portfolio_lane_candidates")
        }
        assert {
            "current_source_frontier_hash",
            "current_semantic_fingerprint",
        } <= lane_columns
        assert {"source_frontier_hash", "semantic_fingerprint"} <= candidate_columns
        assert lane_indexes["uq_curation_lane_managed_publisher"]["unique"] is True
        assert "postgresql_where" in lane_indexes["uq_curation_lane_managed_publisher"][
            "dialect_options"
        ]
        program_fks = {
            item["name"] for item in inspector.get_foreign_keys("curation_programs")
        }
        assert "fk_curation_program_current_portfolio" in program_fks
        candidate_checks = {
            item["name"]
            for item in inspector.get_check_constraints(
                "curation_portfolio_lane_candidates"
            )
        }
        assert {
            "ck_curation_candidate_action",
            "ck_curation_candidate_status",
        } <= candidate_checks
    finally:
        engine.dispose()


def test_repositories_allow_many_readers_and_enforce_one_managed_context_publisher() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        programs = CurationProgramRepository()
        portfolios = PortfolioRepository()
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-curation-{suffix}"
        source_id = f"ctx-source-{suffix}"
        managed_id = f"ctx-managed-{suffix}"
        program_a_id = uuid.uuid4().hex
        program_b_id = uuid.uuid4().hex
        lane_a_id = uuid.uuid4().hex
        lane_b_id = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="curation persistence",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        DesktopThread(
                            task_id=context_id,
                            workspace_id=workspace_id,
                            thread_id=f"thread-{context_id}",
                            title=context_id,
                        )
                        for context_id in (source_id, managed_id)
                    ]
                )

            async with sessions.begin() as session:
                await programs.create(session, workspace_id, program_id=program_a_id)
                await programs.create(session, workspace_id, program_id=program_b_id)
                await programs.subscribe(
                    session, program_a_id, source_id, "primary", 0
                )
                await programs.subscribe(
                    session, program_b_id, source_id, "review", 0
                )
                lane_a = await programs.add_lane(
                    session,
                    program_a_id,
                    "Testing",
                    managed_context_id=managed_id,
                    lane_id=lane_a_id,
                )
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await programs.add_lane(
                            session,
                            program_b_id,
                            "Architecture",
                            managed_context_id=managed_id,
                            lane_id=lane_b_id,
                        )
                lane_a.lifecycle = CurationLaneLifecycle.RETIRED.value
                await session.flush()
                lane_b = await programs.add_lane(
                    session,
                    program_b_id,
                    "Architecture",
                    managed_context_id=managed_id,
                    lane_id=lane_b_id,
                )
                advanced = await programs.advance_publisher_epoch(session, lane_b_id, 1)
                assert advanced.publisher_epoch == 2
                with pytest.raises(StaleCurationWrite):
                    await programs.advance_publisher_epoch(session, lane_b_id, 1)

                portfolio = await portfolios.create_revision(
                    session,
                    program_b_id,
                    1,
                    [{"context_id": source_id, "revision_id": "r1"}],
                    "f" * 64,
                    0,
                )
                candidate = await portfolios.add_candidate(
                    session,
                    portfolio.portfolio_revision_id,
                    lane_b.lane_id,
                    PortfolioLaneAction.UPDATE,
                    "Architecture",
                    source_allocation=[{"context_id": source_id, "revision_id": "r1"}],
                )
                attempt = await portfolios.add_attempt(
                    session,
                    candidate.candidate_id,
                    1,
                    CurationWorkerKind.LANE_CURATOR,
                    "curator-model",
                    {"purpose": "Architecture"},
                )
                current = await portfolios.switch_current(
                    session,
                    program_b_id,
                    portfolio.portfolio_revision_id,
                    expected_current_id=None,
                    expected_program_revision=0,
                )
                assert current.current_portfolio_revision_id == portfolio.portfolio_revision_id
                assert current.revision == 1
                assert attempt.candidate_id == candidate.candidate_id
                with pytest.raises(StaleCurationWrite):
                    await portfolios.switch_current(
                        session,
                        program_b_id,
                        portfolio.portfolio_revision_id,
                        expected_current_id=None,
                        expected_program_revision=0,
                    )

            async with sessions() as session:
                subscriptions = (
                    await session.execute(
                        CurationSourceSubscription.__table__.select().where(
                            CurationSourceSubscription.source_context_id == source_id
                        )
                    )
                ).all()
                assert len(subscriptions) == 2
                assert await session.get(CurationProgram, program_a_id) is not None
                assert await session.get(PortfolioRevision, portfolio.portfolio_revision_id) is not None
        finally:
            async with sessions.begin() as session:
                await session.execute(delete(CurationAttempt))
                await session.execute(delete(PortfolioLaneCandidate))
                for program_id in (program_a_id, program_b_id):
                    program = await session.get(CurationProgram, program_id)
                    if program is not None:
                        program.current_portfolio_revision_id = None
                await session.flush()
                await session.execute(delete(PortfolioRevision))
                await session.execute(delete(CurationLane))
                await session.execute(delete(CurationSourceSubscription))
                await session.execute(
                    delete(CurationProgram).where(
                        CurationProgram.program_id.in_([program_a_id, program_b_id])
                    )
                )
                await session.execute(
                    delete(DesktopThread).where(
                        DesktopThread.task_id.in_([source_id, managed_id])
                    )
                )
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id == workspace_id
                    )
                )
            await engine.dispose()

    asyncio.run(run())
