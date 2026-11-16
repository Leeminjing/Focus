"""Terminal Loop convergence releases Round inputs without removing another owner."""

import asyncio

import pytest
from sqlalchemy import func, select

from backend.app.desktop.contexts.evolution.models import ContextInputSnapshot
from backend.app.desktop.contexts.evolution.repository import ContextRevisionRepository
from backend.app.desktop.patrol.agent_loop.lifecycle.terminal_lifecycle import (
    LoopTerminalLifecycle,
)
from backend.app.desktop.patrol.agent_loop.models import AgentLoop
from backend.tests.test_loop_governance_convergence import _loop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_terminal_convergence_releases_round_inputs_and_preserves_other_owners(tmp_path):
    async def exercise():
        async with _loop(tmp_path, "snap") as (sessions, fixture):
            repository = ContextRevisionRepository()
            async with sessions.begin() as session:
                current = await repository.current(session, fixture["context_id"])
                await repository.freeze(session, current.ref, owner_kind="round", owner_id=fixture["round_id"])
                await repository.freeze(session, current.ref, owner_kind="run", owner_id="independent-run")
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                await LoopTerminalLifecycle().finalize(session, loop, "stopped", "snapshot-test")
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ContextInputSnapshot).where(
                    ContextInputSnapshot.owner_kind == "round", ContextInputSnapshot.owner_id == fixture["round_id"],
                )) == 0
                assert await session.scalar(select(func.count()).select_from(ContextInputSnapshot).where(
                    ContextInputSnapshot.owner_kind == "run", ContextInputSnapshot.owner_id == "independent-run",
                )) == 1
                assert (await repository.current(session, fixture["context_id"])).ref == current.ref
            async with sessions.begin() as session:
                await repository.release(session, "run", "independent-run")

    asyncio.run(exercise())
