"""本文件对外提供实际文件工具到必要 Artifact 检查的生产入口回归。

输入为隔离 Loop、实际授权/dispatch/文件工具/结算及可信工具审计；输出为可复核产物、重复幂等和
缺失/错误/参数篡改/内容变化/越界拒绝断言。具体工作流为经真实运行链及 Security→执行 ledger→文件工具读取或写入，再用实际 checkpoint
消息合同记录领域来源，必要检查只接受当前内容和调用证明。示例：pytest backend/tests/test_loop_artifact_source_contract.py。
实际工具用例使用逐用例 runtime 数据库，独占其派发队列；其余合同用例保留原夹具，所有来源和状态断言保持。
真实成功文件链同时核对完整正文派生与不可变原 payload；正文不需要 Verifier 工具权限。
"""

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.completion_sources import CompletionSourceValidator
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant
from backend.app.desktop.domain_evidence.artifacts import ArtifactObservationRecorder
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.domain_evidence.run_outcomes import RunOutcomeRecorder
from backend.app.desktop.models import DesktopRun, DesktopWorkspace
from backend.app.desktop.execution_attempts.tool import ToolExecutionLedger
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_failure_repair_transaction import _authorize, _launch, _finish
from backend.tests.runtime_context_support import tool_runtime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from focus.runtime.tool_attempts import ToolExecutionMiddleware
from focus.security.middleware import AccessPolicyMiddleware
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import write_file, read_file

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _file_tool(sessions, fixture, root, record, tool, args):
    call_id = uuid.uuid4().hex
    call = {"id": call_id, "name": tool.name, "args": args, "type": "tool_call"}
    runtime = tool_runtime(agent_id=f"main:{fixture['context_id']}", task_id=fixture["context_id"],
        workspace=str(root), run_id=record.run_id, permissions=("read", "write", "host_command"), access_mode=AccessMode.FULL)
    runtime.context["tool_execution_ledger"] = ToolExecutionLedger(sessions, authority_validator=RunOwnershipPolicy().assert_live)
    runtime.state["messages"] = [AIMessage(id="actual-file-call", content="", tool_calls=[call])]
    runtime.tool_call_id = call_id
    request = ToolCallRequest(tool_call=call, tool=tool, state=runtime.state, runtime=runtime)

    async def execute(current):
        output = await asyncio.to_thread(tool.func, **current.tool_call["args"], runtime=current.runtime)
        return ToolMessage(content=output, name=tool.name, tool_call_id=call_id)

    async def execute_once(current):
        return await ToolExecutionMiddleware().awrap_tool_call(current, execute)

    result = await AccessPolicyMiddleware().awrap_tool_call(request, execute_once)
    assert result.status == "success"
    return call_id, result.content


@pytest.mark.usefixtures("runtime_postgres_database")
@pytest.mark.parametrize("tool", [write_file, read_file])
def test_real_file_tool_records_current_artifact_and_rejects_stale_or_forged_calls(tmp_path, tool):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="artifact-proof", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        try:
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                root = Path((await session.get(DesktopWorkspace, loop.workspace_id)).path)
                loop.equipment = {"permissions": ["read", "write", "host_command"], "access_mode": "danger-full-access"}
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id))
                grant.permission_scope = loop.equipment["permissions"]
            (root / "manifest.json").write_text('{"id":"test-pet"}\n', encoding="utf-8")
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "artifact-proof")
            await _authorize(sessions, fixture, claim, "核验仓库 manifest 文件")
            record = await _launch(sessions, fixture, coordinator, claim)
            args = {"path": "manifest.json"}
            if tool is write_file:
                args["content"] = '{"id":"test-pet"}\n'
            call_id, output = await _file_tool(sessions, fixture, root, record, tool, args)
            from focus.runtime.runs.schemas import RunStatus

            record.status = RunStatus.success
            revision = await _finish(sessions, fixture, coordinator, record, [])
            call = {"id": call_id, "name": tool.name, "args": args, "type": "tool_call"}
            async with sessions.begin() as session:
                row = await session.get(DesktopRun, record.run_id)
                messages = ({"id": row.origin_message_id, "role": "human", "content": "核验文件"},
                    {"id": "file-call", "role": "ai", "tool_calls": [call]},
                    {"id": "file-result", "role": "tool", "name": tool.name, "tool_call_id": call_id, "content": output})
                await RunOutcomeRecorder().record(session, row, messages)
                await RunOutcomeRecorder().record(session, row, messages)
                sources = tuple(await session.scalars(select(DesktopDomainResult).where(
                    DesktopDomainResult.run_id == row.run_id, DesktopDomainResult.kind == "artifact")))
                assert len(sources) == 1 and sources[0].payload["path"] == "manifest.json"
                proof = sources[0].payload["execution_proof"]
                assert proof["call_id"] == call_id and proof["run_id"] == row.run_id
                contract = SimpleNamespace(loop_id=fixture["loop_id"], workspace_revision=revision,
                    criteria=[SimpleNamespace(check_id="files", status="satisfied", evidence=[
                        SimpleNamespace(kind="artifact", source_id=sources[0].result_key)])])
                checks = [{"check_id": "files", "required": True}]
                await CompletionSourceValidator().validate(session, checks, contract)
                from backend.app.desktop.agent_loop.completion_evidence_catalog import CompletionEvidenceCatalog

                frozen = {'completion_sources': [{'source_id': sources[0].result_key, 'kind': 'artifact',
                    'run_id': row.run_id, 'payload': sources[0].payload}]}
                view = await CompletionEvidenceCatalog().project(session, frozen, fixture['loop_id'], revision)
                assert view['completion_sources'][0]['artifact_content']['text'] == (root / 'manifest.json').read_bytes().decode('utf-8')
                assert 'artifact_content' not in sources[0].payload
                changed_call = {**call, "args": {**args, "path": "README.md"}}
                (root / "README.md").write_text("unrelated", encoding="utf-8")
                await ArtifactObservationRecorder().record(session, row, {call_id: changed_call})
                assert await session.scalar(select(func.count()).select_from(DesktopDomainResult).where(
                    DesktopDomainResult.run_id == row.run_id, DesktopDomainResult.kind == "artifact")) == 1
                (root / "manifest.json").write_text("changed after verification", encoding="utf-8")
                with pytest.raises(ValueError, match="内容已过期"):
                    await CompletionSourceValidator().validate(session, checks, contract)
                await ArtifactObservationRecorder().record(session, row, {call_id: call})
                assert await session.scalar(select(func.count()).select_from(DesktopDomainResult).where(
                    DesktopDomainResult.run_id == row.run_id, DesktopDomainResult.kind == "artifact")) == 1
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["missing", "outside", "changed", "invalid_content"])
def test_file_observation_does_not_invent_artifacts(tmp_path, case):
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "manifest.json"
    path.write_text("actual", encoding="utf-8")
    args = {"path": "manifest.json", "content": "actual"}
    if case == "missing":
        args["path"] = "missing.json"
    elif case == "outside":
        (tmp_path / "outside.json").write_text("actual", encoding="utf-8")
        args["path"] = "../outside.json"
    elif case == "changed":
        args["content"] = "unconfirmed"
    else:
        args["content"] = 123
    assert ArtifactObservationRecorder._observe(root, {"name": "write_file", "args": args}, "ok") is None
