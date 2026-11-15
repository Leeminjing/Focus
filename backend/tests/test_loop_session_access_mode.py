"""Keep explicit user file-mode changes in sync with Workspace Patrol authority."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from backend.app.desktop.conversations.service import ConversationService
from backend.app.desktop.patrol.agent_loop.models import AgentLoop
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest
from backend.app.desktop.persistence.models import DesktopThread
from backend.tests.test_workspace_patrol import _setup


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("mode", ["read-only", "danger-full-access"])
def test_primary_picker_updates_loop_authority_and_preserves_pause(tmp_path, mode):
    async def scenario():
        engine, sessions, workspace_id, patrol = await _setup(tmp_path)
        try:
            receipt = await patrol.submit(workspace_id, PatrolInputRequest(
                submission_id=uuid.uuid4().hex, content="research", access_mode="workspace-write"))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, receipt["loop_id"])
                loop.status = "paused"
                context_id = loop.initial_context_id
                original_revision = loop.authority_revision
                original_round = loop.current_round_id
            conversations = ConversationService(SimpleNamespace(session_factory=sessions))
            await conversations.set_session_access_mode(context_id, mode)
            async with sessions() as session:
                loop = await session.get(AgentLoop, receipt["loop_id"])
                context = await session.get(DesktopThread, context_id)
                assert loop.equipment["access_mode"] == context.ui_state["access_mode"] == mode
                assert loop.status == "paused"
                assert loop.authority_revision == original_revision + 1
                assert loop.current_round_id != original_round
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_new_workspace_input_changes_mode_but_old_request_replay_does_not(tmp_path):
    async def scenario():
        engine, sessions, workspace_id, patrol = await _setup(tmp_path)
        try:
            original = PatrolInputRequest(submission_id=uuid.uuid4().hex,
                content="research", access_mode="workspace-write")
            receipt = await patrol.submit(workspace_id, original)
            await patrol.submit(workspace_id, PatrolInputRequest(submission_id=uuid.uuid4().hex,
                content="continue", access_mode="danger-full-access"))
            await patrol.submit(workspace_id, original)
            async with sessions() as session:
                loop = await session.get(AgentLoop, receipt["loop_id"])
                context = await session.get(DesktopThread, loop.initial_context_id)
                assert loop.equipment["access_mode"] == context.ui_state["access_mode"] == "danger-full-access"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_workspace_primary_decision_cannot_silently_change_picker_mode(tmp_path):
    from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import validate_context_permissions
    from backend.app.desktop.patrol.agent_loop.schemas import ContextPermission

    async def scenario():
        engine, sessions, workspace_id, patrol = await _setup(tmp_path)
        try:
            receipt = await patrol.submit(workspace_id, PatrolInputRequest(
                submission_id=uuid.uuid4().hex, content="research", access_mode="danger-full-access"))
            async with sessions() as session:
                loop = await session.get(AgentLoop, receipt['loop_id'])
                observation = SimpleNamespace(portfolio_frontier=[{'context_id': loop.initial_context_id}])
                proposal = SimpleNamespace(actions=(), context_permissions=(ContextPermission(
                    context_id=loop.initial_context_id, access_mode='workspace-write'),))
                with pytest.raises(ValueError, match='界面选择'):
                    await validate_context_permissions(session, loop, observation, proposal)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize('ingress', ['picker', 'input'])
def test_mode_change_preserves_outstanding_approval_and_its_wait_round(tmp_path, ingress):
    from backend.app.desktop.patrol.agent_loop.governance.gates import PendingDecisionProjector
    from backend.app.desktop.patrol.agent_loop.service import AgentLoopService
    from backend.app.desktop.patrol.agent_loop.models import LoopPendingDecision

    async def scenario():
        engine, sessions, workspace_id, patrol = await _setup(tmp_path)
        try:
            receipt = await patrol.submit(workspace_id, PatrolInputRequest(
                submission_id=uuid.uuid4().hex, content='research', access_mode='workspace-write'))
            gate = await PendingDecisionProjector(sessions).project(receipt['loop_id'], {'type':'access_approval'})
            controls = AgentLoopService(sessions)
            before = await controls.active_wait_request(receipt['loop_id'])
            async with sessions() as session:
                context_id = (await session.get(AgentLoop, receipt['loop_id'])).initial_context_id
            if ingress == 'picker':
                await ConversationService(SimpleNamespace(session_factory=sessions)).set_session_access_mode(
                    context_id, 'danger-full-access')
            else:
                await patrol.submit(workspace_id, PatrolInputRequest(submission_id=uuid.uuid4().hex,
                    content='continue', access_mode='danger-full-access'))
            after = await controls.active_wait_request(receipt['loop_id'])
            async with sessions() as session:
                loop = await session.get(AgentLoop, receipt['loop_id'])
                assert loop.status == 'waiting_user'
                assert loop.current_round_id == after['round_id'] == before['round_id']
                assert after['request_id'] == before['request_id']
                assert (await session.get(LoopPendingDecision, gate.pending_decision_id)).status == 'pending'
        finally:
            await engine.dispose()
    asyncio.run(scenario())
