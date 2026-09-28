r"""本文件对外提供 Loop Run 通用调度路径与真实桌面首个 Live GET 故障恢复的回归测试。

输入为隔离 PostgreSQL 中已授权的 Loop directive、带稳定消息身份的 Main Run、模拟的 LangGraph values 里程碑和一次注入的 HTTP 500；输出为 Run 尚未结算时可按 Loop/Run 身份读取的工具活动事件与桌面恢复证据。
具体工作流为令通用 durable worker 认领 Run，通过 DesktopService 的共同启动入口发布工具调用，再以真实 Electron 检查首次 Live 失败后恢复并查询 per-Loop journal。示例：`pytest backend/tests/test_loop_run_live_dispatch.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from urllib.request import urlopen
import uuid

import pytest
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop import service as desktop_service_module
from backend.app.desktop.agent_loop import AgentLoopService, LoopCreateRequest
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.live_api import LoopLiveEventFeed, LoopLiveSnapshotService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDirective
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.agent_loop.run_activity_bridge import LoopRunActivityBridge
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.assembler import RunExecutionAssembly
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker, RunDispatchRepository
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.service import DesktopService, PreparedRun
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceSlot
from backend.app.desktop.context_evolution import (
    ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository,
)

from test_agent_loop_round_liveness import _seed_loop, _stop
from test_loop_execution_ownership import _seed_launching_directive


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _seed_active_first_round(sessions, tmp_path: Path) -> dict:
    suffix = uuid.uuid4().hex[:8]
    workspace_id = f"ws-first-{suffix}"
    context_id = f"context-first-{suffix}"
    revision_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    anchor_message_id = f"message-{uuid.uuid4().hex}"
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="first round"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="first round"))
        await session.flush()
        ref = ContextRevisionRef(
            context_id=context_id, revision_id=revision_id, generation=1,
            execution_thread_id=f"thread-{context_id}", checkpoint_ns="",
            checkpoint_id=f"checkpoint-{context_id}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        )
        await ContextRevisionRepository().insert(session, ContextRevisionContract(
            ref=ref, content_hash="a" * 64, projection_status=ContextRevisionProjectionStatus.VALID,
            origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC),
        ))
        await ContextRevisionRepository().switch_current(session, ref, None)
        admission = await RunAdmissionService().admit(session, DesktopRun(
            run_id=run_id, task_id=context_id, agent_id=f"main:{context_id}",
            kind="main", status="pending", origin="direct_user",
            execution_thread_id=f"thread-{context_id}", origin_message_id=anchor_message_id,
            context_revision_id=revision_id,
            input_messages=[{"role": "human", "id": anchor_message_id, "content": "开始首轮测试"}],
            equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
            workspace_anchor={"workspace_id": workspace_id, "workspace_path": str(workspace_path)},
        ))
        admission.dispatch.accepted_at = datetime(2000, 1, 1, tzinfo=UTC)
    service = AgentLoopService(sessions)
    snapshot = await service.start(LoopCreateRequest(
        loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id,
        initial_run_id=run_id, holder_id=f"patrol-{suffix}",
        goal="Observe the first active Context Run", task_contract="Show confirmed progress",
        acceptance_criteria=({"criterion_id": "live", "text": "live activity visible"},),
        capabilities=("continue_context", "request_completion"),
        context_scope=(context_id,), permission_scope=("read", "write"),
    ))
    async with sessions.begin() as session:
        slot = await session.scalar(select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == workspace_id, WorkspaceSlot.kind == "authoritative",
        ))
        run = await session.get(DesktopRun, run_id, with_for_update=True)
        run.workspace_anchor = {**run.workspace_anchor, "slot_id": slot.slot_id}
    return {
        "service": service, "loop_id": loop_id, "context_id": context_id,
        "revision_id": revision_id, "round_id": snapshot["current_round_id"],
        "snapshot": snapshot, "run_id": run_id, "anchor_message_id": anchor_message_id,
    }


def test_generic_dispatch_publishes_loop_tool_activity_before_settlement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="live-dispatch", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="live-dispatch")
            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
                anchor_message_id = directive.message_id
                slot = await session.scalar(select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == fixture["snapshot"]["workspace_id"],
                    WorkspaceSlot.kind == "authoritative",
                ))
                assert slot is not None
                slot_id = slot.slot_id
            run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                admission = await RunAdmissionService().admit(
                    session,
                    DesktopRun(
                        run_id=run_id,
                        task_id=fixture["context_id"],
                        agent_id=f"main:{fixture['context_id']}",
                        kind="main",
                        status="pending",
                        origin="delegated_patrol",
                        execution_thread_id=f"thread-{fixture['context_id']}",
                        origin_message_id=anchor_message_id,
                        context_revision_id=fixture["revision_id"],
                        directive_id=directive_id,
                        loop_id=fixture["loop_id"],
                        round_id=fixture["round_id"],
                        input_messages=[{"role": "human", "id": anchor_message_id, "content": directive.content}],
                        equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
                        workspace_anchor={"slot_id": slot_id},
                    ),
                )
                admission.dispatch.accepted_at = datetime(2000, 1, 1, tzinfo=UTC)

            service = object.__new__(DesktopService)
            service.session_factory = sessions
            service.bridge = MemoryStreamBridge()
            service.run_manager = SimpleNamespace()
            service.checkpointer = SimpleNamespace()
            service.store = SimpleNamespace()
            service.app_config = SimpleNamespace()
            execution_done = asyncio.get_running_loop().create_future()

            async def execute(body, thread_id, resources, factory):
                resources.bridge.publish(
                    run_id,
                    StreamEvent(
                        id="",
                        event="events",
                        data={"data": {"messages": [
                            {"role": "human", "id": anchor_message_id, "content": "directive"},
                            {"role": "ai", "id": "assistant-1", "tool_calls": [{"id": "call-1", "name": "pytest"}]},
                        ]}},
                    ),
                )
                return SimpleNamespace(run_id=run_id, task=execution_done)

            monkeypatch.setattr(desktop_service_module, "execute_prepared_run", execute)

            class Assembler:
                async def assemble(self, requested_run_id: str) -> RunExecutionAssembly:
                    return RunExecutionAssembly(
                        run_id=requested_run_id,
                        body=SimpleNamespace(context={"run_id": requested_run_id}),
                        thread_id=f"thread-{fixture['context_id']}",
                        agent_factory=lambda: None,
                    )

            worker = DurableRunDispatchWorker(
                sessions,
                "desktop-main:test",
                Assembler(),
                service._start_dispatched_run,
                RunDispatchRepository(),
            )
            assert await worker.drain(limit=1) == 1

            found = []
            for _ in range(20):
                async with sessions() as session:
                    found = list((await session.scalars(select(LoopJournalEvent).where(
                        LoopJournalEvent.loop_id == fixture["loop_id"],
                        LoopJournalEvent.kind == "context.tool.started",
                        LoopJournalEvent.entity_id == "call-1",
                    ))).all())
                if found:
                    break
                await asyncio.sleep(0.05)
            assert len(found) == 1, "通用 worker 执行中的工具活动必须在 Run 结算前进入 Loop journal"
            assert found[0].payload["run_id"] == run_id
            execution_done.set_result(None)
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_generic_assembly_uses_the_planned_isolated_slot(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="planned-slot", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="planned-slot")
            isolated_root = tmp_path / "planned-isolated"
            isolated_root.mkdir()
            slot_id = uuid.uuid4().hex
            run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                task = await session.get(DesktopThread, fixture["context_id"])
                session.add(WorkspaceSlot(
                    slot_id=slot_id,
                    workspace_id=task.workspace_id,
                    kind="isolated",
                    root_path=str(isolated_root),
                    provider="local",
                    current_fingerprint="f" * 64,
                    owner_loop_id=fixture["loop_id"],
                ))
                await session.flush()
                await RunAdmissionService().admit(session, DesktopRun(
                    run_id=run_id,
                    task_id=fixture["context_id"],
                    agent_id=f"main:{fixture['context_id']}",
                    kind="main",
                    status="pending",
                    origin="delegated_patrol",
                    execution_thread_id=f"thread-{fixture['context_id']}",
                    context_revision_id=fixture["revision_id"],
                    directive_id=directive_id,
                    loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"],
                    equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
                    workspace_anchor={"workspace_id": task.workspace_id, "workspace_path": str(isolated_root), "slot_id": slot_id},
                ))

            service = object.__new__(DesktopService)
            service.session_factory = sessions
            seen_paths = []

            async def prepare(*args, **kwargs):
                seen_paths.append(args[3])
                return PreparedRun(
                    body=SimpleNamespace(context={}),
                    thread_id=f"thread-{fixture['context_id']}",
                    agent_factory=lambda: None,
                    payload={},
                )

            service._prepare = prepare
            await service.assemble_run(run_id)
            async with sessions() as session:
                anchor = await session.get(RunExecutionAnchor, run_id)
                stored = await session.get(DesktopRun, run_id)
            assert seen_paths == [str(isolated_root)]
            assert anchor.slot_id == slot_id
            assert anchor.directive_id == directive_id
            assert stored.workspace_anchor["slot_id"] == slot_id
            await RunLifecycleFinalizer(sessions, SimpleNamespace()).abort_prepared(run_id, "test fixture complete")
            async with sessions.begin() as session:
                dispatch = await RunDispatchRepository().by_run(session, run_id)
                dispatch.status = "failed_to_start"
                dispatch.settled_at = datetime.now(UTC)
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_live_activity_is_idempotent_private_and_replayable(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="activity-replay", started_at=datetime.now(UTC))
            run_id = uuid.uuid4().hex
            bridge = LoopRunActivityBridge(
                MemoryStreamBridge(), sessions,
                loop_id=fixture["loop_id"], context_id=fixture["context_id"],
                run_id=run_id, correlation_id=f"run:{run_id}", anchor_message_id="anchor",
            )
            event = StreamEvent(
                id="", event="events",
                data={"data": {"messages": [
                    {"role": "human", "id": "anchor", "content": "start"},
                    {"role": "ai", "id": "model-1", "content": "secret-token-123", "tool_calls": [{"id": "tool-1", "name": "pytest", "args": {"token": "secret-token-123"}}]},
                    {"role": "tool", "id": "result-1", "tool_call_id": "tool-1", "name": "pytest", "status": "success", "content": "secret-token-123", "files": [{"path": "C:/secret-token-123.txt"}]},
                ]}},
            )
            bridge.publish(run_id, event)
            bridge.publish(run_id, event)
            await bridge.close()
            async with sessions() as session:
                events = list((await session.scalars(select(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"],
                    LoopJournalEvent.payload["run_id"].astext == run_id,
                ).order_by(LoopJournalEvent.sequence))).all())
            assert [item.kind for item in events] == [
                "context.model.completed", "context.tool.started",
                "context.tool.completed", "context.artifact.observed",
            ]
            assert "secret-token-123" not in json.dumps([item.payload for item in events])
            replay = await LoopLiveEventFeed(sessions).read_batch(fixture["loop_id"], events[0].sequence)
            replayed = [item for item in replay["events"] if item["payload"].get("run_id") == run_id]
            assert [item["kind"] for item in replayed] == [item.kind for item in events[1:]]
            assert next(item for item in replayed if item["kind"] == "context.tool.started")["payload"]["summary"] == "pytest 正在执行"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_activity_write_retries_a_transient_journal_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="activity-retry", started_at=datetime.now(UTC))
            run_id = uuid.uuid4().hex
            bridge = LoopRunActivityBridge(
                MemoryStreamBridge(), sessions, loop_id=fixture["loop_id"], context_id=fixture["context_id"],
                run_id=run_id, correlation_id=f"run:{run_id}", anchor_message_id="anchor",
            )
            original = bridge._writer._journal.append
            attempts = 0

            async def transient(session, loop_id, draft):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("temporary journal connection failure")
                return await original(session, loop_id, draft)

            monkeypatch.setattr(bridge._writer._journal, "append", transient)
            event = StreamEvent(id="", event="events", data={"data": {"messages": [
                {"role": "human", "id": "anchor"}, {"role": "ai", "id": "model-retry"},
            ]}})
            bridge.publish(run_id, event)
            bridge.publish(run_id, event)
            await bridge.close()
            async with sessions() as session:
                rows = list((await session.scalars(select(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"],
                    LoopJournalEvent.kind == "context.model.completed",
                    LoopJournalEvent.entity_id == "model-retry",
                ))).all())
            assert attempts == 2
            assert len(rows) == 1
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_exhausted_activity_write_is_visible_as_degraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="activity-down", started_at=datetime.now(UTC))
            run_id = uuid.uuid4().hex
            bridge = LoopRunActivityBridge(
                MemoryStreamBridge(), sessions, loop_id=fixture["loop_id"], context_id=fixture["context_id"],
                run_id=run_id, correlation_id=f"run:{run_id}", anchor_message_id="anchor",
            )

            async def broken(session, loop_id, draft):
                raise ConnectionError("journal unavailable")

            monkeypatch.setattr(bridge._writer._journal, "append", broken)
            bridge.publish(run_id, StreamEvent(id="", event="events", data={"data": {"messages": [
                {"role": "human", "id": "anchor"}, {"role": "ai", "id": "model-failed"},
            ]}}))
            with pytest.raises(RuntimeError, match="unpersisted event"):
                await bridge.close()
            async with sessions() as session:
                failure = await session.scalar(select(LoopProjectionFailure).where(
                    LoopProjectionFailure.loop_id == fixture["loop_id"],
                    LoopProjectionFailure.projector_name == "run_activity",
                ))
                snapshot = await LoopLiveSnapshotService().read(session, fixture["loop_id"])
            assert failure is not None
            assert failure.status == "quarantined"
            assert failure.unit_id == f"context-model:{run_id}:model-failed:completed"
            assert snapshot["diagnostics"]["recovery_status"] == "degraded"
            assert "run_activity" in snapshot["diagnostics"]["degraded_scope"]
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_one_run_reaches_live_sse_and_real_electron_before_settlement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    electron = Path(os.environ.get("FOCUS_ELECTRON_EXECUTABLE", "desktop/node_modules/electron/dist/electron.exe"))
    if sys.platform != "win32" or not electron.is_file():
        pytest.skip("真实桌面验收需要 Windows Electron；设置 FOCUS_ELECTRON_EXECUTABLE")

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        server = desktop = None
        server_log = desktop_log = None
        try:
            fixture = await _seed_active_first_round(sessions, tmp_path)
            run_id = fixture["run_id"]
            service = object.__new__(DesktopService)
            service.session_factory = sessions
            service.bridge = MemoryStreamBridge()
            service.run_manager = SimpleNamespace(cancel=lambda *args, **kwargs: None)
            service.checkpointer = SimpleNamespace()
            service.store = SimpleNamespace()
            service.app_config = SimpleNamespace()
            execution_done = asyncio.get_running_loop().create_future()
            live_bridge = {}

            async def execute(body, thread_id, resources, factory):
                live_bridge["value"] = resources.bridge
                return SimpleNamespace(run_id=run_id, task=execution_done)

            monkeypatch.setattr(desktop_service_module, "execute_prepared_run", execute)

            class Assembler:
                async def assemble(self, requested_run_id):
                    return RunExecutionAssembly(
                        run_id=requested_run_id, body=SimpleNamespace(context={"run_id": requested_run_id}),
                        thread_id=f"thread-{fixture['context_id']}", agent_factory=lambda: None,
                    )

            worker = DurableRunDispatchWorker(
                sessions, "desktop-main:real-ui", Assembler(), service._start_dispatched_run, RunDispatchRepository(),
            )
            assert await worker.drain(limit=1) == 1
            async with sessions() as session:
                dispatch = await RunDispatchRepository().by_run(session, run_id)
                stored = await session.get(DesktopRun, run_id)
            assert dispatch.status == "running" and dispatch.claimed_by == "desktop-main:real-ui"
            assert stored.status == "running" and stored.round_id == fixture["round_id"]

            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            repo = Path(__file__).resolve().parents[2]
            env = os.environ.copy()
            env["FOCUS_LOOP_E2E_LOOP_ID"] = fixture["loop_id"]
            env["FOCUS_LOOP_E2E_CONTEXT_ID"] = fixture["context_id"]
            env["FOCUS_LOOP_E2E_RUN_ID"] = run_id
            env["FOCUS_LOOP_E2E_FAIL_FIRST_LIVE"] = "1"
            env["FOCUS_LOOP_E2E_SERVER"] = f"http://127.0.0.1:{port}"
            env["FOCUS_LOOP_E2E_HISTORY"] = json.dumps([
                {"role": "human", "id": "history-human", "content": "开始首轮测试"},
                {"role": "ai", "id": "history-ai", "content": "历史 revision 已提交"},
            ], ensure_ascii=False)
            log_path = tmp_path / "live-server.log"
            server_log = log_path.open("w", encoding="utf-8")
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.tests.loop_live_e2e_server:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=repo, env=env, stdout=server_log, stderr=subprocess.STDOUT,
            )
            for _ in range(100):
                try:
                    response = await asyncio.to_thread(urlopen, f"{env['FOCUS_LOOP_E2E_SERVER']}/desktop/api/agent-loops/{fixture['loop_id']}")
                    assert json.load(response)["loop_id"] == fixture["loop_id"]
                    break
                except Exception:
                    if server.poll() is not None:
                        raise AssertionError(f"Live backend exited: {log_path.read_text(encoding='utf-8')}")
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError(f"Live backend did not start: {log_path.read_text(encoding='utf-8')}")

            screenshot = tmp_path / "first-round.png"
            result_path = tmp_path / "desktop-result.json"
            env["FOCUS_LOOP_E2E_RESULT_FILE"] = str(result_path)
            desktop_log = (tmp_path / "desktop.log").open("w", encoding="utf-8")
            desktop = subprocess.Popen(
                [str(electron.resolve()), str(repo / "desktop" / "loop-first-round-ui.e2e.cjs"), str(screenshot)],
                cwd=repo, env=env, stdout=desktop_log, stderr=subprocess.STDOUT,
            )

            async def read_desktop_payload(key: str) -> dict:
                for _ in range(300):
                    if not result_path.is_file():
                        if desktop.poll() is not None:
                            raise AssertionError(f"Electron exited before {key}: {(tmp_path / 'desktop.log').read_text(encoding='utf-8')}")
                        await asyncio.sleep(0.1)
                        continue
                    try:
                        payload = json.loads(result_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        await asyncio.sleep(0.1)
                        continue
                    if "error" in payload:
                        raise AssertionError(payload["error"])
                    if key in payload:
                        return payload
                    await asyncio.sleep(0.1)
                raise AssertionError(f"Electron did not report {key}: {(tmp_path / 'desktop.log').read_text(encoding='utf-8')}")

            ready = await read_desktop_payload("ready")
            assert ready["run_id"] == run_id
            async with sessions() as session:
                loop_after_recovery = await session.get(AgentLoop, fixture["loop_id"])
                assert loop_after_recovery.status == "running"
            live_bridge["value"].publish(run_id, StreamEvent(
                id="", event="events", data={"data": {"messages": [
                    {"role": "human", "id": fixture["anchor_message_id"], "content": "开始首轮测试"},
                    {"role": "ai", "id": "assistant-e2e", "tool_calls": [{"id": "tool-e2e", "name": "pytest"}]},
                ]}},
            ))
            activity = None
            for _ in range(100):
                async with sessions() as session:
                    activity = await session.scalar(select(LoopJournalEvent).where(
                        LoopJournalEvent.loop_id == fixture["loop_id"],
                        LoopJournalEvent.kind == "context.tool.started",
                        LoopJournalEvent.entity_id == "tool-e2e",
                    ))
                if activity is not None:
                    break
                await asyncio.sleep(0.05)
            assert activity is not None and activity.sequence > ready["after_sequence"]
            feed = await LoopLiveEventFeed(sessions).read_batch(fixture["loop_id"], ready["after_sequence"])
            assert any(item["kind"] == "context.tool.started" and item["payload"]["run_id"] == run_id for item in feed["events"])
            shown = await read_desktop_payload("screenshot")
            assert shown["run_id"] == run_id and shown["sse_sequence"] >= activity.sequence
            assert screenshot.is_file() and screenshot.stat().st_size > 1000
            assert desktop.wait(timeout=10) == 0

            execution_done.set_result(None)
            settlement_event = SimpleNamespace(run_id=run_id, event_id=uuid.uuid4().hex, payload={})
            async with sessions.begin() as session:
                settled = await session.get(DesktopRun, run_id, with_for_update=True)
                settled.status = "success"
                settled.settled_at = datetime.now(UTC)
                await LoopCoordinator(sessions).handle_run_settled(settlement_event, session)
                await RunDispatchRepository().settle_by_run(session, run_id)
            async with sessions.begin() as session:
                await LoopCoordinator(sessions).handle_run_settled(settlement_event, session)
            async with sessions() as session:
                settled_events = list((await session.scalars(select(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"],
                    LoopJournalEvent.kind == "context.run.settled",
                    LoopJournalEvent.entity_id == run_id,
                ))).all())
            assert len(settled_events) == 1
        finally:
            if desktop is not None and desktop.poll() is None:
                desktop.terminate()
                desktop.wait(timeout=10)
            if server is not None and server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
            if server_log is not None:
                server_log.close()
            if desktop_log is not None:
                desktop_log.close()
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())
