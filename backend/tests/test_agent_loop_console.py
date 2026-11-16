r"""验证共享 Loop 查询的拓扑、完整会话、事实投影与工作区输入观察。

输入为真实 PostgreSQL Loop、不可变 Context revision、模拟 checkpoint 和 Portfolio 用户意见；输出为
轻量节点图、可分页 Human/Tool 会话、精确测试事实、Mission 恒等及 observation 中外置 user_intent 的断言。
具体工作流为播种 Workspace Patrol、读取三个 query service、提交工作区反馈并冻结观察。
示例：`pytest test_agent_loop_console.py`。
公共事实排除 Tool，但完整会话仍保留原始工具消息。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.contexts.evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.patrol.agent_loop import AgentLoopService
from backend.app.desktop.patrol.agent_loop.facts.fact_projection import (
    LoopFactProjectionService,
)
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import (
    PatrolInputRequest,
)
from backend.app.desktop.patrol.agent_loop.models import (
    LoopInterventionTransition,
    LoopUserIntent,
)
from backend.app.desktop.patrol.agent_loop.queries.console_query import (
    LoopConsoleQueryService,
)
from backend.app.desktop.patrol.agent_loop.queries.conversation_query import (
    ContextConversationQueryService,
)
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import (
    LoopObservationService,
)
from backend.app.desktop.patrol.agent_loop.workspaces.workspace_patrol import (
    WorkspacePatrolService,
)
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)
from backend.tests.workspace_loop_support import seed_workspace_patrol_loop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Checkpointer:
    def __init__(self, checkpoint_id: str, messages=None) -> None:
        self._checkpoint_id = checkpoint_id
        self._messages = list(messages or [
            HumanMessage(content="Run the focused tests.", id="m-human"),
            ToolMessage(content="12 passed, 2 failed, 1 skipped", tool_call_id="call-1", name="pytest", id="m-tool"),
        ])

    async def aget_tuple(self, _config):
        return SimpleNamespace(
            config={"configurable": {"checkpoint_id": self._checkpoint_id}},
            checkpoint={
                "channel_values": {
                    "messages": self._messages
                }
            },
            metadata={},
        )


def test_console_queries_and_portfolio_intent_reach_next_observation(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-console-{suffix}"
        context_id = f"context-console-{suffix}"
        foreign_context_id = f"context-foreign-{suffix}"
        revision_id = uuid.uuid4().hex
        checkpoint_id = f"checkpoint-{suffix}"
        loop_id = uuid.uuid4().hex
        messages = [HumanMessage(content="Run the focused tests.", id="m-human")]
        messages.extend(AIMessage(content=f"analysis result {index}", id=f"m-ai-{index}") for index in range(145))
        messages.append(
            HumanMessage(
                content="compressed debugging history",
                id="m-compressed",
                additional_kwargs={
                    "compression": {
                        "block_id": "block-1",
                        "source": [{"id": "m-original", "role": "human", "content": "original debugging history"}],
                    }
                },
            )
        )
        messages.append(ToolMessage(content="12 passed, 2 failed, 1 skipped", tool_call_id="call-1", name="pytest", id="m-tool"))
        checkpointer = _Checkpointer(checkpoint_id, messages)
        try:
            async with sessions.begin() as session:
                workspace_path = tmp_path / workspace_id
                workspace_path.mkdir()
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="console"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="Testing lane"))
                session.add(DesktopThread(task_id=foreign_context_id, workspace_id=workspace_id, thread_id=f"foreign-thread-{suffix}", title="Outside loop"))
                await session.flush()
                ref = ContextRevisionRef(
                    context_id=context_id,
                    revision_id=revision_id,
                    generation=1,
                    execution_thread_id=f"thread-{suffix}",
                    checkpoint_ns="",
                    checkpoint_id=checkpoint_id,
                    payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
                )
                repository = ContextRevisionRepository()
                await repository.insert(
                    session,
                    ContextRevisionContract(
                        ref=ref,
                        content_hash="c" * 64,
                        projection_status=ContextRevisionProjectionStatus.VALID,
                        origin_kind=ContextRevisionOriginKind.RUN_SETTLED,
                        origin_id=f"initial-{suffix}",
                        created_at=datetime.now(UTC),
                    ),
                )
                await repository.switch_current(session, ref, None)
                session.add(
                    DesktopRun(
                        run_id=f"initial-{suffix}",
                        task_id=context_id,
                        agent_id=f"main:{context_id}",
                        kind="main",
                        status="success",
                        origin="direct_user",
                        execution_thread_id=f"thread-{suffix}",
                        context_revision_id=revision_id,
                        final_checkpoint_id=checkpoint_id,
                        workspace_result={
                            "workspace_status": "settled",
                            "effect_evidence": [
                                {"kind": "workspace_fingerprint_transition", "changed": True, "changed_files": ["src/service.py"]}
                            ],
                            "artifacts": ["reports/pytest.xml"],
                        },
                        settled_at=datetime.now(UTC),
                    )
                )
            service = AgentLoopService(sessions)
            started = await seed_workspace_patrol_loop(sessions, loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=f"initial-{suffix}", holder_id=f"patrol-{suffix}", goal="Verify the implementation", task_contract="Use evidence", acceptance_criteria=({"criterion_id": "tests", "text": "tests pass"},), capabilities=("continue_context", "request_completion"), context_scope=(context_id,), permission_scope=("read",))
            async with sessions() as session:
                console = await LoopConsoleQueryService().read(session, loop_id)
                conversation = await ContextConversationQueryService(checkpointer).read(
                    session,
                    loop_id,
                    context_id,
                    revision_id=None,
                    before=None,
                    limit=1,
                )
                full_conversation = await ContextConversationQueryService(checkpointer).read(
                    session,
                    loop_id,
                    context_id,
                    revision_id=None,
                    before=None,
                    limit=200,
                )
                facts = await LoopFactProjectionService(checkpointer).read(
                    session,
                    loop_id,
                    context_id=context_id,
                    kind="test",
                    status=None,
                    before=None,
                    limit=20,
                )
                all_facts = await LoopFactProjectionService(checkpointer).read(
                    session,
                    loop_id,
                    context_id=context_id,
                    kind=None,
                    status=None,
                    before=None,
                    limit=20,
                )
                with pytest.raises(HTTPException) as foreign_error:
                    await ContextConversationQueryService(checkpointer).read(
                        session,
                        loop_id,
                        foreign_context_id,
                        revision_id=None,
                        before=None,
                        limit=20,
                    )
            assert console["nodes"][0]["topic"] == "Primary execution"
            assert conversation["total"] == 148
            assert conversation["has_more"] is True
            assert conversation["content_hash"] == "c" * 64
            assert conversation["sources"] == []
            assert conversation["messages"][0]["message"]["id"] == "m-tool"
            assert [item["index"] for item in full_conversation["messages"]] == list(range(148))
            compressed = next(item for item in full_conversation["messages"] if item["message"]["id"] == "m-compressed")
            assert compressed["message"]["compression"]["source"][0]["id"] == "m-original"
            assert {item["message"]["role"] for item in full_conversation["messages"]} >= {"human", "ai", "tool"}
            assert facts["facts"][0]["metrics"]["passed"] == 12
            assert facts["facts"][0]["status"] == "failed"
            assert {item["kind"] for item in all_facts["facts"]} >= {
                "run",
                "workspace",
                "artifact",
                "test",
            }
            assert all(item["evidence"]["run_id"] == f"initial-{suffix}" for item in all_facts["facts"])
            assert foreign_error.value.status_code == 404

            result = await WorkspacePatrolService(sessions, SimpleNamespace(checkpointer=checkpointer)).submit(
                workspace_id,
                PatrolInputRequest(
                    submission_id=f"portfolio-feedback-{suffix}",
                    access_mode="read-only",
                    content="保留测试 Lane，暂停重复实现 Lane。",
                ),
            )
            after_intent = await service.get(loop_id)
            assert after_intent["goal_revision"] == started["goal_revision"]
            assert after_intent["mission"] == started["mission"]
            observation = await LoopObservationService(sessions, checkpointer).capture(loop_id, result["round_id"])
            assert observation.mission["outcome"] == "Verify the implementation"
            assert observation.mission["boundaries"]["legacy_text"] == "Use evidence"
            assert observation.mission["completion_checks"][0]["check_id"] == "tests"
            assert observation.goal is None
            assert observation.user_intents[0]["scope"] == "portfolio"
            assert observation.user_intents[0]["content"] == "保留测试 Lane，暂停重复实现 Lane。"
            async with sessions() as session:
                intent = await session.scalar(select(LoopUserIntent).where(LoopUserIntent.intent_id == result["intent_id"]))
                lifecycle = tuple((await session.scalars(select(LoopInterventionTransition).where(LoopInterventionTransition.intent_id == intent.intent_id).order_by(LoopInterventionTransition.revision))).all())
                assert intent.status == "observed"
                assert intent.origin_kind == "user"
                assert intent.intent_kind == "workspace_input"
                assert intent.delivery_state == "observed"
                assert [item.to_state for item in lifecycle] == ["submitted", "accepted", "observed"]
                assert intent.observed_round_id == result["round_id"]

            # Other owners may retain an old input for internal recovery; that
            # does not authorize the public conversation to browse it.
            from backend.app.desktop.contexts.evolution.history import (
                build_revision_history,
            )

            async with sessions.begin() as session:
                repository = ContextRevisionRepository()
                previous = await repository.current(session, context_id)
                await repository.freeze(session, previous.ref, owner_kind="observation", owner_id="conversation-retained-" + suffix)
                current_ref = previous.ref.model_copy(update={
                    "revision_id": uuid.uuid4().hex, "generation": previous.ref.generation + 1,
                    "checkpoint_id": "new-checkpoint-" + suffix,
                })
                history = build_revision_history((), ({"role": "human", "id": "new-message", "content": "new current content"},))
                current = previous.model_copy(update={"ref": current_ref, "history_payload": history,
                    "content_hash": "d" * 64, "execution_messages": (), "authored_messages": ()})
                await repository.insert(session, current)
                await repository.switch_current(session, current_ref, previous.ref)
            async with sessions() as session:
                pinned = await repository.get_by_id(session, previous.ref.revision_id)
                assert pinned.ref == previous.ref
                query = ContextConversationQueryService(checkpointer)
                for before in (None, conversation["next_before"]):
                    with pytest.raises(HTTPException) as stale:
                        await query.read(session, loop_id, context_id, revision_id=previous.ref.revision_id, before=before, limit=20)
                    assert stale.value.status_code == 409 and "刷新" in stale.value.detail
                refreshed = await query.read(session, loop_id, context_id, revision_id=None, before=None, limit=20)
                assert refreshed["revision"]["revision_id"] == current_ref.revision_id
                assert refreshed["messages"][0]["message"]["content"] == "new current content"
                page = await query.read(session, loop_id, context_id, revision_id=current_ref.revision_id, before=None, limit=20)
                assert page["messages"] == refreshed["messages"]
        finally:
            await engine.dispose()

    asyncio.run(run())
