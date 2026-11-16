"""A failed access resume can be abandoned only after a clean, published replacement."""

import asyncio
import os
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.contexts.curation.models import (
    CurationLane,
    CurationProgram,
    PortfolioLaneCandidate,
    PortfolioRevision,
)
from backend.app.desktop.contexts.evolution import (
    ContextRevisionOriginKind,
    ContextRevisionProjectionStatus,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.conversations.interrupts import main_pending_interrupt
from backend.app.desktop.patrol.agent_loop.models import LoopPendingDecision
from backend.app.desktop.patrol.agent_loop.recovery.user_access_gate_recovery import (
    UserAccessGateRecovery,
)
from backend.app.desktop.persistence.models import DesktopThread, DesktopWorkspace
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_user_access_gate_recovery import _handled

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("failure", [None, "unchanged", "pending", "missing", "unreadable", "unrelated", "invalid",
    "wrong_target", "wrong_token", "unpublished_candidate", "unpublished_portfolio", "wrong_workspace", "wrong_origin", "missing_base"])
def test_retry_supersedes_only_a_proven_replaced_orphan(tmp_path, failure):
    async def scenario():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="orphan-gate", started_at=datetime.now(UTC))
        try:
            gate, original, resumed, wait, body, _ = await _handled(sessions, fixture)
            repository = ContextRevisionRepository()
            async with sessions.begin() as session:
                source = await session.get(type(resumed), resumed.run_id)
                source.status = "error"
                if failure == "missing_base":
                    source.context_revision_id = None
                source.loop_id, source.round_id = fixture['loop_id'], fixture['round_id']
                source.equipment = deepcopy(original.equipment)
                source.workspace_anchor = deepcopy(original.workspace_anchor)
                stored = await session.get(LoopPendingDecision, gate.pending_decision_id)
                stored.created_at = source.settled_at + timedelta(seconds=1)
                stored.payload = {**deepcopy(stored.payload), "source": {
                    "task_id": source.task_id, "run_id": source.run_id}, "recovery": {
                    **stored.payload["recovery"], "status": "orphaned"}}
                base = await repository.current(session, fixture["context_id"])
                portfolio_id = uuid.uuid4().hex
                replacement = base.model_copy(update={"ref": base.ref.model_copy(update={
                    "revision_id": uuid.uuid4().hex, "generation": base.ref.generation + 1,
                    "execution_thread_id": "shadow:replacement:" + uuid.uuid4().hex,
                    "checkpoint_id": "clean-checkpoint"}),
                    "sources": () if failure == "unrelated" else (ContextRevisionSourceContract(source=base.ref, position=0),),
                    "origin_kind": ContextRevisionOriginKind.ROOT if failure == "wrong_origin" else ContextRevisionOriginKind.CURATION, "origin_id": portfolio_id,
                    "projection_status": ContextRevisionProjectionStatus.ERROR if failure == "invalid" else base.projection_status})
                if failure != "unchanged":
                    await repository.insert(session, replacement)
                    await repository.switch_current(session, replacement.ref, base.ref)
                    lane = await session.scalar(select(CurationLane).where(CurationLane.managed_context_id == source.task_id))
                    assert lane is not None
                    program = await session.get(CurationProgram, lane.program_id)
                    program_id, lane_id = program.program_id, lane.lane_id
                    workspace_id = fixture["snapshot"]["workspace_id"]
                    if failure == "wrong_workspace":
                        workspace_id = uuid.uuid4().hex
                        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path / workspace_id), display_name="other"))
                        await session.flush()
                        program.workspace_id = workspace_id
                    generation = int(await session.scalar(select(func.max(PortfolioRevision.generation)).where(
                        PortfolioRevision.program_id == program_id)) or 0) + 1
                    session.add(PortfolioRevision(portfolio_revision_id=portfolio_id, program_id=program_id,
                        generation=generation, frontier_hash="a" * 64, base_program_revision=program.revision,
                        status="ready" if failure == "unpublished_portfolio" else "published",
                        published_at=None if failure == "unpublished_portfolio" else datetime.now(UTC)))
                    await session.flush()
                    if failure != "unpublished_portfolio":
                        program.current_portfolio_revision_id = portfolio_id
                    session.add(PortfolioLaneCandidate(candidate_id=uuid.uuid4().hex, portfolio_revision_id=portfolio_id,
                        lane_id=lane_id, action="update", purpose="replace orphaned execution",
                        target_context_id=uuid.uuid4().hex if failure == "wrong_target" else source.task_id,
                        base_context_revision_id=uuid.uuid4().hex if failure == "unrelated" else source.context_revision_id,
                        candidate_context_revision_id=uuid.uuid4().hex if failure == "wrong_token" else replacement.ref.revision_id,
                        status="ready" if failure == "unpublished_candidate" else "published"))

            class Checkpointer:
                async def aget_tuple(self, config):
                    if failure == "unreadable":
                        raise OSError("checkpoint unavailable")
                    if failure == "missing":
                        return None
                    if config['configurable']['thread_id'] == resumed.execution_thread_id:
                        return SimpleNamespace(config=deepcopy(config), checkpoint={"id": "old-checkpoint"},
                            pending_writes=[("tool", "__interrupt__", gate.payload["recovery"]["request"])])
                    writes = [("tool", "__interrupt__", gate.payload["recovery"]["request"])] if failure == "pending" else []
                    return SimpleNamespace(config=deepcopy(config), checkpoint={"id": "clean-checkpoint"}, pending_writes=writes)

            fixture["service"]._user_gate_recovery = UserAccessGateRecovery(Checkpointer())
            if failure is None:
                result = await fixture["service"].resolve_wait_request(fixture["loop_id"], wait["request_id"], body, actor_id="user")
                assert result["created"] and result["loop_status"] == "running"
                async with sessions() as session:
                    current = await repository.current(session, fixture["context_id"])
                    assert current.ref == replacement.ref
                    assert all(edge.source.context_id != original.task_id for edge in current.sources)
                    stored = await session.get(LoopPendingDecision, gate.pending_decision_id)
                    assert stored.status == "superseded"
                    task = await session.get(DesktopThread, fixture["context_id"])
                    assert await main_pending_interrupt(session, task, Checkpointer(), "access_review") is None
            else:
                with pytest.raises(HTTPException) as refused:
                    await fixture["service"].resolve_wait_request(fixture["loop_id"], wait["request_id"], body, actor_id="user")
                assert refused.value.status_code == 409
                async with sessions() as session:
                    assert (await session.get(LoopPendingDecision, gate.pending_decision_id)).status == "pending"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(scenario())
