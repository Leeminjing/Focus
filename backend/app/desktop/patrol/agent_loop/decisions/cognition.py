"""本文件对外提供 preempt_observation、require_observation 与认知抢占通知。

输入为已按 Loop→Round 锁定的事务和被替换/当前 Observation identity；输出为精确取消负载。
工作流为提升既有 fence、作废旧 lease，将旧会话与尝试标为 superseded，并在既有 outbox 保存
取消意图；提交后的通知及运行时重放只取消这些身份，真实请求清理和用量结算仍由原执行器完成。
require_observation 校验写回者所属不可变版本，旧回调仅能保留自己的审计。
示例：await preempt_observation(db, loop, round, old_id, new_id, 2)。
"""

from datetime import UTC, datetime
import uuid
import weakref

from sqlalchemy import delete, func, select

from backend.app.desktop.patrol.agent_loop.models import (
    LoopCoordinatorFence, LoopCoordinatorLease, LoopEventOutbox, LoopObservation,
    LoopPatrolAttempt, LoopRound, LoopWorkerRequest,
)
from backend.app.desktop.patrol.agent_loop.sessions.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession


_runtimes = weakref.WeakSet()


def register_runtime(runtime):
    _runtimes.add(runtime)


async def notify_preemption(payload):
    for runtime in tuple(_runtimes):
        await runtime.preempt_observation(payload)


async def require_observation(session, round_id, observation_id, generation=None):
    row = await session.get(LoopRound, round_id)
    if row is None or row.observation_id != observation_id:
        raise ValueError("observation_superseded")
    observation = await session.get(LoopObservation, observation_id) if observation_id else None
    if generation is not None and observation is not None and observation.generation != generation:
        raise ValueError("observation_generation_changed")
    return row


async def preempt_observation(session, loop, round_row, old_observation_id, new_observation_id, generation):
    if round_row.decision_id is not None:
        raise ValueError("成功授权的 Round 不能替换认知 Observation")
    if old_observation_id:
        from backend.app.desktop.patrol.agent_loop.task_progress.work_queue import TaskProgressQueue

        await TaskProgressQueue().supersede_observation(session, loop.loop_id, old_observation_id)
    fence = await session.get(LoopCoordinatorFence, round_row.round_id, with_for_update=True)
    if fence is None:
        fence = LoopCoordinatorFence(round_id=round_row.round_id, fencing_token=1)
        session.add(fence)
    else:
        fence.fencing_token += 1
    await session.execute(delete(LoopCoordinatorLease).where(LoopCoordinatorLease.round_id == round_row.round_id))
    payload = {"loop_id": loop.loop_id, "round_id": round_row.round_id,
        "old_observation_id": old_observation_id, "observation_id": new_observation_id,
        "observation_generation": generation, "fencing_token": fence.fencing_token}
    now = datetime.now(UTC)
    sessions = tuple(await session.scalars(select(LoopPatrolSession).where(
        LoopPatrolSession.round_id == round_row.round_id, LoopPatrolSession.observation_id == old_observation_id,
        LoopPatrolSession.status == "active").with_for_update())) if old_observation_id else ()
    from backend.app.desktop.patrol.agent_loop.sessions.patrol_session_repository import PatrolSessionRepository
    from backend.app.desktop.patrol.agent_loop.sessions.patrol_session_state import PatrolActivity, PatrolPhase

    for row in sessions:
        await PatrolSessionRepository().transition(session, row.session_id, PatrolPhase.SUPERSEDED,
            PatrolActivity(summary="新输入已替换本观察的认知资格"),
            terminal_outcome={"status": "superseded", "observation_id": old_observation_id,
                              "replaced_by": new_observation_id})
    workers = tuple(await session.scalars(select(LoopWorkerRequest).where(
        LoopWorkerRequest.round_id == round_row.round_id, LoopWorkerRequest.observation_id == old_observation_id,
        LoopWorkerRequest.status.in_(("pending", "running"))).with_for_update())) if old_observation_id else ()
    for row in workers:
        pending = row.status == "pending"
        row.status = "superseded"
        row.result = {**(row.result or {}), "reason": "observation_superseded"}
        if pending:
            row.completed_at = now
    from backend.app.desktop.patrol.agent_loop.decisions.evidence_jobs import EvidenceJob

    jobs = await session.scalars(select(EvidenceJob).where(
        EvidenceJob.observation_id == old_observation_id,
        EvidenceJob.status.in_(("pending", "running"))).with_for_update()) if old_observation_id else ()
    for job in jobs:
        job.status = "superseded"
        job.fencing_token += 1
    assignments = tuple(await session.scalars(select(LoopCuratorAssignment).where(
        LoopCuratorAssignment.session_id.in_([row.session_id for row in sessions]),
        LoopCuratorAssignment.state.in_(("queued", "reading", "analyzing", "proposed"))).with_for_update())) if sessions else ()
    from backend.app.desktop.patrol.agent_loop.curation.curator_assignments import CuratorAssignmentRepository

    for row in assignments:
        await CuratorAssignmentRepository().transition(session, row.assignment_id, "cancelled",
            "原观察已被新输入替换", failure="observation_superseded")
    attempts = tuple(await session.scalars(select(LoopPatrolAttempt).where(
        LoopPatrolAttempt.round_id == round_row.round_id, LoopPatrolAttempt.observation_id == old_observation_id,
        LoopPatrolAttempt.status.in_(("pending", "running"))).with_for_update())) if old_observation_id else ()
    for row in attempts:
        row.status = "superseded"
        row.error = "observation_superseded"
    from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopContextExpansion
    from backend.app.desktop.patrol.agent_loop.context_expansion.repository import ContextExpansionRepository

    expansions = tuple(await session.scalars(select(LoopContextExpansion).where(
        LoopContextExpansion.round_id == round_row.round_id).with_for_update()))
    repository = ContextExpansionRepository()
    for row in expansions:
        if not repository.is_terminal(row.state):
            await repository.transition(session, row.expansion_id, "superseded", "原观察已被新输入替换")
    payload.update(session_ids=[row.session_id for row in sessions],
        worker_request_ids=[row.worker_request_id for row in workers],
        attempt_ids=[row.patrol_attempt_id for row in attempts],
        requires_cleanup=bool(sessions or workers or attempts))
    sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(
        LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
    session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence,
        event_type="loop.cognition.preempted", payload=payload,
        idempotency_key=f"cognition-preempted:{new_observation_id}"))
    return payload
