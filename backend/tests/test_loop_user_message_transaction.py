"""本文件对外提供 D5 用户原文受理、冻结授权、真实交付和收口的 PostgreSQL 回归。

输入为逐用例隔离数据库、HTTP/服务请求、当前装备及可控模型执行；输出为幂等受理、原文和身份贯穿、禁止裸 Run 与角色缩权的断言。
具体工作流为真实受理后冻结 Observation，PortfolioPatrol 决策经 Kernel 授权、既有 dispatcher/启动边界/结算收口，
并验证并发重放、冲突和普通意见的类型边界；共享库中的其他队列不进入本次真实全库 Worker，可控模型不是 native 实际模型验收。
示例：pytest backend/tests/test_loop_user_message_transaction.py -q。
完成拒绝使用完整 RequestCompletionAction 合同及共享政策的封闭 user_message_pending 码，不以不完整 helper 夹具替代 API 类型。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.directive_equipment import resolve_context_equipment
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.agent_loop.user_message_delivery import LoopUserMessageDelivery, UserMessageDeliveryRejected
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository, InterventionTransitionRejected
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopUserIntent, LoopObservation, LoopRound, LoopInterventionTransition
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.patrol import PortfolioPatrol
from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent, RequestCompletionAction
from backend.app.desktop.agent_loop.service import AgentLoopService
from backend.app.desktop.context_curation.models import CurationLane
from backend.app.desktop.models import DesktopRun, MainRunCreate
from backend.app.desktop.routes import start_main_run
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_failure_repair_transaction import _launch, _finish
from backend.tests.test_round_task_progress import _Checkpointer
from focus.runtime.runs.schemas import RunStatus

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


async def authorize_message(sessions, fixture, claim, intent_id):
    observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], claim.round_id)

    async def model(frozen):
        return PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=f"user-decision:{claim.round_id}",
            loop_id=frozen.loop_id, loop_revision=frozen.loop_revision, round_id=frozen.round_id,
            holder_id=frozen.grant["holder_id"], grant_id=frozen.grant["grant_id"], grant_revision=frozen.grant["revision"],
            goal_revision=frozen.goal_revision, fencing_token=claim.fencing_token,
            observed_frontier_hash=frozen.observed_frontier_hash, observed_workspace_revision=frozen.workspace["revision"],
            observed_projection_sequence=frozen.projection_sequence, base_entity_revisions=frozen.base_entity_revisions,
            rationale="交付冻结用户原文", actions=({"action": "deliver_user_message", "intent_id": intent_id},))

    intent = await PortfolioPatrol(sessions, model).decide(observation, fixture["snapshot"]["holder_id"])
    result = await LoopKernel(sessions, require_fencing=True).commit(intent)
    assert result.status == "committed", result.reason
    return observation, result


def test_http_acceptance_concurrent_replay_conflict_and_restart_do_not_start_run(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="user-admission", started_at=datetime.now(UTC))
        service = fixture["service"]
        try:
            def forbid(*args, **kwargs):
                raise AssertionError("受理前不得调用 Main Run 入口")

            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(agent_loop_service=service,
                desktop_service=SimpleNamespace(start_main_run=forbid, notify_run_dispatch=forbid, run_by_idempotency=forbid))))
            request_id = uuid.uuid4().hex
            body = MainRunCreate(message="原文  保留\n完整", idempotency_key=request_id)
            responses = await asyncio.gather(*(start_main_run(fixture["context_id"], body, request, None) for _ in range(3)))
            assert responses[0] == responses[1] == responses[2]
            accepted = responses[0]
            assert accepted["delivery_state"] == "accepted" and accepted["run_id"] is None
            async with sessions() as session:
                intent = await session.get(LoopUserIntent, accepted["intent_id"])
                assert intent.content == body.message and intent.request_payload["request"] == {"message": body.message}
                round_row = await session.get(LoopRound, accepted["round_id"])
                assert round_row.status == "observed" and round_row.observation_id is None and round_row.decision_id is None
                assert await session.scalar(select(func.count()).select_from(DesktopRun).where(
                    DesktopRun.loop_id == fixture["loop_id"], DesktopRun.round_id == round_row.round_id)) == 0
            await service.control(fixture["loop_id"], "pause")
            recovered = AgentLoopService(sessions)
            assert await recovered.user_message(fixture["context_id"], body.message, request_id=request_id) == accepted
            assert (await recovered.get(fixture["loop_id"]))["status"] == "paused"
            with pytest.raises(HTTPException) as conflict:
                await recovered.user_message(fixture["context_id"], "改写", request_id=request_id)
            assert conflict.value.status_code == 409
            with pytest.raises(HTTPException) as conflict:
                await recovered.user_message("another-target", body.message, request_id=request_id)
            assert conflict.value.status_code == 409
        finally:
            await _stop(service, fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_user_message_real_observation_patrol_directive_dispatch_and_settlement(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="user-tx", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        try:
            accepted = await fixture["service"].user_message(fixture["context_id"], "原文\n不由模型改写")
            async with sessions.begin() as session:
                with pytest.raises(ValueError, match="冻结"):
                    await RunOwnershipPolicy().admit(session, DesktopRun(run_id=uuid.uuid4().hex,
                        task_id=fixture["context_id"], kind="main", loop_id=fixture["loop_id"],
                        round_id=accepted["round_id"], user_intent_id=accepted["intent_id"], equipment={"permissions": ["read"]}))
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "user-transaction")
            observation, result = await authorize_message(sessions, fixture, claim, accepted["intent_id"])
            assert observation.user_intents[0]["intent_kind"] == "direct_message"
            async with sessions() as session:
                directive = await session.get(LoopDirective, result.directive_ids[0])
                assert directive.content == "原文\n不由模型改写" and directive.actor_kind == "user"
                assert directive.origin_kind == "direct_user" and directive.correlation_id == accepted["intent_id"]
            record = await _launch(sessions, fixture, coordinator, claim)
            record.status = RunStatus.success
            await _finish(sessions, fixture, coordinator, record, [])
            async with sessions() as session:
                intent = await session.get(LoopUserIntent, accepted["intent_id"])
                assert intent.delivery_state == "settled" and intent.status == "addressed"
                run_row = await session.get(DesktopRun, intent.resulting_run_id)
                assert run_row.origin == "direct_user" and run_row.directive_id == directive.directive_id
                assert run_row.input_messages[0]["content"] == intent.content
                states = tuple(await session.scalars(select(LoopInterventionTransition.to_state).where(
                    LoopInterventionTransition.intent_id == intent.intent_id).order_by(LoopInterventionTransition.revision)))
                assert states == ("submitted", "accepted", "observed", "delivered", "run_started", "settled")
                prior = await session.get(LoopRound, accepted["round_id"])
                assert prior.observation_id is not None and prior.decision_id == result.decision_id and prior.status == "settled"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_server_equipment_and_message_lifecycle_are_role_and_type_bound(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="user-equipment", started_at=datetime.now(UTC))
        try:
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                loop.equipment = {"model_name": "model-original", "skills": ["vault"],
                    "permissions": ["read", "write", "host_command"], "access_mode": "danger-full-access"}
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id))
                grant.permission_scope = loop.equipment["permissions"]
                assert await resolve_context_equipment(session, loop, fixture["context_id"]) == {
                    **loop.equipment, "skill_snapshots": []}
                narrowed = await resolve_context_equipment(session, loop, fixture["context_id"],
                    overrides={"permissions": ["read"], "access_mode": "read-only"})
                assert narrowed["permissions"] == ["read"] and narrowed["access_mode"] == "read-only"
                lane = await session.scalar(select(CurationLane).where(CurationLane.program_id == loop.program_id))
                lane.lane_policy = {**lane.lane_policy, "workspace_mode": "read_only"}
                read_only = await resolve_context_equipment(session, loop, fixture["context_id"])
                assert read_only["permissions"] == ["read"] and read_only["access_mode"] == "read-only"
                with pytest.raises(ValueError, match="超出"):
                    await resolve_context_equipment(session, loop, fixture["context_id"], overrides={"permissions": ["write"]})
                opinion = LoopUserIntent(intent_id=uuid.uuid4().hex, loop_id=loop.loop_id, scope="portfolio",
                    content="审查布局", correlation_id=uuid.uuid4().hex, goal_revision=1, authority_revision=1)
                session.add(opinion)
                lifecycle = InterventionLifecycleRepository()
                await lifecycle.register(session, opinion)
                await lifecycle.transition(session, opinion.intent_id, "accepted")
                await lifecycle.transition(session, opinion.intent_id, "observed")
                with pytest.raises(InterventionTransitionRejected):
                    await lifecycle.transition(session, opinion.intent_id, "delivered")
                await lifecycle.transition(session, opinion.intent_id, "addressed")
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("tamper", ["unfrozen", "request", "target", "mission", "completion"])
def test_delivery_rejects_unfrozen_changed_or_unresolved_message(tmp_path, tamper):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="user-fence", started_at=datetime.now(UTC))
        try:
            accepted = await fixture["service"].user_message(fixture["context_id"], "保留原文")
            if tamper != "unfrozen":
                await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], accepted["round_id"])
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, accepted["round_id"])
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
                intent = await session.get(LoopUserIntent, accepted["intent_id"])
                if tamper == "request":
                    intent.request_payload = {**intent.request_payload, "request": {"message": "被修改的正文"}}
                elif tamper == "target":
                    observation = await session.get(LoopObservation, round_row.observation_id)
                    observation.envelope = {**observation.envelope, "user_intents": [
                        {**observation.envelope["user_intents"][0], "context_id": "other"}]}
                elif tamper == "mission":
                    intent.goal_revision += 1
                if tamper == "completion":
                    from backend.app.desktop.agent_loop.kernel import KernelRejected

                    with pytest.raises(KernelRejected, match="user_message_pending"):
                        await LoopKernel._validate_completion(session, loop, round_row,
                            RequestCompletionAction(action='request_completion', verification_id='unused',
                                final_context_ids=(fixture['context_id'],), final_slot_id='unused'), 0, None)
                else:
                    with pytest.raises(UserMessageDeliveryRejected):
                        await LoopUserMessageDelivery().validate(session, loop, round_row, grant, intent.intent_id)
                assert await session.scalar(select(func.count()).select_from(LoopDirective).where(
                    LoopDirective.loop_id == loop.loop_id, LoopDirective.origin_kind == "direct_user")) == 0
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
