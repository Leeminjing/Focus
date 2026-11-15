"""Apply an explicit user's file mode to Workspace Patrol in the caller's transaction.

Old decisions and Runs remain immutable. A changed mode revokes their grant,
converges old work, and starts a fresh observation unless a user wait is open.
Changing mode never approves an outstanding tool request or resumes a pause.
"""

from datetime import UTC, datetime
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopRound
from backend.app.desktop.persistence.models import DesktopThread
from focus.security.policy import AccessMode


async def apply_workspace_file_mode(session, loop, mode):
    from backend.app.desktop.patrol.agent_loop.compression_authority.repository import CompressionAuthorityRepository
    from backend.app.desktop.patrol.agent_loop.inputs.user_messages import LoopUserMessageAdmission
    from backend.app.desktop.patrol.agent_loop.journal.lifecycle_events import LoopLifecycleEventRecorder
    from backend.app.desktop.patrol.agent_loop.journal.round_events import RoundStateEventRecorder
    from backend.app.desktop.patrol.agent_loop.lifecycle.runtime_convergence import LoopRuntimeConvergence
    from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import create_observation_round
    from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest

    mode = str(AccessMode(mode))
    primary = await session.get(DesktopThread, loop.initial_context_id, with_for_update=True)
    if primary is not None:
        primary.ui_state = {**(primary.ui_state or {}), "access_mode": mode}
    if (loop.equipment or {}).get("access_mode") == mode:
        return ()
    grant = await session.scalar(select(LoopDelegationGrant).where(
        LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"
    ).with_for_update())
    if grant is None:
        raise HTTPException(409, "Loop 当前授权已撤销，不能通过文件模式选择恢复授权")
    if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
        raise HTTPException(409, "Loop 当前授权已过期")
    prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
    wait = await session.scalar(select(LoopWaitRequest).where(
        LoopWaitRequest.loop_id == loop.loop_id, LoopWaitRequest.status == "open",
        LoopWaitRequest.scope["information_only"].astext.is_distinct_from("true"),
    ).with_for_update())
    loop.equipment = {**loop.equipment, "access_mode": mode}
    loop.revision += 1
    loop.authority_revision += 1
    grant.status, grant.revoked_at = "revoked", datetime.now(UTC)
    await session.flush()
    session.add(LoopUserMessageAdmission._renewed_grant(loop, grant))
    cancelled = await LoopRuntimeConvergence().converge(
        session, loop, "file_mode_changed", preserve_waits=True)
    await CompressionAuthorityRepository().supersede(session, loop.loop_id, "file_mode_changed")
    if wait is None:
        round_row = await create_observation_round(session, loop, prior, "0" * 64)
        session.add(round_row)
        loop.current_round_id = round_row.round_id
        await RoundStateEventRecorder().record(session, round_row)
    await LoopLifecycleEventRecorder().record(session, loop)
    return cancelled


async def workspace_loop_for_primary(session, context_id):
    return await session.scalar(select(AgentLoop).where(
        AgentLoop.initial_context_id == context_id,
        AgentLoop.interaction_mode == "workspace_patrol",
        AgentLoop.status.in_(("running", "paused", "waiting_user")),
    ).order_by(AgentLoop.created_at.desc()).limit(1).with_for_update())


def notify_cancelled_runs(desktop, run_ids):
    manager = getattr(desktop, "run_manager", None)
    if manager is not None:
        for run_id in run_ids:
            manager.cancel(run_id, action="interrupt")
