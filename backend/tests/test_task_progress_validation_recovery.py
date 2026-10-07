"""本文件对外提供任务记忆验证反馈、原子发布和遗留恢复的隔离集成测试。

输入为独立 PostgreSQL 的冻结工作和模拟模型；输出为按尝试诊断、预算、重启、fencing 与一次发布断言。
具体工作流为注入非法候选、篡改贡献或事务故障，重建 runtime 后恢复并核对原输入和 receipts；验证空增量不调用及完整请求预算，不调用真实模型或修改桌面数据库。
示例：python -m pytest backend/tests/test_task_progress_validation_recovery.py -q。
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from focus.runtime.runs.usage import ModelUsage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_round_task_progress import _Checkpointer
from test_round_memory_boundaries import _next_round

from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import validation_diagnostic
from backend.app.desktop.agent_loop.task_progress.contracts import ProgressCandidate, canonical_hash
from backend.app.desktop.agent_loop.task_progress.candidate_contract import CandidateValidationError, candidate_schema
from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import interpretation_payload
from backend.app.desktop.agent_loop.task_progress.consolidation import TaskProgressConsolidator
from backend.app.desktop.agent_loop.task_progress.models import LoopProgressReceipt, LoopProgressWork
from backend.app.desktop.agent_loop.task_progress.repository import ProgressPublicationRejected, TaskProgressRepository
from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime, _SYSTEM


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")
_SECRET = "sk-unknown-fixture-credential"


class _Model:
    context_window_tokens = 100000
    max_output_tokens = 1000
    last_usage_reported = True
    last_usage = ModelUsage(model_calls=1, input_tokens=17, output_tokens=9)

    def __init__(self, failure=None, always=False):
        self.failure = failure
        self.always = always
        self.payloads = []

    async def invoke(self, schema, system, payload):
        self.payloads.append(payload)
        failure = self.failure[len(self.payloads) - 1] if isinstance(self.failure, tuple) else self.failure
        assessments = [{"source_key": source["source_key"], "disposition": "unknown", "explanation": "待验证"}
                       for source in payload["task_delta"]["sources"]]
        failing = failure and (isinstance(self.failure, tuple) or len(self.payloads) == 1 or self.always)
        if failing and failure == "duplicate":
            assessments.append({**assessments[0], "explanation": _SECRET})
        elif failing and failure == "outside":
            assessments.append({"source_key": _SECRET, "disposition": "unknown", "explanation": _SECRET})
        elif failing and failure == "malformed":
            raise json.JSONDecodeError(_SECRET, _SECRET, 0)
        return schema.model_validate({"source_assessments": assessments})


async def _receipt_count(session, loop_id):
    return await session.scalar(select(func.count()).select_from(LoopProgressReceipt).where(LoopProgressReceipt.loop_id == loop_id))


@pytest.mark.parametrize("failure,code", [("duplicate", "duplicate_source_assessment"),
                                          ("outside", "unknown_assessment_source"),
                                          ("malformed", "schema_invalid_json")])
def test_invalid_candidate_feedback_survives_restart_and_publishes_once(tmp_path, caplog, failure, code):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label=f"fb-{failure[:3]}", started_at=datetime.now(UTC))
        loop_id = fixture["loop_id"]
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, fixture["round_id"])
            identity = observation.decision_inputs_ref
            async with sessions() as session:
                inputs = await TaskProgressRepository().inputs(session, identity)
                head = await TaskProgressRepository().current(session, loop_id)
                receipts = await _receipt_count(session, loop_id)
                ledger = await session.get(LoopBudgetUsage, loop_id)
                before = (ledger.model_calls, ledger.input_tokens, ledger.output_tokens)
            model = _Model(failure)
            assert await TaskProgressRuntime(sessions, None, model_factory=lambda name: model).drain(loop_id=loop_id) == 1
            async with sessions() as session:
                work = await session.get(LoopProgressWork, identity)
                diagnostic = [event for event in work.attempt_events if event["event"] == "candidate_validation"][0]
                assert code in {error["code"] for error in diagnostic["errors"]}
                assert diagnostic["inputs_hash"] == canonical_hash(inputs)
                assert work.state == "pending" and work.attempts == 1
                assert (await TaskProgressRepository().current(session, loop_id)).progress_id == head.progress_id
                assert await _receipt_count(session, loop_id) == receipts
                assert _SECRET not in json.dumps(work.attempt_events)
            recovered = TaskProgressRuntime(sessions, None, model_factory=lambda name: model)
            assert await recovered.drain(loop_id=loop_id) == 1
            assert await recovered.drain(loop_id=loop_id) == 0
            assert len(model.payloads) == 2
            assert model.payloads[1]["validation_feedback"]["errors"][0]["code"] == code
            assert model.payloads[1]["task_delta"] == model.payloads[0]["task_delta"]
            assert _SECRET not in json.dumps(model.payloads)
            async with sessions() as session:
                work = await session.get(LoopProgressWork, identity)
                assert work.state == "published"
                assert (await TaskProgressRepository().current(session, loop_id)).generation == head.generation + 1
                assert await _receipt_count(session, loop_id) == receipts + len(inputs.task_delta.sources)
                assert await TaskProgressRepository().inputs(session, identity) == inputs
                ledger = await session.get(LoopBudgetUsage, loop_id)
                assert (ledger.model_calls - before[0], ledger.input_tokens - before[1], ledger.output_tokens - before[2]) == (2, 34, 18)
                assert len([event for event in work.attempt_events if event["event"] == "candidate_validation"]) == 2
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()
    asyncio.run(run())
    assert _SECRET not in caplog.text


def test_continuous_invalidity_stops_after_three_calls_and_keeps_each_diagnostic(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="invalid", started_at=datetime.now(UTC))
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            model = _Model("duplicate", always=True)
            runtime = TaskProgressRuntime(sessions, None, model_factory=lambda name: model)
            for _ in range(3):
                assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 0
            assert len(model.payloads) == 3
            async with sessions() as session:
                work = await session.get(LoopProgressWork, observation.decision_inputs_ref)
                assert work.state == "blocked" and work.attempts == 3
                assert len(work.usage) == 3 and all(item["accounted"] for item in work.usage)
                assert len([event for event in work.attempt_events if event["event"] == "candidate_validation"]) == 3
                assert (await TaskProgressRepository().current(session, fixture["loop_id"])).generation == 0
                assert await _receipt_count(session, fixture["loop_id"]) == 0
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["diagnostic", "publication"])
def test_persistence_failure_does_not_publish_or_absorb_and_recovers(tmp_path, failure):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label=f"p-{failure[:3]}", started_at=datetime.now(UTC))
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            model = _Model()
            runtime = TaskProgressRuntime(sessions, None, model_factory=lambda name: model)
            async def reject(*args, **kwargs):
                if failure == "publication":
                    await TaskProgressRepository().publish(*args, **kwargs)
                raise RuntimeError("fixture transaction failure")
            setattr(runtime._repository, "record_validation" if failure == "diagnostic" else "publish", reject)
            await runtime.drain(loop_id=fixture["loop_id"])
            async with sessions() as session:
                assert (await TaskProgressRepository().current(session, fixture["loop_id"])).generation == 0
                assert await _receipt_count(session, fixture["loop_id"]) == 0
                assert (await session.get(LoopProgressWork, observation.decision_inputs_ref)).state == "pending"
            await TaskProgressRuntime(sessions, None, model_factory=lambda name: model).drain(loop_id=fixture["loop_id"])
            async with sessions() as session:
                assert (await session.get(LoopProgressWork, observation.decision_inputs_ref)).state == "published"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_legacy_blocked_work_requires_explicit_authority_and_preserves_missing_evidence(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="legacy", started_at=datetime.now(UTC))
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            async with sessions.begin() as session:
                work = await session.get(LoopProgressWork, observation.decision_inputs_ref)
                work.state = "blocked"
                work.attempts = 3
                work.error = "ValueError: 来源解释重复或引用冻结范围之外的来源"
                work.attempt_events = [{"event": "blocked", "failure_kind": "ValueError"}]
                frozen = await TaskProgressRepository().inputs(session, observation.decision_inputs_ref)
            model = _Model()
            runtime = TaskProgressRuntime(sessions, None, model_factory=lambda name: model)
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 0
            assert not model.payloads
            await runtime.retry(observation.decision_inputs_ref, loop_id=fixture["loop_id"])
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
            assert model.payloads[0]["validation_feedback"]["evidence_missing"]
            async with sessions() as session:
                work = await session.get(LoopProgressWork, observation.decision_inputs_ref)
                assert work.state == "published" and work.attempt_events[0]["event"] == "blocked"
                assert work.retry_budget_authorization is not None
                assert await TaskProgressRepository().inputs(session, observation.decision_inputs_ref) == frozen
            await _stop(fixture["service"], fixture["loop_id"])
            async with sessions.begin() as session:
                work = await session.get(LoopProgressWork, observation.decision_inputs_ref)
                work.state = "blocked"
            with pytest.raises(ValueError, match="有效的预算授权"):
                await runtime.retry(observation.decision_inputs_ref, loop_id=fixture["loop_id"])
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_stale_worker_cannot_append_diagnostic_or_override_new_claim(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="fence", started_at=datetime.now(UTC))
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            runtime = TaskProgressRuntime(sessions, None)
            old = await runtime._claim(loop_id=fixture["loop_id"])
            async with sessions.begin() as session:
                work = await session.get(LoopProgressWork, old[0])
                work.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                inputs = await TaskProgressRepository().inputs(session, old[0])
            newer = await runtime._claim(loop_id=fixture["loop_id"])
            diagnostic = validation_diagnostic(inputs, old[1], ProgressCandidate(), ())
            with pytest.raises(ProgressPublicationRejected, match="fence/lease"):
                async with sessions.begin() as session:
                    await TaskProgressRepository().record_validation(session, inputs, old[1], diagnostic)
            await runtime._fail(old[0], old[1], ValueError("old failure"))
            async with sessions() as session:
                work = await session.get(LoopProgressWork, newer[0])
                assert work.fence == newer[1] and work.state == "claimed" and work.error is None
                assert not any(event["event"] == "candidate_validation" for event in work.attempt_events)
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_empty_delta_publishes_once_without_model_or_reservation(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="empty", started_at=datetime.now(UTC))
        loop_id = fixture["loop_id"]
        try:
            service = LoopObservationService(sessions, _Checkpointer())
            await service.capture(loop_id, fixture["round_id"])
            await TaskProgressRuntime(sessions, None, model_factory=lambda name: _Model()).drain(loop_id=loop_id)
            next_round = await _next_round(sessions, fixture)
            observation = await service.capture(loop_id, next_round)
            identity = observation.decision_inputs_ref
            async with sessions() as session:
                inputs = await TaskProgressRepository().inputs(session, identity)
                assert not inputs.task_delta.sources
                previous = await TaskProgressRepository().current(session, loop_id)
                receipt_count = await _receipt_count(session, loop_id)
                ledger = await session.get(LoopBudgetUsage, loop_id)
                usage_before = (ledger.model_calls, ledger.input_tokens, ledger.output_tokens)

            def forbidden(name):
                pytest.fail("空增量不得创建或调用模型")

            runtime = TaskProgressRuntime(sessions, None, model_factory=forbidden)
            assert await runtime.drain(loop_id=loop_id) == 1
            assert await runtime.drain(loop_id=loop_id) == 0
            async with sessions() as session:
                work = await session.get(LoopProgressWork, identity)
                current = await TaskProgressRepository().current(session, loop_id)
                assert work.state == "published" and work.usage == []
                assert current.generation == previous.generation + 1 and current.document == previous.document
                assert await _receipt_count(session, loop_id) == receipt_count
                ledger = await session.get(LoopBudgetUsage, loop_id)
                assert (ledger.model_calls, ledger.input_tokens, ledger.output_tokens) == usage_before
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("tampering", ["duplicate", "document", "contribution"])
def test_publication_revalidates_tampered_candidate_body_and_contribution(tmp_path, tampering):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="tamper", started_at=datetime.now(UTC))
        loop_id = fixture["loop_id"]
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, fixture["round_id"])
            runtime = TaskProgressRuntime(sessions, None)
            identity, fence = await runtime._claim(loop_id=loop_id)
            async with sessions() as session:
                inputs = await TaskProgressRepository().inputs(session, identity)
                previous = await TaskProgressRepository().current(session, loop_id)
                receipts = await _receipt_count(session, loop_id)
            candidate = await _Model().invoke(candidate_schema(inputs), _SYSTEM, interpretation_payload(inputs, observation.mission))
            document, contribution = TaskProgressConsolidator().apply(inputs, candidate, observation.mission)
            if tampering == "duplicate":
                contribution = contribution.model_copy(update={"source_assessments": contribution.source_assessments + (contribution.source_assessments[0],)})
                expected = CandidateValidationError
            elif tampering == "document":
                document = document.model_copy(update={"items": (document.items[0].model_copy(update={"description": "被篡改"}), *document.items[1:])})
                expected = ProgressPublicationRejected
            else:
                contribution = contribution.model_copy(update={"direct_source_keys": ("outside",)})
                expected = ProgressPublicationRejected
            with pytest.raises(expected):
                async with sessions.begin() as session:
                    await TaskProgressRepository().publish(session, identity, fence, document, contribution)
            async with sessions() as session:
                assert (await TaskProgressRepository().current(session, loop_id)).progress_id == previous.progress_id
                assert await _receipt_count(session, loop_id) == receipts
                assert (await session.get(LoopProgressWork, identity)).state == "claimed"
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()
    asyncio.run(run())


def test_distinct_reference_failures_keep_same_work_history_across_restart(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="history", started_at=datetime.now(UTC))
        loop_id = fixture["loop_id"]
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, fixture["round_id"])
            model = _Model(("duplicate", "outside", None))
            for _ in range(3):
                assert await TaskProgressRuntime(sessions, None, model_factory=lambda name: model).drain(loop_id=loop_id) == 1
            assert await TaskProgressRuntime(sessions, None, model_factory=lambda name: model).drain(loop_id=loop_id) == 0
            assert [payload["validation_feedback"]["errors"][0]["code"] for payload in model.payloads[1:]] == [
                "duplicate_source_assessment", "unknown_assessment_source"]
            assert all(payload["task_delta"] == model.payloads[0]["task_delta"] for payload in model.payloads)
            async with sessions() as session:
                work = await session.get(LoopProgressWork, observation.decision_inputs_ref)
                diagnostics = [event for event in work.attempt_events if event["event"] == "candidate_validation"]
                assert work.state == "published" and work.attempts == 3
                assert [event["fence"] for event in diagnostics] == [1, 2, 3]
                assert [event["valid"] for event in diagnostics] == [False, False, True]
                assert diagnostics[0]["errors"][0]["code"] == "duplicate_source_assessment"
                assert diagnostics[1]["errors"][0]["code"] == "unknown_assessment_source"
                assert len({event["inputs_hash"] for event in diagnostics}) == 1
                assert _SECRET not in json.dumps(diagnostics)
                assert (await TaskProgressRepository().current(session, loop_id)).generation == 1
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("capacity", ["too_small", "fits"])
def test_full_request_input_budget_is_checked_and_reserved(tmp_path, monkeypatch, capacity):
    async def run():
        from backend.app.desktop.agent_loop import structured_worker
        from focus.messages.usage import estimate_raw_tokens
        from focus.models.responses import FocusResponsesChatModel
        from focus.models.provider_contract import ProviderContract
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="budget", started_at=datetime.now(UTC))
        loop_id = fixture["loop_id"]
        model = FocusResponsesChatModel(model="fixture", api_key="fixture", max_tokens=1000,
                                       provider_contract=ProviderContract(provider="openai", protocol="responses"))
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop_id, fixture["round_id"])
            config = SimpleNamespace(name="fixture", model="fixture", curation_output_method="json_schema",
                                     curation_max_output_tokens=1000, context_window=100000)
            app = SimpleNamespace(get_model=lambda name: config, resolve_default_model_name=lambda: "fixture")
            monkeypatch.setattr(structured_worker, "create_chat_model", lambda **kwargs: model)
            worker = structured_worker.StructuredWorkerModel(app)
            runtime = TaskProgressRuntime(sessions, app, model_factory=lambda name: worker)
            identity, fence = await runtime._claim(loop_id=loop_id)
            async with sessions() as session:
                inputs = await TaskProgressRepository().inputs(session, identity)
                ledger = await session.get(LoopBudgetUsage, loop_id)
                before = (ledger.model_calls, ledger.input_tokens, ledger.output_tokens)
            schema = candidate_schema(inputs)
            payload = interpretation_payload(inputs, observation.mission)
            complete = worker.estimate_input_tokens(schema, _SYSTEM, payload)
            messages = worker.request_messages(schema, _SYSTEM, payload)
            old = estimate_raw_tokens("".join(str(message.content) for message in messages), len(messages))
            assert complete > old
            envelope = observation.model_dump(mode="json")
            envelope["budget"]["limits"]["max_input_tokens"] = before[1] + (old if capacity == "too_small" else complete)
            calls = []

            async def invoke(schema, system, payload):
                calls.append(payload)
                return await _Model().invoke(schema, system, payload)

            monkeypatch.setattr(worker, "invoke", invoke)
            if capacity == "too_small":
                with pytest.raises(ValueError, match="input_tokens_budget"):
                    await runtime._interpret(inputs, fence, envelope, None, payload)
                assert not calls
            else:
                await runtime._interpret(inputs, fence, envelope, None, payload)
                assert len(calls) == 1
            async with sessions() as session:
                work = await session.get(LoopProgressWork, identity)
                ledger = await session.get(LoopBudgetUsage, loop_id)
                if capacity == "too_small":
                    assert work.usage == []
                    assert (ledger.model_calls, ledger.input_tokens, ledger.output_tokens) == before
                else:
                    assert work.usage[0]["estimated_input_tokens"] == complete
                    assert ledger.input_tokens - before[1] == complete
                assert await TaskProgressRepository().inputs(session, identity) == inputs
        finally:
            model._client.close()
            await _stop(fixture["service"], loop_id)
            await engine.dispose()
    asyncio.run(run())
