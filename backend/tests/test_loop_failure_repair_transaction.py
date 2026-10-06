"""本文件对外提供失败证据驱动后继 Round 修复的生产事务链验收。

输入为逐用例隔离 PostgreSQL、TypeScript 5.9.3 fixture、可控 Patrol 与记忆模型；输出为真实红态构建、后继冻结失败来源、授权修复及绿态构建测试。
具体工作流为真实 Observation/Patrol/Kernel 决策、durable admission/dispatch、工作区绑定、工具账本执行及 finalizer/outbox 收口，再以真实持久来源验证必要检查。
模型仅控制决策，不伪造命令结果或执行身份；失败来源不可修改或删除。示例：pytest backend/tests/test_loop_failure_repair_transaction.py。
共享真实执行夹具保留用户 Directive 的 direct_user/user_intent_id 来源，仍经实际准入、启动和结算。
"""

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import pytest
from langchain_core.messages import ToolMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import powershell, write_file

from backend.app.desktop.agent_loop.completion_sources import CompletionSourceValidator
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.dispatch import LoopRunWorkspaceBinder, LoopWaveDispatcher
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDecision, LoopDelegationGrant, LoopRound
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.patrol import PortfolioPatrol
from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent
from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.domain_evidence.repository import DomainResultRepository
from backend.app.desktop.domain_evidence.tests import TestResultParser
from backend.app.desktop.execution_attempts.tool import ToolExecutionLedger
from backend.app.desktop.models import DesktopRun, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.assembler import RunExecutionAssembly
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.run_orchestration.outbox import RunOutboxConsumer
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.tests.runtime_context_support import tool_runtime
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_memory_execution_recovery import ControlledMemory
from backend.tests.test_loop_settings_build_evidence import FIXTURE
from backend.tests.test_round_task_progress import _Checkpointer

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


class ExecutionAssembler:
    def __init__(self, sessions, fixture, slot_id):
        self._sessions, self._fixture, self._slot_id = sessions, fixture, slot_id

    async def assemble(self, run_id):
        body = SimpleNamespace(context={})
        await LoopRunWorkspaceBinder(self._sessions).bind(
            run_id=run_id, loop_id=self._fixture["loop_id"], body=body, slot_id=self._slot_id)
        return RunExecutionAssembly(run_id=run_id, body=body,
            thread_id=f"thread-{self._fixture['context_id']}", agent_factory=lambda: None)


class NoCheckpoints:
    async def aget_tuple(self, config):
        return None


async def _prepare(sessions, fixture):
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
        root = Path((await session.get(DesktopWorkspace, loop.workspace_id)).path)
        shutil.copytree(FIXTURE / "src", root / "src")
        shutil.copyfile(FIXTURE / "tsconfig.json", root / "tsconfig.json")
        (root / "src/settings.test.ts").write_text('import { DEFAULT_SETTINGS } from "./settings";\nconsole.log(DEFAULT_SETTINGS);\n', encoding="utf-8")
        loop.equipment = {"permissions": ["read", "write", "host_command"], "access_mode": "danger-full-access"}
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id))
        grant.permission_scope = loop.equipment["permissions"]
        slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == "authoritative"))
        slot.current_fingerprint = WorkspaceFingerprinter().capture(root).digest
    return root


async def _authorize(sessions, fixture, claim, message, failure=None):
    observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], claim.round_id)
    if failure is not None:
        source = next(item for item in observation.task_delta["sources"] if item["source_id"] == failure["call_id"])
        assert source["kind"] == "test" and source["payload"]["status"] == "failed"
        assert source["execution_round_id"] == failure["round_id"] and source["run_id"] == failure["run_id"]

    async def model(frozen):
        return PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=f"repair:{claim.round_id}",
            loop_id=frozen.loop_id, loop_revision=frozen.loop_revision, round_id=frozen.round_id,
            holder_id=fixture["snapshot"]["holder_id"], grant_id=frozen.grant["grant_id"], grant_revision=frozen.grant["revision"],
            goal_revision=frozen.goal_revision, fencing_token=claim.fencing_token,
            observed_frontier_hash=frozen.observed_frontier_hash, observed_workspace_revision=frozen.workspace["revision"],
            observed_projection_sequence=frozen.projection_sequence, base_entity_revisions=frozen.base_entity_revisions,
            rationale=message, evidence=({"kind": "test", "source_id": failure["call_id"]},) if failure else (),
            actions=({"action": "continue_context", "context_id": fixture["context_id"],
                "context_revision_id": fixture["revision_id"], "message": message},))

    intent = await PortfolioPatrol(sessions, model).decide(observation, fixture["snapshot"]["holder_id"])
    result = await LoopKernel(sessions, require_fencing=True).commit(intent)
    assert result.status == "committed", result.reason
    memory = ControlledMemory()
    memory.release.set()
    assert await TaskProgressRuntime(sessions, None, model_factory=lambda name: memory).drain(loop_id=fixture["loop_id"]) == 1
    return observation


async def _launch(sessions, fixture, coordinator, claim):
    run_id = uuid.uuid4().hex
    slot_holder = {}

    async def admit(directive, message, slot_id):
        slot_holder["slot_id"] = slot_id
        async with sessions.begin() as session:
            loop = await session.get(AgentLoop, fixture["loop_id"])
            await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, DesktopRun(
                run_id=run_id, task_id=fixture["context_id"], agent_id=f"main:{fixture['context_id']}",
                kind="main", status="pending", origin="direct_user" if directive.origin_kind == "direct_user" else "delegated_patrol", execution_thread_id=f"thread-{fixture['context_id']}",
                origin_message_id=directive.message_id, context_revision_id=fixture["revision_id"], directive_id=directive.directive_id,
                action_id=directive.action_id, loop_id=loop.loop_id, round_id=claim.round_id,
                user_intent_id=directive.correlation_id if directive.origin_kind == "direct_user" else None,
                equipment=dict(loop.equipment), workspace_anchor={"slot_id": slot_id},
                input_messages=[{"role": "human", "id": message.id, "content": message.content}]))
        return run_id

    assert await coordinator.dispatch_ready(claim, LoopWaveDispatcher(sessions, admit), 1) == (run_id,)
    resources = SimpleNamespace(bridge=MemoryStreamBridge(), run_manager=SimpleNamespace(),
        checkpointer=NoCheckpoints(), store=None, app_config=None)
    done = asyncio.get_running_loop().create_future()
    record = RunRecord(run_id=run_id, thread_id=f"thread-{fixture['context_id']}",
        status=RunStatus.running, on_disconnect=DisconnectMode.continue_, task=done)

    async def execute(body, thread_id, adapted, factory):
        return record

    async def start(assembly):
        return await LoopRunExecutionBoundary(sessions).start(assembly, resources, execute)

    worker = DurableRunDispatchWorker(sessions, "failure-repair", ExecutionAssembler(sessions, fixture, slot_holder["slot_id"]), start)
    assert await worker.drain(limit=1) == 1
    async with sessions() as session:
        assert (await session.get(DesktopRun, run_id)).status == "running"
    await coordinator.release(claim)
    return record


async def _tool(sessions, fixture, root, record, tool, args):
    call_id, identity = uuid.uuid4().hex, uuid.uuid4().hex
    ledger = ToolExecutionLedger(sessions, authority_validator=RunOwnershipPolicy().assert_live)
    assert await ledger.claim(identity, record.run_id, {"id": call_id, "name": tool.name, "args": args, "type": "tool_call"}) is None
    runtime = tool_runtime(agent_id=f"main:{fixture['context_id']}", task_id=fixture["context_id"],
        workspace=str(root), run_id=record.run_id, permissions=("read", "write", "host_command"), access_mode=AccessMode.FULL)
    runtime.tool_call_id = call_id
    output = await asyncio.to_thread(tool.func, **args, runtime=runtime)
    await ledger.complete(identity, ToolMessage(content=output, name=tool.name, tool_call_id=call_id))
    return call_id, output


async def _finish(sessions, fixture, coordinator, record, results):
    record.task.set_result(None)
    await record.loop_activity_task
    settlement = await RunLifecycleFinalizer(sessions, NoCheckpoints()).finalize(record)
    async with sessions.begin() as session:
        run = await session.get(DesktopRun, record.run_id)
        revision = run.workspace_result["revision"]
        for call_id, parsed in results:
            await DomainResultRepository().record(session, kind="test", source_id=call_id,
                payload={**parsed, "workspace_revision": revision}, loop_id=fixture["loop_id"],
                context_id=fixture["context_id"], run_id=record.run_id)
    consumer = RunOutboxConsumer(sessions)
    assert await consumer.drain(f"repair:{fixture['loop_id']}", coordinator.handle_run_settled, loop_id=fixture["loop_id"]) == 1
    async with sessions() as session:
        run = await session.get(DesktopRun, record.run_id)
        previous = await session.get(LoopRound, run.round_id)
        assert run.settled_at is not None and previous.status == "settled"
    return revision


def _verification(fixture, revision, call_ids):
    return SimpleNamespace(loop_id=fixture["loop_id"], workspace_revision=revision, criteria=[
        SimpleNamespace(check_id="tests", status="satisfied", evidence=[SimpleNamespace(kind="test", source_id=call_id) for call_id in call_ids])])


def test_failed_build_enters_successor_observation_and_drives_authorized_repair(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="failure-repair", started_at=datetime.now(UTC))
        coordinator, validator = LoopCoordinator(sessions), CompletionSourceValidator()
        checks = [{"check_id": "tests", "required": True}]
        try:
            root = await _prepare(sessions, fixture)
            compiler = FIXTURE / "node_modules/typescript/bin/tsc"
            assert compiler.is_file()
            command = f"& node '{compiler}' --project tsconfig.json; exit $LASTEXITCODE"
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "failure-repair")
            await _authorize(sessions, fixture, claim, "执行配置接口完整构建")
            red = await _launch(sessions, fixture, coordinator, claim)
            red_call, output = await _tool(sessions, fixture, root, red, powershell, {"command": command})
            assert json.loads(output)["exit_code"] != 0 and "TS2305" in output
            parsed = TestResultParser.parse("powershell", output, command=command, run_id=red.run_id, call_id=red_call)
            assert parsed["status"] == "failed"
            red.status, red.error = RunStatus.error, "TS2305: DEFAULT_SETTINGS is not exported"
            red_revision = await _finish(sessions, fixture, coordinator, red, [(red_call, parsed)])
            async with sessions() as session:
                with pytest.raises(ValueError):
                    await validator.validate(session, checks, _verification(fixture, red_revision, [red_call]))
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "failure-repair")
            assert claim.round_id != fixture["round_id"]
            await _authorize(sessions, fixture, claim, "依据 TS2305 失败来源修复调用方并重新运行全部检查",
                {"call_id": red_call, "run_id": red.run_id, "round_id": fixture["round_id"]})
            green = await _launch(sessions, fixture, coordinator, claim)
            await _tool(sessions, fixture, root, green, write_file,
                {"path": "src/settings.test.ts", "content": (FIXTURE / "src/settings.test.ts").read_text(encoding="utf-8")})
            results = []
            for next_command in (command, "& node --test --test-reporter=tap dist/settings.test.js; exit $LASTEXITCODE"):
                call_id, output = await _tool(sessions, fixture, root, green, powershell, {"command": next_command})
                assert json.loads(output)["exit_code"] == 0
                result = TestResultParser.parse("powershell", output, command=next_command, run_id=green.run_id, call_id=call_id)
                assert result["status"] == "verified"
                results.append((call_id, result))
            assert results[-1][1]["metrics"]["passed"] == 1
            green.status = RunStatus.success
            revision = await _finish(sessions, fixture, coordinator, green, results)
            assert revision > red_revision
            async with sessions() as session:
                await validator.validate(session, checks, _verification(fixture, revision, [call_id for call_id, result in results]))
                with pytest.raises(ValueError):
                    await validator.validate(session, checks, _verification(fixture, revision, [red_call]))
                failed_source = await session.scalar(select(DesktopDomainResult).where(DesktopDomainResult.source_id == red_call))
                assert failed_source.payload["status"] == "failed"
                decisions = tuple((await session.scalars(select(LoopDecision).where(LoopDecision.loop_id == fixture["loop_id"])) ).all())
                assert len(decisions) == 2 and all(item.status == "committed" for item in decisions)
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
