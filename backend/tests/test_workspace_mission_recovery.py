r"""Workspace Patrol 的 Mission 证据、具体澄清原因和显式恢复回归。

输入为已准备的工作区 Context、结构化 Mission 和真实等待记录；输出为错误澄清被拒绝、
历史载荷可读及恢复事务幂等的断言。使用共享 Workspace Patrol 测试状态，不再启动旧 Context Loop。
示例：`python -m pytest backend/tests/test_workspace_mission_recovery.py -q`。
"""

from __future__ import annotations

from backend.tests.workspace_loop_support import _seed_greeting_loop

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop import LoopKernel, PatrolDecisionIntent
from backend.app.desktop.patrol.agent_loop.inputs.clarification_admission import (
    ClarificationAdmissionPolicy,
    ClarificationFacts,
    ClarificationRejected,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_retrieval import (
    PlanningRetrievalSession,
)
from backend.app.desktop.patrol.agent_loop.missions.mission_models import LoopMissionRevision
from backend.app.desktop.patrol.agent_loop.missions.mission_projection import EffectiveMissionProjector
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopGoalRevision, LoopRound, LoopWorkerRequest
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import PatrolDecisionProposal
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import current_frontier_hash
from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope, ResumeWithCurrentMissionRequest, WaitEvidenceIdentity, WaitForUserAction
from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.patrol.agent_loop.waits.wait_requests import (
    LoopWaitRequestFactory,
    LoopWaitRequestService,
)
from backend.app.desktop.patrol.agent_loop.workers.runtime import LoopWorkerRuntime
from backend.app.desktop.contexts.curation.contracts import MissionEvidenceRef
from backend.app.desktop.persistence.models import DesktopRun
from backend.tests.config_helpers import app_config_for


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_legacy_observation_wait_and_retrieval_payloads_remain_readable() -> None:
    observation_payload = {
        "loop_id": "legacy-loop",
        "loop_revision": 1,
        "round_id": "legacy-round",
        "goal_revision": 1,
        "authority_revision": 1,
        "observed_frontier_hash": "a" * 64,
        "goal": {"goal": "旧目标", "task_contract": "原始\n边界", "acceptance_criteria": [{"criterion_id": "tests", "text": "测试通过"}]},
        "grant": {},
        "portfolio_frontier": [],
        "workspace": {"revision": 1},
        "budget": {},
    }
    retrieval_payload = {
        "session_id": "b" * 64,
        "observation_hash": "c" * 64,
        "frontier_hash": "a" * 64,
        "catalog_id": "d" * 64,
        "authorized_index_ids": ["e" * 64],
        "planner_version": "legacy-planner-v1",
        "retrieval_version": "legacy-retrieval-v1",
        "budget": {"max_queries": 1, "max_candidates": 1, "max_exact_reads": 1, "max_model_calls": 1, "max_tokens": 100},
    }
    originals = json.loads(json.dumps([observation_payload, retrieval_payload], ensure_ascii=False))

    observation = LoopObservationEnvelope.model_validate(observation_payload)
    retrieval = PlanningRetrievalSession.model_validate(retrieval_payload)
    wait = WaitForUserAction.model_validate({"action": "wait_for_user", "reason": "旧版原因"})
    evidence = MissionEvidenceRef.model_validate({"loop_id": "legacy-loop", "goal_revision": 1, "item_id": "tests", "content_hash": "f" * 64})

    assert observation.goal == observation_payload["goal"]
    assert retrieval.schema_version == "retrieval-session-v1"
    assert wait.reason == "旧版原因"
    assert evidence.item_id == "tests"
    assert [observation_payload, retrieval_payload] == originals


def test_clarification_policy_requires_current_specific_blocker() -> None:
    policy = ClarificationAdmissionPolicy()
    facts = ClarificationFacts(
        mission_revision=3,
        outcome="交付可运行插件",
        check_ids=frozenset({"build"}),
        capabilities=frozenset({"continue_context"}),
        pending_gate_ids=frozenset({"approval-1"}),
        budget_exhaustions=frozenset({"model_calls_budget"}),
        external_blockers=frozenset(),
        safe_continuation=False,
        permission_needs=frozenset({"write"}),
    )
    def action(cause: str, kind: str, reference_id: str, revision: int = 3) -> WaitForUserAction:
        return WaitForUserAction(
            action="wait_for_user",
            reason="需要具体用户决定",
            cause=cause,
            required_input="请决定是否继续",
            evidence_identity=WaitEvidenceIdentity(kind=kind, reference_id=reference_id, revision=revision),
        )

    policy.validate(action("human_gate", "gate", "approval-1"), facts)
    policy.validate(action("permission", "capability", "write"), facts)
    policy.validate(action("budget", "budget", "model_calls_budget"), facts)
    with pytest.raises(ClarificationRejected, match="已包含最终结果"):
        policy.validate(action("missing_goal", "mission", "outcome"), facts)
    with pytest.raises(ClarificationRejected, match="已过期"):
        policy.validate(action("human_gate", "gate", "approval-1", revision=2), facts)
    with pytest.raises(ClarificationRejected, match="不一致"):
        policy.validate(action("human_gate", "gate", "invented"), facts)
    with pytest.raises(ClarificationRejected, match="不一致"):
        policy.validate(action("permission", "capability", "arbitrary"), facts)

    safely_continuable = ClarificationFacts(
        mission_revision=3,
        outcome=facts.outcome,
        check_ids=facts.check_ids,
        capabilities=facts.capabilities,
        pending_gate_ids=frozenset(),
        budget_exhaustions=frozenset(),
        external_blockers=frozenset(),
        safe_continuation=True,
    )
    with pytest.raises(ClarificationRejected, match="未决输入请求"):
        policy.validate(action("missing_input", "mission", "missing:some_detail"), safely_continuable)
    with pytest.raises(ClarificationRejected, match="未决输入请求"):
        policy.validate(action("missing_input", "mission", "missing:some_detail"), facts)
    input_pending = ClarificationFacts(
        mission_revision=3,
        outcome=facts.outcome,
        check_ids=facts.check_ids,
        capabilities=facts.capabilities,
        pending_gate_ids=frozenset({"input-1"}),
        budget_exhaustions=frozenset(),
        external_blockers=frozenset(),
        safe_continuation=False,
        missing_input_ids=frozenset({"input-1"}),
    )
    policy.validate(action("missing_input", "input", "input-1"), input_pending)
    with pytest.raises(ClarificationRejected, match="未决输入请求"):
        policy.validate(action("missing_input", "input", "input-fabricated"), input_pending)


def test_new_patrol_wait_requires_typed_cause_while_legacy_audit_remains_readable() -> None:
    legacy = WaitForUserAction.model_validate({"action": "wait_for_user", "reason": "旧版原因"})
    assert legacy.cause is None
    with pytest.raises(ValidationError, match="cause"):
        PatrolDecisionProposal.model_validate({
            "rationale": "尝试澄清", "mission_references": ({"role": "outcome", "reference_id": "outcome"},),
            "actions": ({"action": "wait_for_user", "reason": "请补充目标"},),
        })


def test_worker_evidence_uses_the_canonical_structured_and_legacy_mission(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            runtime = LoopWorkerRuntime(sessions, app_config_for("worker-test", None))
            structured_fixture = await _seed_greeting_loop(sessions, tmp_path)
            structured_request = LoopWorkerRequest(
                worker_request_id=uuid.uuid4().hex, loop_id=structured_fixture["loop_id"],
                round_id=structured_fixture["snapshot"]["current_round_id"], kind="lane_curator", scope={},
            )
            async with sessions.begin() as session:
                session.add(structured_request)
            structured_request = (await runtime._claim_many(1, structured_fixture["loop_id"]))[0]
            structured_payload, _, _ = await runtime._evidence(structured_request)
            async with sessions() as session:
                structured = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == structured_fixture["loop_id"]))
            assert structured_payload["mission"] == EffectiveMissionProjector.from_rows(structured=structured, legacy=None).model_payload()
            assert structured_payload["mission"]["section_hashes"]["outcome"]
            assert "goal" not in structured_payload

            legacy_fixture = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                legacy_structured = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == legacy_fixture["loop_id"]))
                await session.delete(legacy_structured)
            legacy_request = LoopWorkerRequest(
                worker_request_id=uuid.uuid4().hex, loop_id=legacy_fixture["loop_id"],
                round_id=legacy_fixture["snapshot"]["current_round_id"], kind="lane_curator", scope={},
            )
            async with sessions.begin() as session:
                session.add(legacy_request)
            legacy_request = (await runtime._claim_many(1, legacy_fixture["loop_id"]))[0]
            legacy_payload, _, _ = await runtime._evidence(legacy_request)
            async with sessions() as session:
                legacy = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == legacy_fixture["loop_id"]))
            assert legacy_payload["mission"] == EffectiveMissionProjector.from_rows(structured=None, legacy=legacy).model_payload()
            assert legacy_payload["mission"]["source_format"] == "legacy_adapter"
            assert "legacy_source" not in legacy_payload["mission"]
            assert "goal" not in legacy_payload
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_complete_new_mission_cannot_be_replaced_by_missing_goal_clarification(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            snapshot = fixture["snapshot"]
            loop_id = fixture["loop_id"]
            run_id = fixture["run_id"]
            async with sessions() as session:
                original = await session.get(DesktopRun, run_id)
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                assert original.input_messages == [{"role": "user", "content": "你好"}]
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"false-missing-goal:{loop_id}",
                loop_id=loop_id,
                loop_revision=snapshot["revision"],
                round_id=round_row.round_id,
                holder_id="patrol-test",
                grant_id=snapshot["grant"]["grant_id"],
                grant_revision=snapshot["authority_revision"],
                goal_revision=snapshot["goal_revision"],
                observed_frontier_hash=round_row.frontier_hash,
                observed_workspace_revision=round_row.workspace_revision,
                rationale="没有派生机会，所以请用户重新给目标",
                actions=({
                    "action": "wait_for_user",
                    "reason": "请用户重新说明要做什么",
                    "cause": "missing_goal",
                    "required_input": "请重新给出最终结果",
                    "evidence_identity": {"kind": "mission", "reference_id": "outcome", "revision": snapshot["goal_revision"]},
                },),
            )
            result = await LoopKernel(sessions).commit(intent)
            async with sessions() as session:
                waits = tuple((await session.scalars(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop_id, LoopWaitRequest.status == "open", LoopWaitRequest.kind == "clarification"))).all())
            assert result.status == "rejected", "完整 Mission 不能被缺失目标的虚假澄清取代"
            assert waits == ()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_stale_frontier_wait_proposal_never_opens_clarification(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            snapshot = fixture["snapshot"]
            async with sessions() as session:
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                intent = PatrolDecisionIntent(
                    decision_id=uuid.uuid4().hex,
                    idempotency_key=f"stale-wait:{fixture['loop_id']}",
                    loop_id=fixture["loop_id"],
                    loop_revision=snapshot["revision"],
                    round_id=round_row.round_id,
                    holder_id="patrol-test",
                    grant_id=snapshot["grant"]["grant_id"],
                    grant_revision=snapshot["authority_revision"],
                    goal_revision=snapshot["goal_revision"],
                    observed_frontier_hash=round_row.frontier_hash,
                    observed_workspace_revision=round_row.workspace_revision,
                    rationale="请求权限",
                    actions=({
                        "action": "wait_for_user", "reason": "需要写入权限", "cause": "permission",
                        "required_input": "请授权写入",
                        "evidence_identity": {"kind": "capability", "reference_id": "write", "revision": snapshot["goal_revision"]},
                    },),
                )
            async with sessions.begin() as session:
                changed = await session.get(LoopRound, snapshot["current_round_id"], with_for_update=True)
                changed.frontier_hash = "b" * 64
            result = await LoopKernel(sessions).commit(intent)
            async with sessions() as session:
                waits = tuple((await session.scalars(select(LoopWaitRequest).where(
                    LoopWaitRequest.loop_id == fixture["loop_id"], LoopWaitRequest.status == "open",
                ))).all())
            assert result.status == "superseded" and result.reason == "frontier_changed"
            assert waits == ()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_explicit_recovery_reuses_current_mission_once_and_rejects_other_waits(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            loop_id = fixture["loop_id"]
            old_round_id = fixture["snapshot"]["current_round_id"]
            async with sessions() as session:
                prior_round_count = len(tuple((await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id))).all()))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                old_round = await session.get(LoopRound, old_round_id, with_for_update=True)
                old_round.status = "settled"
                wait = await LoopWaitRequestService().open(
                    session,
                    loop,
                    LoopWaitRequestFactory.clarification("请重新说明任务", {"cause": "missing_goal", "mission_revision": 1, "evidence_identity": {"kind": "mission", "reference_id": "outcome", "revision": 1}}),
                    created_by="portfolio-patrol",
                    correlation_id="old-decision",
                    round_id=old_round_id,
                )
                wait_id = wait.request_id
            body = ResumeWithCurrentMissionRequest(
                confirmation="resume_with_current_mission",
                request_revision=1,
                idempotency_key=f"resume:{wait_id}",
            )
            service = fixture["service"]
            first = await service.resume_with_current_mission(loop_id, wait_id, body, actor_id="user")
            replay = await service.resume_with_current_mission(loop_id, wait_id, body, actor_id="user")
            async with sessions() as session:
                waits = tuple((await session.scalars(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop_id))).all())
                responses = tuple((await session.scalars(select(LoopWaitResponse).where(LoopWaitResponse.request_id == wait_id))).all())
                rounds = tuple((await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id))).all())
                successor = await session.get(LoopRound, first["current_round_id"])
                assert successor.frontier_hash == await current_frontier_hash(session, loop_id)
            assert first["created"] is True and replay["created"] is False
            assert first["recovery_event_id"] == replay["recovery_event_id"]
            assert first["current_round_id"] != old_round_id
            assert replay["current_round_id"] == first["current_round_id"]
            assert len(waits) == 1 and waits[0].status == "superseded"
            assert responses == ()
            assert len(rounds) == prior_round_count + 1

            other = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, other["loop_id"], with_for_update=True)
                budget_wait = await LoopWaitRequestService().open(
                    session,
                    loop,
                    LoopWaitRequestFactory.budget_action("模型调用上限", {}),
                    created_by="kernel",
                    correlation_id="budget-decision",
                )
                budget_wait_id = budget_wait.request_id
            with pytest.raises(HTTPException) as error:
                await other["service"].resume_with_current_mission(
                    other["loop_id"],
                    budget_wait_id,
                    ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission", request_revision=1, idempotency_key="wrong-budget"),
                    actor_id="user",
                )
            assert error.value.status_code == 409

            limited = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                limited_loop = await session.get(AgentLoop, limited["loop_id"], with_for_update=True)
                limited_round = await session.get(LoopRound, limited_loop.current_round_id, with_for_update=True)
                limited_round.status = "settled"
                limited_grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == limited_loop.loop_id,
                    LoopDelegationGrant.revision == limited_loop.authority_revision,
                ))
                limited_grant.budgets = {**limited_grant.budgets, "max_model_calls": 0}
                limited_wait = await LoopWaitRequestService().open(
                    session, limited_loop,
                    LoopWaitRequestFactory.clarification("请重新说明任务", {"cause": "missing_goal", "mission_revision": limited_loop.goal_revision, "evidence_identity": {"kind": "mission", "reference_id": "outcome", "revision": limited_loop.goal_revision}}),
                    created_by="portfolio-patrol", correlation_id="limited-decision", round_id=limited_round.round_id,
                )
            with pytest.raises(HTTPException, match="当前预算"):
                await limited["service"].resume_with_current_mission(
                    limited["loop_id"], limited_wait.request_id,
                    ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission", request_revision=1, idempotency_key="limited-resume"),
                    actor_id="user",
                )
            async with sessions() as session:
                persisted = await session.get(LoopWaitRequest, limited_wait.request_id)
                assert persisted.status == "open"
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("prompt,scope", [
    ("请批准写入", {}),
    ("请补充确实缺少的输入", {"cause": "missing_input", "evidence_identity": {"kind": "input", "reference_id": "input-1", "revision": 1}}),
    ("请批准写入", {"cause": "permission", "evidence_identity": {"kind": "capability", "reference_id": "write", "revision": 1}}),
])
def test_current_mission_recovery_keeps_unrelated_text_waits_open(tmp_path: Path, prompt: str, scope: dict) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                round_row = await session.get(LoopRound, loop.current_round_id, with_for_update=True)
                round_row.status = "settled"
                wait = await LoopWaitRequestService().open(
                    session, loop, LoopWaitRequestFactory.clarification(prompt, scope),
                    created_by="portfolio-patrol", correlation_id=uuid.uuid4().hex, round_id=round_row.round_id,
                )
                request_id = wait.request_id
                expected_status = loop.status
            with pytest.raises(HTTPException) as error:
                await fixture["service"].resume_with_current_mission(
                    fixture["loop_id"], request_id,
                    ResumeWithCurrentMissionRequest(confirmation="resume_with_current_mission", request_revision=1, idempotency_key=uuid.uuid4().hex),
                    actor_id="user",
                )
            assert error.value.status_code == 409
            async with sessions() as session:
                persisted = await session.get(LoopWaitRequest, request_id)
                assert persisted.status == "open"
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == expected_status
        finally:
            await engine.dispose()

    asyncio.run(run())
