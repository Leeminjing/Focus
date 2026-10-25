"""Exercise session mode changes through real Run recovery and checkpoint replay."""

import asyncio
import os
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.persistence.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.runs.assembly_service import RunAssemblyService
from backend.app.desktop.runs.recovery_service import RunRecoveryService
from backend.app.desktop.runs.security_service import RunSecurityService
from backend.app.desktop.runs.policy import _RUN_DISPATCH_EQUIPMENT_KEY
from focus.security.context import security_context_of
from focus.security.middleware import AccessPolicyMiddleware
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import powershell, write_file


@tool
def opaque_action() -> str:
    """Perform an opaque local operation."""
    return "executed"


class _Session:
    """In-memory persistence port; recovery, identities and checkpoint reads stay real."""

    def __init__(self, workspace):
        self.workspace = DesktopWorkspace(workspace_id="ws", path=str(workspace), display_name="ws")
        self.task = DesktopThread(task_id="task", thread_id="thread", workspace_id="ws", title="test",
                                  ui_state={"access_mode": "read-only"})
        self.runs = [DesktopRun(run_id="original", task_id="task", agent_id="main:task", kind="main",
                                origin="direct_user", status="interrupted", input_messages=[],
                                execution_thread_id="thread", checkpoint_ns="",
                                equipment={"access_mode": "read-only", "permissions": ["read", "write"]})]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def scalar(self, query):
        return self.task if query.column_descriptions[0]["entity"] is DesktopThread else self.runs[-1]

    async def get(self, model, key):
        if model is DesktopThread:
            return self.task
        if model is DesktopWorkspace:
            return self.workspace
        return next((run for run in self.runs if run.run_id == key), None)

    def add(self, run):
        if run not in self.runs:
            self.runs.append(run)

    async def commit(self):
        pass


def _ports(tmp_path):
    session = _Session(tmp_path)

    async def none(*_):
        return None

    ports = SimpleNamespace(session_factory=lambda: session, checkpointer=InMemorySaver(),
                            app_config=SimpleNamespace(), _commitment_recovery_payload=none,
                            _build_agent_factory=lambda *args, **kwargs: lambda: None,
                            _model_supports_image_input=lambda _: False,
                            _run_payload=lambda run: {"run_id": run.run_id})
    security = RunSecurityService(ports)
    ports._governed_context = security._governed_context
    ports._attach_session_mode_resolver = security._attach_session_mode_resolver
    return ports, session


class _State(TypedDict, total=False):
    result: str


def _graph(ports, tool, args, seen, call_id="call-1"):
    async def call(_state, runtime: Runtime):
        request = ToolCallRequest(
            tool_call={"name": tool.name, "id": call_id, "args": args}, tool=tool, state={},
            runtime=ToolRuntime(state={}, context=runtime.context, config={}, stream_writer=None,
                                tool_call_id=call_id, store=None, tools=[]),
        )

        async def execute(active):
            seen.append(security_context_of(active.runtime.context))
            return ToolMessage(content="executed", tool_call_id=call_id)

        result = await AccessPolicyMiddleware().awrap_tool_call(request, execute)
        return {"result": result.content}

    graph = StateGraph(_State, context_schema=dict)
    graph.add_node("call", call)
    graph.add_edge(START, "call")
    graph.add_edge("call", END)
    return graph.compile(checkpointer=ports.checkpointer)


async def _initial(ports, session):
    run = session.runs[0]
    return await RunAssemblyService(ports)._prepare(
        run, "thread", "ws", session.workspace.path, [], "test", dict(run.equipment), "", "main",
    )


@pytest.mark.parametrize("mode", ["workspace-write", "danger-full-access"])
def test_recovered_run_uses_saved_mode_and_keeps_live_resolver(tmp_path, mode):
    async def scenario():
        ports, session = _ports(tmp_path)
        initial = await _initial(ports, session)
        seen = []
        graph = _graph(ports, write_file, {"path": "result.txt", "content": "ok",
                                         "requested_mode": mode, "reason": "write result"}, seen)
        config = {"configurable": {"thread_id": "thread"}}
        paused = await graph.ainvoke({}, config, context=initial.body.context)
        payload = paused["__interrupt__"][0].value
        assert seen == []
        session.task.ui_state = {"access_mode": mode}
        prepared = await RunRecoveryService(ports).resume_run(
            "thread", {"decision": "approve", "request_id": payload["request_id"]},
        )
        resumed = session.runs[-1]
        assert resumed.origin == "resume"
        assert resumed.equipment["access_mode"] == mode
        assert session.runs[0].equipment["access_mode"] == "read-only"
        security = security_context_of(prepared.body.context)
        assert security.authorization.access_mode == AccessMode(mode)
        assert security.authorization.permissions == ("read", "write")
        resolver = security.extras["session_mode_resolver"]
        result = await graph.ainvoke(Command(resume=prepared.body.resume), config, context=prepared.body.context)
        assert result["result"] == "executed"
        assert len(seen) == 1
        session.task.ui_state = {"access_mode": "read-only"}
        assert await resolver() is AccessMode.READ_ONLY
        next_call = _graph(ports, write_file, {"path": "next.txt", "content": "ok"}, seen, "call-2")
        result = await next_call.ainvoke({}, {"configurable": {"thread_id": "next"}}, context=prepared.body.context)
        assert "FILE_POLICY_DENIED" in result["result"]
        assert len(seen) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["resume", "direct_user", "delegated_patrol"])
def test_main_assembly_attaches_the_round_resolver_for_loop_runs(tmp_path, origin):
    async def scenario():
        ports, session = _ports(tmp_path)
        run = session.runs[0]
        run.origin, run.loop_id, run.round_id = origin, "loop", "round"
        session.task.ui_state = {"access_mode": "danger-full-access"}
        prepared = await _initial(ports, session)
        security = security_context_of(prepared.body.context)
        assert security.authorization.access_mode is AccessMode.READ_ONLY
        assert callable(security.extras.get("round_mode_resolver"))
        assert "session_mode_resolver" not in security.extras

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["approve", "reject", "cancel", "mismatch"])
@pytest.mark.parametrize("escalation", [False, True])
def test_replay_keeps_explicit_approval_after_mode_change(tmp_path, decision, escalation):
    async def scenario():
        ports, session = _ports(tmp_path)
        initial = await _initial(ports, session)
        seen = []
        args = {"path": "result.txt", "content": "ok", "requested_mode": "workspace-write",
                "reason": "write result"} if escalation else {}
        graph = _graph(ports, write_file if escalation else opaque_action, args, seen)
        config = {"configurable": {"thread_id": "thread"}}
        paused = await graph.ainvoke({}, config, context=initial.body.context)
        payload = paused["__interrupt__"][0].value
        session.task.ui_state = {"access_mode": "danger-full-access"}
        with pytest.raises(HTTPException):
            await RunRecoveryService(ports).resume_run("thread", {})
        assert len(session.runs) == 1
        assert seen == []
        answer = {"decision": "approve" if decision == "mismatch" else decision}
        if payload.get("request_id"):
            answer["request_id"] = "wrong" if decision == "mismatch" else payload["request_id"]
        prepared = await RunRecoveryService(ports).resume_run("thread", answer)
        result = await graph.ainvoke(Command(resume=prepared.body.resume), config, context=prepared.body.context)
        approved = decision == "approve" or (decision == "mismatch" and not escalation)
        assert len(seen) == int(approved)
        assert (result["result"] == "executed") == approved

    asyncio.run(scenario())


def test_approve_once_survives_a_new_run_identity_without_changing_standing_mode(tmp_path):
    async def scenario():
        ports, session = _ports(tmp_path)
        initial = await _initial(ports, session)
        seen = []
        graph = _graph(ports, write_file, {"path": "result.txt", "content": "ok",
                                         "requested_mode": "workspace-write", "reason": "write"}, seen)
        config = {"configurable": {"thread_id": "thread"}}
        paused = await graph.ainvoke({}, config, context=initial.body.context)
        answer = {"decision": "approve", "request_id": paused["__interrupt__"][0].value["request_id"]}
        prepared = await RunRecoveryService(ports).resume_run("thread", answer)
        # A prepared recovery can itself be interrupted before consuming the original approval.
        session.runs[-1].status = "interrupted"
        prepared = await RunRecoveryService(ports).resume_run("thread", answer)
        result = await graph.ainvoke(Command(resume=answer), config, context=prepared.body.context)
        assert result["result"] == "executed"
        assert seen[0].authorization.access_mode is AccessMode.WORKSPACE_WRITE
        assert session.task.ui_state["access_mode"] == "read-only"
        assert session.runs[-1].equipment["access_mode"] == "read-only"

    asyncio.run(scenario())


@pytest.mark.parametrize("explicit_identity", [False, True])
def test_loop_recovery_does_not_read_session_mode(tmp_path, explicit_identity):
    async def scenario():
        ports, session = _ports(tmp_path)
        initial = await _initial(ports, session)
        graph = _graph(ports, opaque_action, {}, [])
        config = {"configurable": {"thread_id": "thread"}}
        await graph.ainvoke({}, config, context=initial.body.context)
        if not explicit_identity:
            session.runs[0].loop_id = "loop"
            session.runs[0].round_id = "round"
            session.runs[0].directive_id = "directive"
        session.task.ui_state = {"access_mode": "danger-full-access"}
        prepared = await RunRecoveryService(ports).resume_run(
            "thread", {"decision": "approve"},
            {"loop_id": "loop", "round_id": "round"} if explicit_identity else None,
        )
        security = security_context_of(prepared.body.context)
        assert session.runs[-1].equipment["access_mode"] == "read-only"
        assert callable(security.extras.get("round_mode_resolver"))
        assert "session_mode_resolver" not in security.extras
        assert session.runs[-1].loop_id == "loop"
        assert session.runs[-1].round_id == "round"
        if not explicit_identity:
            assert session.runs[-1].directive_id == "directive"

    asyncio.run(scenario())


def test_resumed_run_does_not_gain_shell_capability(tmp_path):
    async def scenario():
        ports, session = _ports(tmp_path)
        initial = await _initial(ports, session)
        graph = _graph(ports, opaque_action, {}, [])
        config = {"configurable": {"thread_id": "thread"}}
        await graph.ainvoke({}, config, context=initial.body.context)
        session.task.ui_state = {"access_mode": "danger-full-access"}
        prepared = await RunRecoveryService(ports).resume_run("thread", {"decision": "approve"})
        seen = []
        shell_graph = _graph(ports, powershell, {"command": "echo hello"}, seen)
        result = await shell_graph.ainvoke({}, {"configurable": {"thread_id": "shell"}}, context=prepared.body.context)
        assert "PERMISSION_DENIED" in result["result"]
        assert seen == []

    asyncio.run(scenario())


def test_durable_assembly_starts_with_current_saved_session_mode(tmp_path):
    async def scenario():
        ports, session = _ports(tmp_path)
        run = session.runs[0]
        run.status = "pending"
        run.equipment = {**run.equipment, _RUN_DISPATCH_EQUIPMENT_KEY: {"agent_role": "main", "base_prompt": "test"}}
        session.task.ui_state = {"access_mode": "danger-full-access"}
        assembled = await RunAssemblyService(ports).assemble_run(run.run_id)
        security = security_context_of(assembled.body.context)
        assert security.authorization.access_mode is AccessMode.DANGER_FULL_ACCESS
        assert callable(security.extras.get("session_mode_resolver"))

    asyncio.run(scenario())


def test_changed_arguments_cannot_reuse_original_approval(tmp_path):
    async def scenario():
        ports, session = _ports(tmp_path)
        initial = await _initial(ports, session)
        seen = []
        args = {"path": "result.txt", "content": "original", "requested_mode": "workspace-write", "reason": "write"}
        graph = _graph(ports, write_file, args, seen)
        config = {"configurable": {"thread_id": "thread"}}
        paused = await graph.ainvoke({}, config, context=initial.body.context)
        answer = {"decision": "approve", "request_id": paused["__interrupt__"][0].value["request_id"]}
        session.task.ui_state = {"access_mode": "danger-full-access"}
        prepared = await RunRecoveryService(ports).resume_run("thread", answer)
        args["content"] = "changed"
        result = await graph.ainvoke(Command(resume=answer), config, context=prepared.body.context)
        assert "INVALID_MODE_ESCALATION" in result["result"]
        assert seen == []

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["workspace-write", "danger-full-access"])
def test_persisted_run_recovery_replays_checkpoint_with_current_mode(tmp_path, runtime_postgres_database, mode):
    async def scenario():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        ports, seed = _ports(tmp_path)
        ports.session_factory = sessions
        try:
            async with sessions.begin() as session:
                session.add(seed.workspace)
                await session.flush()
                session.add(seed.task)
                await session.flush()
                session.add(seed.runs[0])
            initial = await _initial(ports, seed)
            seen = []
            graph = _graph(ports, write_file, {"path": "result.txt", "content": "ok",
                                             "requested_mode": mode, "reason": "write result"}, seen)
            config = {"configurable": {"thread_id": "thread"}}
            paused = await graph.ainvoke({}, config, context=initial.body.context)
            answer = {"decision": "approve", "request_id": paused["__interrupt__"][0].value["request_id"]}
            async with sessions.begin() as session:
                task = await session.get(DesktopThread, "task")
                task.ui_state = {"access_mode": mode}
            prepared = await RunRecoveryService(ports).resume_run("thread", answer)
            async with sessions() as session:
                resumed = await session.get(DesktopRun, prepared.payload["run_id"])
                original = await session.get(DesktopRun, "original")
                assert resumed.origin == "resume"
                assert resumed.equipment["access_mode"] == mode
                assert original.equipment["access_mode"] == "read-only"
            result = await graph.ainvoke(Command(resume=answer), config, context=prepared.body.context)
            assert result["result"] == "executed"
            assert len(seen) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
