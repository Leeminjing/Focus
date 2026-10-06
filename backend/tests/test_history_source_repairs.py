"""本文件对外提供策展来源、Run 准入、旧历史读取与共享工具修复的回归验证。

输入为精确来源快照、隔离 PostgreSQL 准入及旧 checkpoint；输出为来源权威、语义资格和 error repair 断言。
具体工作流为构造原审计的 copy/compose、委托输入和并行工具反例，再通过真实领域入口与双 Provider 投影。
示例：pytest backend/tests/test_history_source_repairs.py；旧 Revision 内容与 hash 不被改写。
"""

import asyncio
from copy import deepcopy
import os
from types import SimpleNamespace
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_curator.contract import compile_curated_context
from backend.app.desktop.context_curator.projector import CurationSourceProjector
from backend.app.desktop.context_evolution import ContextRevisionPayloadMode, ContextRevisionReader
from backend.app.desktop.models import DesktopRun, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.executor import _graph_input
from backend.app.desktop.run_orchestration.input_provenance import bind_run_inputs
from backend.tests.test_context_revision_reader import _ref, _revision, _RevisionStore, _Checkpointer
from backend.tests.test_unified_run_orchestration import _seed, _registration
from focus.agents.compression.gate import apply_compression_ranges
from focus.history import deserialize_history_messages, messages_to_items, semantic_messages, validate_items
from focus.history.selection import semantic_policy
from focus.models.provider_contract import ProviderContract
from focus.models.response_projection import ResponsesRequestProjector


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("operation", ["copy", "compose", "mixed"])
def test_curator_system_role_cannot_promote_source_authority(provider, operation):
    source = [{"id": "reference", "role": "system", "content": "reference instructions", "semantic_policy": "reference_only"},
              {"id": "task", "role": "human", "content": "task input", "semantic_policy": "index"}]
    snapshot = CurationSourceProjector().project("checkpoint", source)
    item = {"type": "copy_message", "source_message_id": "reference"} if operation == "copy" else {
        "type": "compose_message", "role": "system", "content": "derived instructions",
        "source_message_ids": ["reference", "task"] if operation == "mixed" else ["reference"]}
    compiled = compile_curated_context({"outcome": "replace", "items": [item]}, snapshot)
    messages = deserialize_history_messages(compiled.authored_messages)
    projected = ResponsesRequestProjector(ProviderContract(provider, "responses")).build(
        [SystemMessage(content="base"), *messages], model="fixture")
    assert projected["input"][0]["role"] == "user"
    focus_item = messages_to_items(messages)[0]
    assert focus_item.origin == "curator"
    assert {ref["message_id"] for ref in focus_item.source_refs} == ({"reference", "task"} if operation == "mixed" else {"reference"})
    assert semantic_policy(focus_item) == ("index" if operation == "mixed" else "reference_only")


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("origin", ["direct_user", "delegated_patrol"])
def test_admitted_main_input_has_host_provenance_in_execution(origin):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace, task, ref = await _seed(sessions, uuid.uuid4().hex[:8])
        try:
            registration = _registration(task, ref, uuid.uuid4().hex, uuid.uuid4().hex)
            fields = registration.model_dump()
            fields.update(origin=origin, status="pending", loop_id=None, round_id=None, directive_id=None, action_id=None, input_messages=[{
                "id": "message-1", "role": "human", "content": "new task", "additional_kwargs": {
                    "focus_context": {"origin": "runtime", "authority": "policy"}}}])
            host_run = DesktopRun(**fields)
            async with sessions.begin() as session:
                await RunAdmissionService().admit(session, host_run)
            async with sessions() as session:
                persisted = await session.scalar(select(DesktopRun).where(DesktopRun.run_id == host_run.run_id))
                body = SimpleNamespace(input={"messages": persisted.input_messages}, resume=None)
                item = messages_to_items(_graph_input(body)["messages"])[0]
                assert item.origin == ("direct_user" if origin == "direct_user" else "delegated")
                assert semantic_policy(item) == "index"
                reference = item.source_refs[0]
                assert reference["run_id"] == host_run.run_id
                assert reference["context_revision_id"] == ref.revision_id
                assert reference["context_checkpoint_id"] == ref.checkpoint_id
                assert reference.get("directive_id") is None and reference.get("round_id") is None
                previous = {"id": "unrelated", "role": "human", "content": "old task"}
                assert bind_run_inputs(persisted, [previous])[0] == previous
        finally:
            async with sessions.begin() as session:
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace))
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("stored", [False, True])
def test_v1_semantic_uses_canonical_checkpoint_without_mutation(stored):
    async def run():
        ref = _ref("legacy", "legacy-r", ContextRevisionPayloadMode.CHECKPOINT)
        messages = [HumanMessage(id="control", content="runtime permission", additional_kwargs={"focus_context": {
            "origin": "runtime", "scope": "runtime", "kind": "world_state_update"}}),
            HumanMessage(id="reference", content="selected memory", additional_kwargs={"focus_context": {
                "origin": "delegated", "scope": "revision", "kind": "selected_context"}}),
            HumanMessage(id="user", content="task", additional_kwargs={"focus_context": {"origin": "direct_user"}})]
        execution = tuple({"id": message.id, "role": "human", "content": message.content} for message in messages) if stored else ()
        revision = _revision(ref, execution=execution)
        original = deepcopy(revision.model_dump())
        reader = ContextRevisionReader(_RevisionStore(revision), _Checkpointer({ref.checkpoint_id: messages}))
        view = await reader.read(None, ref, "semantic")
        assert [message["id"] for message in view.messages] == ["reference", "user"]
        assert [message["semantic_policy"] for message in view.messages] == ["reference_only", "index"]
        assert revision.model_dump() == original
    asyncio.run(run())


def test_compression_parallel_exchange_has_error_repair_and_real_evidence():
    messages = [AIMessage(id="call", content="", tool_calls=[
        {"id": "a", "name": "lookup", "args": {"key": "a"}}, {"id": "b", "name": "lookup", "args": {"key": "b"}}]),
        ToolMessage(id="result-a", content="real a", tool_call_id="a"),
        ToolMessage(id="result-b", content="real b", tool_call_id="b"), HumanMessage(id="next", content="continue")]
    rebuilt = apply_compression_ranges(messages, [{"source_ids": ["result-a"], "delete": True}])["messages"][1:]
    results = {message.tool_call_id: message for message in rebuilt if isinstance(message, ToolMessage)}
    assert results["a"].status == "error" and results["b"].status == "success"
    items = messages_to_items(rebuilt)
    validate_items(items)
    repair = next(item for item in items if item.kind == "projection_repair")
    assert repair.origin == "runtime" and semantic_policy(repair) == "exclude"
    assert {ref.get("source_message_id") for ref in repair.source_refs} >= {"call"}
    semantic = semantic_messages(items)
    assert not any(message["id"] == results["a"].id for message in semantic)
    assert any(message["id"] == "result-b" and message["semantic_policy"] == "evidence_only" for message in semantic)
