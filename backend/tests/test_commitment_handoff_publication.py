"""本文件对外提供 typed 合同经实际 checkpoint、Run settlement、Revision 派生及 rollback 的隔离数据库测试。

输入为冻结 handoff、真实 PostgreSQL saver／repository／finalizer 和 shadow writer；输出为 authored／semantic／display 来源及历史不可变断言。
具体工作流为构造精确源 checkpoint，发布包含合同的新 Revision，幂等结算，派生冻结定义并恢复旧 pointer，核对两份历史均未改写。
示例：pytest backend/tests/test_commitment_handoff_publication.py；只使用 isolated_postgres_database。
"""

import asyncio
from datetime import UTC, datetime
from itertools import cycle
import os
import uuid

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionReader, ContextRevisionRepository
from backend.app.desktop.context_evolution.service import ContextEvolutionService
from backend.app.desktop.context_evolution.history import build_revision_history
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopWorkspace
from backend.app.desktop.run_orchestration import RunLifecycleFinalizer, RunRegistrar
from backend.tests.test_commitment_handoff import _completed
from backend.tests.test_commitment_handoff_recovery import _middleware
from backend.tests.test_commitment_resume import _ScriptedDelegator
from backend.tests.test_unified_run_orchestration import _seed, _registration
from focus.agents.lead_agent_state import LeadAgentState
from focus.agents.commitment.handoff import compile_handoff
from focus.history import content_hash, serialize_history_message
from focus.history.bridge import synchronize_items
from focus.history.middleware import TypedHistoryMiddleware
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus


@pytest.mark.usefixtures("isolated_postgres_database")
def test_contract_settlement_derivation_and_rollback_preserve_exact_sources():
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace, task, seed_ref = await _seed(sessions, uuid.uuid4().hex[:8])
        try:
            async with AsyncPostgresSaver.from_conn_string(os.environ["FOCUS_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")) as saver:
                async def factory():
                    return create_agent(GenericFakeChatModel(messages=cycle([AIMessage(content="branch answer")])), [],
                        middleware=[_middleware(_ScriptedDelegator()), TypedHistoryMiddleware()], state_schema=LeadAgentState)
                graph = await factory()
                graph.checkpointer = NamespacedCheckpointer(saver, seed_ref.checkpoint_ns)
                completed, trigger, child_ref = _completed()
                base_config = await graph.aupdate_state({"configurable": {"thread_id": seed_ref.execution_thread_id}},
                    {"messages": [trigger], "execution_items": synchronize_items(None, [trigger])})
                baseline = await graph.aget_state(base_config)
                base_ref = seed_ref.model_copy(update={"revision_id": uuid.uuid4().hex, "generation": 2,
                    "checkpoint_id": baseline.config["configurable"]["checkpoint_id"]})
                base_record = serialize_history_message(trigger)
                base_history = build_revision_history([base_record], [base_record])
                base = ContextRevisionContract(ref=base_ref, history_payload=base_history,
                    content_hash=content_hash(base_history.model_dump(mode="json")),
                    projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT,
                    created_at=datetime.now(UTC))
                repository = ContextRevisionRepository()
                async with sessions.begin() as session:
                    await repository.insert(session, base)
                    await repository.switch_current(session, base_ref, seed_ref)
                run_id = uuid.uuid4().hex
                await RunRegistrar(sessions).register(_registration(task, base_ref, run_id, "contract-settlement"))
                delivery = compile_handoff(completed, trigger, child_ref, [{"kind": "run", "run_id": run_id}])
                final_config = await graph.aupdate_state(base_config, {"messages": delivery,
                    "task_contract": completed["task_contract"],
                    "execution_items": synchronize_items(baseline.values["execution_items"], [trigger, *delivery])},
                    as_node="CommitmentMiddleware.before_agent")
                final = await graph.aget_state(final_config)
                finalizer = RunLifecycleFinalizer(sessions, saver)
                record = RunRecord(run_id=run_id, thread_id=base_ref.execution_thread_id,
                    status=RunStatus.success, on_disconnect=DisconnectMode.cancel)
                settlement = await finalizer.finalize(record)
                assert settlement.context_publication == "published"
                assert (await finalizer.finalize(record)).idempotent
                reader = ContextRevisionReader(repository, saver)
                async with sessions() as session:
                    published = await repository.get(session, settlement.context_revision)
                    authored = await reader.read(session, published.ref, "authored")
                    semantic = await reader.read(session, published.ref, "semantic")
                    display = await reader.read(session, published.ref, "display")
                    assert [item.kind for item in published.history_payload.authored_items] == ["message", "task_contract", "selected_context"]
                    assert [row["semantic_policy"] for row in semantic.messages] == ["index", "index", "reference_only"]
                    assert len(display.messages) == 1 and display.messages[0]["id"] == trigger.id
                    assert (await repository.get(session, base_ref)).model_dump() == base.model_dump()
                service = ContextEvolutionService(sessions, saver, factory)
                async with sessions.begin() as session:
                    derived = await service.stage_definition(session, task, tuple(authored.messages), (),
                        ContextRevisionOriginKind.CURATION, "contract-derived")
                assert [item.kind for item in derived.history_payload.authored_items] == ["message", "task_contract", "selected_context"]
                shadow = await saver.aget_tuple(derived.ref.checkpoint_config())
                assert all(item["scope"] != "runtime" for item in shadow.checkpoint["channel_values"]["execution_items"])
                branch = await factory()
                branch.checkpointer = NamespacedCheckpointer(saver, derived.ref.checkpoint_ns)
                branch_state = await branch.ainvoke(None, {"configurable": {"thread_id": derived.ref.execution_thread_id}})
                assert branch_state["task_contract"] == completed["task_contract"]
                async with sessions.begin() as session:
                    await repository.switch_current(session, base_ref, derived.ref)
                async with sessions() as session:
                    assert (await repository.current(session, task)).ref == base_ref
                    assert (await repository.get(session, published.ref)).model_dump() == published.model_dump()
                    assert (await repository.get(session, base_ref)).model_dump() == base.model_dump()
                assert (await graph.aget_state(base_config)).values == baseline.values
                assert (await graph.aget_state(final_config)).values == final.values
        finally:
            async with sessions.begin() as session:
                await session.execute(delete(DesktopDomainResult).where(DesktopDomainResult.context_id == task))
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace))
            await engine.dispose()
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())
