"""本文件对外提供 D8 完成输入、动态声明与异步校验的生产入口回归。

输入为冻结检查、可信工具报告、隔离 PostgreSQL 与脚本化 Provider；输出为严格身份、有界纠错及来源新鲜度断言。
具体工作流为调用实际模型包装器和 WorkerRuntime，保留旧冻结事实，核对逐次消费及最终权威拒绝。
示例：pytest backend/tests/test_completion_input_contract.py；脚本化响应不代替原生模型验收。
"""

import asyncio
import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.agent_loop.derivation_worker import safe_validation_message
from backend.app.desktop.agent_loop.workers import StructuredCompletionVerifier
from backend.app.desktop.agent_loop.schemas import CompletionVerificationResult
from backend.app.desktop.domain_evidence.tests import TestResultParser
from backend.tests.config_helpers import app_config_for


def _checks():
    return ({"check_id": "tests", "required": True, "expected_evidence_kinds": ["test"]},
            {"check_id": "build", "required": True, "expected_evidence_kinds": ["test"]})


def _unknown():
    return {"criteria": [{"check_id": c["check_id"], "status": "unknown", "explanation": "无可信证据"}
                         for c in _checks()], "conclusion": "unknown", "unresolved": ["tests", "build"]}


def test_declared_ids_are_in_nested_schema_and_runtime_validation():
    schema = CompletionCheckPolicy().schema_for(_checks())
    document = schema.model_json_schema()
    assert document["properties"]["unresolved"]["items"]["enum"] == ["tests", "build"]
    criterion = next(d for d in document["$defs"].values() if "check_id" in d.get("properties", {}))
    assert criterion["properties"]["check_id"]["enum"] == ["tests", "build"]
    assert schema.model_validate(_unknown()).conclusion == "unknown"
    for target in ("unresolved", "criteria"):
        invalid = _unknown()
        if target == "unresolved":
            invalid[target] = ["尚缺逐条用例映射"]
        else:
            invalid[target][0]["check_id"] = "undeclared"
        with pytest.raises(ValidationError):
            schema.model_validate(invalid)
    invalid = _unknown()
    invalid["criteria"] *= 2
    with pytest.raises(ValidationError, match="唯一"):
        schema.model_validate(invalid)


def test_schema_feedback_excludes_original_candidate_content():
    invalid = _unknown()
    invalid["unresolved"] = ["private-key-placeholder"]
    with pytest.raises(ValidationError) as caught:
        CompletionCheckPolicy().schema_for(_checks()).model_validate(invalid)
    feedback = safe_validation_message(caught.value)
    assert "unresolved" in feedback and "private-key-placeholder" not in feedback


@pytest.mark.usefixtures("isolated_postgres_database")
def test_unknown_source_identity_is_opaque_in_feedback_and_durable_failure(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop.models import LoopWorkerRequest
        from test_loop_worker_recovery import _case, _fail_three

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            async def respond(messages, config):
                provider.inputs.append(messages[-1].content)
                config["callbacks"][0].usage_metadata["safe-source"] = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
                return AIMessage(content=json.dumps({"criteria": [{"check_id": "tests", "status": "satisfied",
                    "explanation": "来源", "evidence": [{"kind": "fact", "source_id": "private-key-placeholder"}]}], "conclusion": "satisfied"}))
            provider.ainvoke = respond
            await _fail_three(runtime, fixture)
            assert len(provider.inputs) == 6
            assert all("private-key-placeholder" not in p for p in provider.inputs)
            assert "source_not_frozen" in provider.inputs[1]
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert "private-key-placeholder" not in json.dumps(row.result)
                assert "private-key-placeholder" not in json.dumps(row.scope["attempt_history"])
    asyncio.run(run())


def test_async_result_validation_is_awaited_and_uses_existing_feedback(monkeypatch):
    async def run():
        config = app_config_for("async-verifier", None)
        config.models[0].curation_output_method = "prompt_json"
        calls, validations = [], []

        class Provider:
            async def ainvoke(self, messages, config):
                calls.append(messages[-1].content)
                config["callbacks"][0].usage_metadata["async-verifier"] = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
                return AIMessage(content=json.dumps(_unknown()))

        monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
        worker = RoleBoundStructuredModel(config, "claim_verifier")

        async def validate(proposal):
            await asyncio.sleep(0)
            validations.append(proposal)
            if len(validations) == 1:
                raise ValueError("source_stale revision=1 expected=2")

        await worker.invoke_validated(CompletionVerificationResult, "verify", {}, validate)
        assert len(calls) == len(validations) == 2
        assert "source_stale" in calls[1]
        assert [r["outcome"] for r in worker.last_attempt_records] == ["error", "success"]
        assert worker.usage.model_calls == 2
    asyncio.run(run())


def _execution(output, code=0):
    return json.dumps({"run_id": "run", "call_id": "call", "workspace": ".", "status": "exited", "exit_code": code, "output": output})


def _tap():
    return "TAP version 13\n# Subtest: config\n    # Subtest: persists prompt\n    ok 1 - persists prompt\n    1..1\nok 1 - config\n# Subtest: vault missing\nok 2 - vault missing # SKIP optional\n1..2\n# tests 2\n# suites 1\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 1\n"


def test_trusted_tap_preserves_named_cases_and_proof_without_stdout():
    parsed = TestResultParser.parse("powershell", _execution(_tap()), command="npm test", run_id="run", call_id="call")
    report = parsed["named_results"]
    assert report["status"] == "complete"
    assert [(c["name"], c["status"]) for c in report["cases"]] == [("persists prompt", "passed"), ("vault missing", "skipped")]
    assert report["cases"][0]["suite"] == ["config"]
    assert len(report["report_hash"]) == 64
    assert parsed["execution_proof"]["bound"] is True
    assert "stdout" not in report and "TAP version" not in json.dumps(report)


@pytest.mark.parametrize("output", [_tap().split("1..2")[0], _tap().replace("# tests 2", "# tests 3"), "184 passed, 0 failed"])
def test_truncated_ambiguous_or_aggregate_reports_do_not_invent_coverage(output):
    parsed = TestResultParser.parse("test", _execution(output), command="npm test", run_id="run", call_id="call")
    assert parsed["named_results"]["status"] == "unknown"
    assert parsed["named_results"]["cases"] == []


def test_sensitive_case_titles_are_opaque_and_cannot_claim_coverage():
    output = _tap().replace("persists prompt", "secret-value-123")
    parsed = TestResultParser.parse("test", _execution(output), command="npm test", run_id="run", call_id="call", secrets=("secret-value-123",))
    assert "secret-value-123" not in json.dumps(parsed)
    case = parsed["named_results"]["cases"][0]
    assert case["name"] is None and case["coverage"] == "unknown"
    assert len(case["case_id"]) == 64


def test_unbound_and_failed_command_never_become_verified_with_names():
    failed = TestResultParser.parse("test", _execution(_tap(), 1), command="npm test", run_id="run", call_id="call")
    unbound = TestResultParser.parse("test", _execution(_tap()), command="npm test", run_id="other", call_id="call")
    assert failed["status"] == unbound["status"] == "failed"
    assert unbound["named_results"]["status"] == "unknown"


def test_explicitly_truncated_execution_cannot_publish_named_coverage():
    execution = json.loads(_execution(_tap()))
    execution["truncated"] = True
    parsed = TestResultParser.parse("test", json.dumps(execution), command="npm test", run_id="run", call_id="call")
    assert parsed["named_results"]["status"] == "unknown" and parsed["named_results"]["cases"] == []


def test_cancellation_during_async_validation_retains_actual_call_and_cleans_up(monkeypatch):
    async def run():
        config = app_config_for("async-cancel", None)
        config.models[0].curation_output_method = "prompt_json"
        started, cleaned = asyncio.Event(), asyncio.Event()

        class Provider:
            async def ainvoke(self, messages, config):
                config["callbacks"][0].usage_metadata["cancel"] = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
                return AIMessage(content=json.dumps(_unknown()))

        monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
        worker = RoleBoundStructuredModel(config, "claim_verifier")

        async def validate(proposal):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        task = asyncio.create_task(worker.invoke_validated(CompletionVerificationResult, "verify", {}, validate))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and worker.usage.model_calls == 1
        assert len(worker.last_attempt_records) == 1 and worker.last_attempt_records[0]["outcome"] == "cancelled"
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_worker_freezes_all_sources_and_projects_same_validator_eligibility(tmp_path, monkeypatch):
    async def run():
        from sqlalchemy import select
        from backend.app.desktop.agent_loop.completion_evidence_catalog import CompletionEvidenceCatalog
        from backend.app.desktop.agent_loop.models import AgentLoop, LoopWorkerRequest
        from backend.app.desktop.domain_evidence.models import DesktopDomainResult
        from backend.app.desktop.domain_evidence.repository import DomainResultRepository
        from backend.app.desktop.models import DesktopRun, DesktopWorkspace
        from completion_evidence_support import record_verified_fixture
        from test_loop_worker_recovery import _case

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                root = (await session.get(DesktopWorkspace, loop.workspace_id)).path
            current = await record_verified_fixture(sessions, fixture["loop_id"], fixture["context_id"], fixture["round_id"], root, 1)
            old = await record_verified_fixture(sessions, fixture["loop_id"], fixture["context_id"], fixture["round_id"], root, 0)
            async with sessions.begin() as session:
                for i in range(130):
                    await DomainResultRepository().record(session, loop_id=fixture["loop_id"], context_id=fixture["context_id"],
                        run_id=None, kind="run_outcome", source_id=f"history-{i}", payload={"status": "asserted"})
                newer = await DomainResultRepository().record(session, loop_id=fixture["loop_id"], context_id=fixture["context_id"],
                    run_id=(await session.get(DesktopDomainResult, current)).run_id, kind="test",
                    source_id=(await session.get(DesktopDomainResult, current)).source_id,
                    payload={**(await session.get(DesktopDomainResult, current)).payload, "workspace_revision": 0})
            request = (await runtime._claim_many(1, fixture["loop_id"]))[0]
            frozen, _, _ = await runtime._evidence(request)
            async with sessions() as session:
                keys = set(await session.scalars(select(DesktopDomainResult.result_key).where(DesktopDomainResult.loop_id == fixture["loop_id"])))
                assert {s["source_id"] for s in frozen["completion_sources"]} == keys
                original = deepcopy(frozen)
                view = await CompletionEvidenceCatalog().project(session, frozen, fixture["loop_id"], 1)
                assert frozen == original
                assert set(view["available_source_ids"]) == {current}
                states = {s["source_id"]: s["eligibility"]["status"] for s in view["completion_sources"]}
                assert states[old] == states[newer] == "stale"
                assert len(view["completion_sources"]) == len(keys)
                row = await session.get(LoopWorkerRequest, identity)
                assert row.scope["frozen_worker_input"] == frozen
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_actual_verifier_corrects_stale_source_in_same_worker_attempt(tmp_path, monkeypatch):
    async def run():
        from sqlalchemy import select
        from backend.app.desktop.agent_loop.models import AgentLoop, LoopWorkerRequest
        from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
        from backend.app.desktop.models import DesktopWorkspace
        from completion_evidence_support import record_verified_fixture
        from test_loop_worker_recovery import _case, _step

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                root = (await session.get(DesktopWorkspace, loop.workspace_id)).path
            current = await record_verified_fixture(sessions, fixture["loop_id"], fixture["context_id"], fixture["round_id"], root, 1)
            old = await record_verified_fixture(sessions, fixture["loop_id"], fixture["context_id"], fixture["round_id"], root, 0)

            async def respond(messages, config):
                provider.inputs.append(messages[-1].content)
                config["callbacks"][0].usage_metadata["worker-retry"] = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
                source = old if len(provider.inputs) == 1 else current
                return AIMessage(content=json.dumps({"criteria": [{"check_id": "tests", "status": "satisfied",
                    "explanation": "真实退出码", "evidence": [{"kind": "fact", "source_id": source}]}], "conclusion": "satisfied"}))
            provider.ainvoke = respond
            await _step(runtime, fixture["loop_id"])
            assert len(provider.inputs) == 2
            assert "source_stale" in provider.inputs[1] and old in provider.inputs[1]
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert (row.status, row.attempt) == ("success", 1)
                assert len(row.scope["attempt_history"]) == 1
                receipts = tuple(await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture["loop_id"])))
                assert len(receipts) == 2 and all(r.settled_at for r in receipts)
                assert "completion_view" not in row.scope["frozen_worker_input"]
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("change", ["workspace", "pause"])
def test_final_record_rechecks_after_candidate_validation(tmp_path, monkeypatch, change):
    async def run():
        from sqlalchemy import select, func
        from backend.app.desktop.agent_loop.completion import CompletionCandidateValidator, CompletionEvidenceService
        from backend.app.desktop.agent_loop.models import AgentLoop, CompletionVerification, LoopRound
        from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract
        from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
        from test_loop_worker_recovery import _case

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            request = (await runtime._claim_many(1, fixture["loop_id"]))[0]
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                round_row = await session.get(LoopRound, fixture["round_id"])
                binding = dict(verification_id="candidate-race", loop_id=loop.loop_id, round_id=round_row.round_id,
                    goal_revision=round_row.goal_revision, frontier_hash=round_row.frontier_hash, workspace_revision=round_row.workspace_revision)
            proposal = CompletionVerificationResult.model_validate({"criteria": [{"check_id": "tests", "status": "unknown", "explanation": "无来源"}], "conclusion": "unknown"})
            await CompletionCandidateValidator(sessions, request, binding,
                [{"check_id": "tests", "expected_evidence_kinds": ["fact"]}], ()).validate(proposal)
            if change == "pause":
                await fixture["service"].control(fixture["loop_id"], "pause")
            else:
                async with sessions.begin() as session:
                    slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == "authoritative"))
                    slot.revision += 1
            contract = CompletionVerificationContract(**binding, **proposal.model_dump(mode="json"))
            with pytest.raises(ValueError):
                await CompletionEvidenceService(sessions).record(contract, identity, retry_identity=request.retry_identity)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(CompletionVerification).where(CompletionVerification.loop_id == fixture["loop_id"])) == 0
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_complete_request_capacity_rejects_before_provider_and_receipt(tmp_path, monkeypatch):
    async def run():
        from sqlalchemy import select
        from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
        from backend.app.desktop.agent_loop.models import LoopWorkerRequest
        from test_loop_worker_recovery import _case, _fail_three

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            runtime._app_config.models[0].context_window = 4096
            runtime._app_config.models[0].curation_max_output_tokens = 512
            async with sessions.begin() as session:
                row = await session.get(LoopWorkerRequest, identity)
                row.scope = {**row.scope, "scope_description": "甲" * 4000}
            await _fail_three(runtime, fixture)
            assert provider.inputs == []
            async with sessions() as session:
                assert not tuple(await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture["loop_id"])))
                row = await session.get(LoopWorkerRequest, identity)
                assert row.status == "error" and "provider_request_window" in json.dumps(row.result)
                assert (row.attempt, row.max_attempts) == (3, 3)
    asyncio.run(run())


async def _record_legacy_tap(sessions, fixture, root):
    import subprocess
    import uuid
    from pathlib import Path
    from datetime import UTC, datetime
    from langchain_core.messages import ToolMessage
    from focus.history import serialize_history_message, content_hash
    from backend.app.desktop.models import DesktopRun, ToolExecutionAttempt
    from backend.app.desktop.domain_evidence.repository import DomainResultRepository

    path = Path(root) / "legacy-tap.cjs"
    path.write_text('require("node:test")("persists prompt", () => {});', encoding="utf-8")
    executed = subprocess.run(["node", "--test", "--test-reporter=tap", str(path)], cwd=root, capture_output=True, text=True, timeout=20)
    assert executed.returncode == 0
    run_id, call_id, attempt_id = (uuid.uuid4().hex for _ in range(3))
    command = "node --test --test-reporter=tap legacy-tap.cjs"
    output = json.dumps({"run_id": run_id, "call_id": call_id, "workspace": str(root), "status": "exited", "exit_code": 0, "output": executed.stdout})
    parsed = TestResultParser.parse("test", output, command=command, run_id=run_id, call_id=call_id)
    assert parsed.pop("named_results")["status"] == "complete"
    payload = {**parsed, "workspace_revision": 1}
    async with sessions.begin() as session:
        session.add(DesktopRun(run_id=run_id, task_id=fixture["context_id"], agent_id="fixture:" + run_id,
            kind="worker", status="success", loop_id=fixture["loop_id"], round_id=fixture["round_id"],
            equipment={"permissions": ["read", "host_command"]}, workspace_result={"revision": 1, "workspace_status": "settled"}, settled_at=datetime.now(UTC)))
        await session.flush()
        session.add(ToolExecutionAttempt(attempt_id=attempt_id, run_id=run_id, call_id=call_id,
            call_hash=content_hash({"id": call_id, "name": "test", "args": {"command": command}}), status="completed",
            result=serialize_history_message(ToolMessage(content=output, name="test", tool_call_id=call_id))))
        key = await DomainResultRepository().record(session, loop_id=fixture["loop_id"], context_id=fixture["context_id"],
            run_id=run_id, kind="test", source_id=call_id, payload=payload)
    return key, payload


@pytest.mark.usefixtures("isolated_postgres_database")
def test_legacy_report_derivation_preserves_source_and_uses_exact_tool_proof(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop.models import AgentLoop
        from backend.app.desktop.models import DesktopWorkspace
        from backend.app.desktop.domain_evidence.models import DesktopDomainResult
        from backend.app.desktop.agent_loop.completion_evidence_catalog import CompletionEvidenceCatalog
        from test_loop_worker_recovery import _case

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                root = (await session.get(DesktopWorkspace, loop.workspace_id)).path
            key, original = await _record_legacy_tap(sessions, fixture, root)
            async with sessions() as session:
                source = await session.get(DesktopDomainResult, key)
                frozen = {"completion_sources": [{"source_id": key, "kind": "test", "run_id": source.run_id, "payload": source.payload}]}
                view = await CompletionEvidenceCatalog().project(session, frozen, fixture["loop_id"], 1)
                derived = view["completion_sources"][0]["payload"]
                assert derived["named_results"]["status"] == "complete"
                assert derived["named_results"]["cases"][0]["name"] == "persists prompt"
                assert derived["metadata_derivation"]["source_result_key"] == key
                assert source.payload == original and frozen["completion_sources"][0]["payload"] == original
                assert "named_results" not in source.payload
    asyncio.run(run())
