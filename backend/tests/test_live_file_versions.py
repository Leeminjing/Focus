"""本文件验证真实写文件、耐久版本、outbox 重放、历史预览及普通输入引用。

输入为隔离 PostgreSQL、临时文件与生产工具；输出为 Run 未结算即可读、原版本不漂移和越界拒绝断言。
工作流复用生产受理、执行账本、事实投影和详情；Shell 测试仅用 Git 已知路径，不信任 stdout。
示例：python -m pytest backend/tests/test_live_file_versions.py -q。
"""

import asyncio
from dataclasses import replace
from hashlib import sha256
import subprocess
import uuid

import pytest
from fastapi import HTTPException
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from backend.tests.test_workspace_patrol import _setup
from backend.tests.test_workspace_patrol_decisions import _prepare
from backend.tests.runtime_context_support import tool_runtime
from backend.app.desktop.agent_loop.models import AgentLoop, LoopUserIntent
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRequest, FileReference, request_document
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.materialized_fact_query import MaterializedFactQueryService
from backend.app.desktop.agent_loop.live_access import LoopLiveRedactionPolicy
from backend.app.desktop.agent_loop.event_contract import LiveEventAuthorizationPolicy
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.materialized_fact_sources import RunFactSourceReader
from backend.app.desktop.domain_evidence.file_versions import FileVersionCapture, FileVersionRecorder
from backend.app.desktop.domain_evidence.file_reads import FileVersionReader
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.execution_attempts import ToolExecutionLedger
from backend.app.desktop.run_orchestration.outbox import RunOutboxConsumer
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from focus.tools.builtins.workspace_tools import write_file
from focus.security.context import security_context_of
from focus.runtime.tool_attempts import ToolExecutionUncertain


def test_snapshot_boundaries_and_shell_evidence(tmp_path):
    path = tmp_path / "plan.md"
    capture = FileVersionCapture("one", tmp_path, ("private-value",))
    capture.before_write(path)
    path.write_text("token=private-value\nplan", encoding="utf-8")
    capture.after_write(path)
    version = capture.changes[0]
    assert version["operation"] == "created" and version["before"]["exists"] is False
    assert "private-value" not in str(version)
    assert version["after"]["sha256"] == sha256(path.read_bytes()).hexdigest()
    assert version["after"]["display_hash"] != version["after"]["sha256"]
    capture.before_write(path); capture.after_write(path)
    assert len(capture.changes) == 1
    capture.after_write(tmp_path.parent / "outside.txt")
    assert len(capture.changes) == 1
    other = FileVersionCapture("two", tmp_path)
    other.after_write(path)
    assert other.changes[0]["before"]["reason"] == "before_unobserved"
    assert other.changes[0]["slot_id"] != version["slot_id"]
    concurrent = FileVersionCapture("one", tmp_path)
    concurrent.before_write(path)
    path.write_text("external writer", encoding="utf-8")
    concurrent.after_write(path, "expected tool bytes")
    assert concurrent.changes == []
    non_git = FileVersionCapture("one", tmp_path)
    non_git.before_shell(tmp_path)
    non_git.after_shell("confirmed")
    assert non_git.changes == []
    for raw, reason in [(b"x" * 1048577, "over_1_mib"), (b"a\0b", "binary"), (b"\xff", "non_utf8")]:
        other.before_write(path); path.write_bytes(raw); other.after_write(path)
        assert other.changes[-1]["after"]["reason"] == reason
        assert "text" not in other.changes[-1]["after"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path.write_text("old", encoding="utf-8")
    shell = FileVersionCapture("one", tmp_path)
    shell.before_shell(tmp_path)
    path.write_text("new", encoding="utf-8")
    shell.after_shell("unknown")
    assert shell.changes == []
    shell.after_shell("confirmed")
    assert shell.changes[0]["before"]["text"] == "old"
    assert shell.changes[0]["after"]["text"] == "new"
    deleted = FileVersionCapture("one", tmp_path)
    deleted.before_shell(tmp_path)
    path.unlink()
    deleted.after_shell("confirmed")
    assert deleted.changes[0]["operation"] == "deleted"


def test_live_versions_atomic_replay_and_frozen_input(isolated_postgres_database, tmp_path, monkeypatch):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            initial = await service.submit(workspace_id, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="输出方案"))
            loop_id = initial["loop_id"]
            await _prepare(sessions, loop_id)
            run_id, slot_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id)
                slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == workspace_id))
                if slot:
                    slot_id = slot.slot_id
                else:
                    session.add(WorkspaceSlot(slot_id=slot_id, workspace_id=workspace_id, kind="authoritative", root_path=str(tmp_path), current_fingerprint="a" * 64))
                session.add(DesktopRun(run_id=run_id, task_id=loop.initial_context_id, agent_id="main", kind="main", status="running",
                    input_messages=[], loop_id=loop_id, workspace_anchor={"slot_id": slot_id}, workspace_result={"artifact_paths": ["plan.md"]}))
            ledger = ToolExecutionLedger(sessions, secret_provider=lambda: ("private-value",))
            recorder, consumer, projector = FileVersionRecorder(), RunOutboxConsumer(sessions), FactProjector(sessions, None)
            async def publish(event, session):
                await recorder.publish(session, event)
            for index, content in enumerate(("version one", "version two")):
                call = {"id": f"call-{index}", "name": "write_file", "args": {"path": str(tmp_path / "plan.md"), "content": content}}
                identity = str(index) * 64
                assert await ledger.claim(identity, run_id, call) is None
                capture = await ledger.prepare_file_capture(run_id)
                runtime = tool_runtime(agent_id="main", task_id=loop.initial_context_id, run_id=run_id, workspace=str(tmp_path), permissions=("read", "write"), extras={"file_version_capture": capture})
                security = replace(security_context_of(runtime.context), extras={"file_version_capture": capture})
                runtime = replace(runtime, context={**runtime.context, **security.to_runtime_context()})
                output = write_file.func(**call["args"], runtime=runtime)
                result = ToolMessage(content=output, tool_call_id=call["id"], name="write_file")
                await ledger.complete(identity, result, file_capture=capture)
                assert (await ledger.claim(identity, run_id, call)).content == output
                assert await consumer.drain("file-test", publish, loop_id=loop_id) == 1
                assert await consumer.drain("file-test", publish, loop_id=loop_id) == 0
                await projector.project_loop(loop_id)
            async with sessions() as session:
                live_run = await session.get(DesktopRun, run_id)
                assert live_run.settled_at is None
                live_run.workspace_result = {"artifact_paths": ["plan.md", str(tmp_path / "plan.md"), "legacy.txt"]}
                summaries = await RunFactSourceReader(None).read(session, (live_run,))
                assert [item["summary"] for item in summaries if item["kind"] == "artifact"] == ["legacy.txt"]
                sources = tuple(await session.scalars(select(DesktopDomainResult).where(DesktopDomainResult.run_id == run_id, DesktopDomainResult.kind == "artifact")))
                assert len(sources) == 2
                facts = tuple(await session.scalars(select(LoopFact).where(LoopFact.loop_id == loop_id, LoopFact.fact_type == "artifact").order_by(LoopFact.occurred_at)))
                assert len(facts) == 2
                details = [await MaterializedFactQueryService().detail(session, loop_id, fact.fact_id) for fact in facts]
                first = details[0]["file"]
                assert first["importance"] == "declared_artifact"
                readable = LoopLiveRedactionPolicy.redact_value(details[0], frozenset({"read"}))
                assert readable["file"]["after"]["text"] == "version one"
                assert readable["fact"]["file"]["result_key"] == first["result_key"]
                assert "evidence" not in readable["fact"]
                assert "file" not in LoopLiveRedactionPolicy.redact_value(details[0], frozenset())
                events = await LoopEventJournal().read(session, loop_id, 0, 1000)
                event = next(item for item in events if item.kind == "fact.upserted" and item.payload.get("presentation", {}).get("file"))
                public = LiveEventAuthorizationPolicy().redact(event, frozenset({"read"})).payload
                assert public["presentation"]["file"]["path"] == "plan.md" and "evidence" not in public
                assert "file" not in LiveEventAuthorizationPolicy().redact(event, frozenset()).payload["presentation"]
                assert first["after"]["text"] == "version one"
                assert details[1]["file"]["before"]["text"] == "version one"
                reference = {key: first[key] for key in ("fact_id", "revision", "result_key", "slot_id", "sha256", "path")}
                with pytest.raises(HTTPException):
                    await FileVersionReader().validate_reference(session, "foreign", FileReference(**reference))
            request = PatrolInputRequest(submission_id=uuid.uuid4().hex, content="  @方案 不满意，请分析原因\n", file_refs=(reference,))
            receipt = await service.submit(workspace_id, request)
            (tmp_path / "plan.md").write_text("version three", encoding="utf-8")
            assert await service.submit(workspace_id, request) == receipt
            with pytest.raises(HTTPException):
                await service.submit(workspace_id, request.model_copy(update={"file_refs": ()}))
            async with sessions() as session:
                row = await session.get(LoopUserIntent, receipt["intent_id"])
                frozen = LoopObservationService._intent_view(row)
                assert frozen["content"] == request.content
                assert frozen["file_refs"][0]["sha256"] == first["sha256"]
                assert (await FileVersionReader().read(session, loop_id, first["fact_id"], first["revision"]))["after"]["text"] == "version one"
            assert "file_refs" not in request_document(PatrolInputRequest(submission_id="legacy", content="普通输入"))
            call = {"id": "failed-commit", "name": "write_file", "args": {"path": str(tmp_path / "plan.md"), "content": "uncommitted"}}
            await ledger.claim("f" * 64, run_id, call)
            capture = await ledger.prepare_file_capture(run_id)
            capture.before_write(call["args"]["path"])
            (tmp_path / "plan.md").write_text("uncommitted", encoding="utf-8")
            capture.after_write(call["args"]["path"])
            async def fail_record(*args):
                raise RuntimeError("模拟提交失败")
            with monkeypatch.context() as patch:
                patch.setattr(FileVersionRecorder, "record", fail_record)
                with pytest.raises(RuntimeError, match="提交失败"):
                    await ledger.complete("f" * 64, ToolMessage(content="written", tool_call_id=call["id"]), file_capture=capture)
            with pytest.raises(ToolExecutionUncertain):
                await ledger.claim("f" * 64, run_id, call)
            assert await consumer.drain("file-test", publish, loop_id=loop_id) == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
