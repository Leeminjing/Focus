"""Patrol 临时 Provider 故障：真实 Responses 请求、冻结输入、预算与单次决策验收。"""

import asyncio
import json

import httpx
import pytest

from backend.app.desktop.patrol.agent_loop.rounds import round_orchestration as module
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import StructuredPatrolDecisionModel
from backend.tests.config_helpers import app_config_for
from backend.tests.test_patrol_failure_diagnostics import _observation
from focus.models.factory import create_chat_model


def _config(method="prompt_json"):
    config = app_config_for("patrol-retry", None)
    config.models[0].protocol = "responses"
    config.models[0].curation_output_method = method
    config.patrol.output_method = method
    return config


def _success(proposal=None):
    proposal = proposal or {"rationale": "继续工作", "mission_references": [{"role": "outcome", "reference_id": "outcome"}],
        "actions": [{"action": "continue_context", "context_id": "context", "context_revision_id": "revision", "message": "继续检查"}]}
    return httpx.Response(200, json={"id": "response-success", "object": "response", "created_at": 0,
        "status": "completed", "model": "patrol-retry", "output": [{"id": "message", "type": "message",
            "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": json.dumps(proposal), "annotations": []}]}],
        "usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}})


def _provider(monkeypatch, outcomes, *, proposal=None):
    requests, waits, models = [], [], []

    def handle(request):
        requests.append(json.loads(request.content))
        outcome = outcomes[min(len(requests) - 1, len(outcomes) - 1)]
        if outcome == "connection":
            raise httpx.ConnectError("temporary connection failure", request=request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("temporary timeout", request=request)
        if outcome != 200:
            return httpx.Response(outcome, json={"error": {"message": "temporary provider failure"}})
        response = _success(proposal)
        if not requests[-1].get("stream"):
            return response
        completed = response.json()
        item = completed["output"][0]
        events = [
            {"type": "response.created", "response": {"id": completed["id"], "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
             "item_id": item["id"], "delta": item["content"][0]["text"]},
            {"type": "response.completed", "response": completed},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(event) + "\n\n" for event in events))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    def create(**kwargs):
        model = create_chat_model(**kwargs, http_async_client=client)
        models.append(model)
        return model

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(module, "create_chat_model", create)
    # Patch only Patrol's backoff; SDK retry sleeps must remain observable.
    monkeypatch.setattr(module, "_patrol_retry_sleep", sleep, raising=False)
    return client, requests, waits, models


@pytest.mark.parametrize("failure", [429, 500, 502, 503, 504, "connection", "timeout"])
@pytest.mark.parametrize("method", ["prompt_json", "json_schema"])
def test_transient_failure_reuses_frozen_request_then_returns_one_intent(monkeypatch, failure, method):
    async def run():
        client, requests, waits, models = _provider(monkeypatch, [failure, 200])
        decision_model = StructuredPatrolDecisionModel(_config(method))
        observation = _observation()
        original = observation.model_dump_json()
        try:
            intent = await decision_model(observation)
            assert intent.actions[0].action == "continue_context"
            assert (intent.loop_id, intent.round_id, intent.grant_id) == ("loop", "round", "grant")
            assert requests[0] == requests[1] and len(requests) == 2
            assert waits == [0.5]
            assert decision_model.call_count == decision_model.usage.model_calls == 2
            assert (decision_model.usage.input_tokens, decision_model.usage.output_tokens) == (40, 10)
            assert len(decision_model.attempt_metadata) == 2
            metrics = decision_model.attempt_metadata[-1]
            assert metrics["input_tokens"] == 40 and metrics["output_tokens"] == 10
            assert metrics["estimated_input_tokens"] > 0 and metrics["contract_valid"] is True
            assert 0 <= metrics["ttft_ms"] <= metrics["duration_ms"]
            assert metrics["output_duration_ms"] >= 0
            assert models[0].max_retries == 0
            assert observation.model_dump_json() == original
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_errors_are_not_retried(monkeypatch, status):
    async def run():
        client, requests, waits, _ = _provider(monkeypatch, [status, 200])
        try:
            with pytest.raises(Exception) as failure:
                await StructuredPatrolDecisionModel(_config())(_observation())
            assert failure.value.status_code == status
            assert len(requests) == 1 and not waits
        finally:
            await client.aclose()
    asyncio.run(run())


def test_transient_errors_exhaust_three_requests(monkeypatch):
    async def run():
        client, requests, waits, _ = _provider(monkeypatch, [503])
        model = StructuredPatrolDecisionModel(_config())
        try:
            with pytest.raises(Exception) as failure:
                await model(_observation())
            assert failure.value.status_code == 503
            assert len(requests) == model.call_count == model.usage.model_calls == 3
            assert waits == [0.5, 1.0]
        finally:
            await client.aclose()
    asyncio.run(run())


def test_cancellation_during_backoff_starts_no_new_request(monkeypatch):
    async def run():
        client, requests, _, _ = _provider(monkeypatch, [429, 200])
        async def cancel(delay):
            raise asyncio.CancelledError()
        monkeypatch.setattr(module, "_patrol_retry_sleep", cancel, raising=False)
        try:
            with pytest.raises(asyncio.CancelledError):
                await StructuredPatrolDecisionModel(_config())(_observation())
            assert len(requests) == 1
        finally:
            await client.aclose()
    asyncio.run(run())


def test_retry_cannot_exceed_frozen_model_call_budget(monkeypatch):
    async def run():
        client, requests, _, _ = _provider(monkeypatch, [429, 200])
        observation = _observation().model_copy(update={"budget": {"limits": {"max_model_calls": 1}}})
        try:
            with pytest.raises(RuntimeError, match="budget 已耗尽"):
                await StructuredPatrolDecisionModel(_config())(observation)
            assert len(requests) == 1
        finally:
            await client.aclose()
    asyncio.run(run())


def test_retry_cannot_exceed_frozen_retry_budget(monkeypatch):
    async def run():
        client, requests, _, _ = _provider(monkeypatch, [429, 200])
        observation = _observation().model_copy(update={"budget": {"limits": {"max_retries": 0}}})
        try:
            with pytest.raises(RuntimeError, match="retry budget 已耗尽"):
                await StructuredPatrolDecisionModel(_config())(observation)
            assert len(requests) == 1
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["success", "exhausted", "pause", "budget", "fence", "retry_budget", "expired_grant"])
def test_owned_patrol_retries_preserve_authority_accounting_and_single_commit(tmp_path, monkeypatch, isolated_postgres_database, scenario):
    import os
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopIndexBudgetReservation
    from backend.app.desktop.patrol.agent_loop.coordinator import LoopCoordinator
    from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import resolve_context_equipment
    from backend.app.desktop.patrol.agent_loop.kernel import LoopKernel
    from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopCoordinatorFence, LoopDecision, LoopDelegationGrant, LoopDirective, LoopObservation, LoopPatrolAttempt, LoopRound
    from backend.app.desktop.patrol.agent_loop.observations.observation_capture import LoopObservationService
    from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
    from backend.tests.test_round_task_progress import _Checkpointer

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="transport-retry", started_at=datetime.now(UTC),
            budgets={"max_model_calls": 1} if scenario == "budget" else None)
        client = None
        try:
            claim = await LoopCoordinator(sessions).claim_for_loop(fixture["loop_id"], "retry-test")
            assert claim is not None
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(claim.loop_id, claim.round_id)
            async with sessions() as session:
                loop = await session.get(AgentLoop, claim.loop_id)
                equipment = await resolve_context_equipment(session, loop, fixture["context_id"])
            proposal = {"rationale": "继续检查", "mission_references": [{"role": "outcome", "reference_id": "outcome"}],
                "context_permissions": [{"context_id": fixture["context_id"], "access_mode": equipment["access_mode"]}],
                "actions": [{"action": "continue_context", "context_id": fixture["context_id"],
                    "context_revision_id": fixture["revision_id"], "message": "运行检查"}]}
            client, requests, waits, _ = _provider(monkeypatch, [503] if scenario == "exhausted" else [429, 200], proposal=proposal)
            if scenario in {"pause", "fence", "retry_budget", "expired_grant"}:
                async def revoke(delay):
                    waits.append(delay)
                    if scenario == "pause":
                        await fixture["service"].control(claim.loop_id, "pause")
                    elif scenario == "fence":
                        async with sessions.begin() as session:
                            fence = await session.get(LoopCoordinatorFence, claim.round_id, with_for_update=True)
                            fence.fencing_token += 1
                    else:
                        async with sessions.begin() as session:
                            grant = await session.get(LoopDelegationGrant, observation.grant["grant_id"], with_for_update=True)
                            if scenario == "retry_budget":
                                grant.budgets = {**grant.budgets, "max_retries": 0}
                            else:
                                grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                monkeypatch.setattr(module, "_patrol_retry_sleep", revoke)
            orchestrator = module.LoopRoundOrchestrator(sessions, _config(), LoopKernel(sessions), _Checkpointer())
            if scenario == "success":
                intent = await orchestrator._decide_patrol(claim, None, observation.grant["holder_id"], observation)
                intent = orchestrator._bind_fencing(intent, claim)
                kernel = LoopKernel(sessions, require_fencing=True)
                result = await kernel.commit(intent)
                assert result.status == "committed", result.reason
                assert await kernel.commit(intent) == result
            else:
                with pytest.raises(Exception):
                    await orchestrator._decide_patrol(claim, None, observation.grant["holder_id"], observation)
            async with sessions() as session:
                loop = await session.get(AgentLoop, claim.loop_id)
                round_row = await session.get(LoopRound, claim.round_id)
                attempts = tuple(await session.scalars(select(LoopPatrolAttempt).where(LoopPatrolAttempt.loop_id == claim.loop_id)))
                receipts = tuple(await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == claim.loop_id)))
                decisions = tuple(await session.scalars(select(LoopDecision).where(LoopDecision.loop_id == claim.loop_id)))
                directives = tuple(await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == claim.loop_id)))
                observations = tuple(await session.scalars(select(LoopObservation).where(LoopObservation.loop_id == claim.loop_id)))
                usage = await session.get(LoopBudgetUsage, claim.loop_id)
                assert len(attempts) == 1
                attempt = attempts[0]
                assert len(observations) == 1 and observations[0].envelope_hash == attempt.observation_hash
                assert observations[0].envelope == observation.model_dump(mode="json")
                assert all(row.actual_usage["owner_id"] == attempt.patrol_attempt_id for row in receipts)
                assert all(row.actual_usage["observation_id"] == observation.observation_id for row in receipts)
                assert len(receipts) == len(requests) == len(attempt.raw_output["model_attempts"])
                assert usage.retries == len(requests) - 1
                assert attempt.raw_output["model_attempts"][0]["status_code"] == (503 if scenario == "exhausted" else 429)
                if scenario == "success":
                    assert loop.status == "running" and not loop.waiting_reason
                    assert len(decisions) == len(directives) == 1
                    assert round_row.decision_id == result.decision_id and attempt.status == "success"
                    assert len(requests) == 2 and requests[0] == requests[1]
                    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, 40, 10)
                    assert sum(row.actual_usage.get("receipt_state") == "unknown" for row in receipts) == 1
                    assert sum(row.settled_at is not None for row in receipts) == 1
                else:
                    assert not decisions and not directives and round_row.decision_id is None
                    assert attempt.status == ("interrupted" if scenario == "pause" else "error")
                    assert all(row.actual_usage.get("receipt_state") == "unknown" for row in receipts)
                    assert loop.status == {"pause": "paused", "fence": "running"}.get(scenario, "waiting_user")
                    assert len(requests) == (3 if scenario == "exhausted" else 1)
        finally:
            if client is not None:
                await client.aclose()
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
