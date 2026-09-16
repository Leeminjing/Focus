r"""本文件验证 Portfolio Patrol 默认只接收有界 Context Frontier Manifest 并按需展开精确 revision。

输入为十个含长历史的已订阅 Context、上一 Portfolio frontier 和两个新 revision；输出为八个
unchanged、两个 changed、无完整 messages 的默认清单，以及受句柄和预算保护的显式展开结果。
具体工作流为构造真实 PostgreSQL 聚合，经 builder 比较版本并验证超限时整体失败而非静默截断。
示例：`pytest backend/tests/test_context_frontier_manifest.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_curation import (
    ContextExpansionDenied,
    ContextFrontierBudgetExceeded,
    ContextFrontierBuilder,
    ContextFrontierFreshness,
    CurationProgram,
    CurationProgramRepository,
    CurationSourceSubscription,
    PortfolioRepository,
    PortfolioRevision,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Checkpointer:
    async def aget_tuple(self, config: dict) -> None:
        return None


def _ref(context_id: str, generation: int) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=uuid.uuid4().hex,
        generation=generation,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="context-frontier-definition",
        checkpoint_id=None,
        payload_mode=ContextRevisionPayloadMode.DEFINITION,
    )


def _contract(ref: ContextRevisionRef, marker: str) -> ContextRevisionContract:
    messages = tuple(
        {
            "id": f"{ref.revision_id}-{index}",
            "role": "human" if index % 2 == 0 else "assistant",
            "content": (
                f"{marker}:{'x' * 900}"
                if index < 19
                else f"Result for {ref.context_id} generation {ref.generation}"
            ),
        }
        for index in range(20)
    )
    return ContextRevisionContract(
        ref=ref,
        authored_messages=messages,
        execution_messages=messages,
        definition_hash=uuid.uuid5(uuid.NAMESPACE_DNS, ref.revision_id).hex * 2,
        projection_hash=uuid.uuid5(uuid.NAMESPACE_URL, ref.revision_id).hex * 2,
        content_hash=uuid.uuid5(uuid.NAMESPACE_OID, ref.revision_id).hex * 2,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=ContextRevisionOriginKind.CURATION,
        origin_id="frontier-test",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_manifest_is_bounded_and_expands_only_selected_revisions() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        revisions = ContextRevisionRepository()
        programs = CurationProgramRepository()
        portfolios = PortfolioRepository()
        builder = ContextFrontierBuilder(
            revisions,
            ContextRevisionReader(revisions, _Checkpointer()),
        )
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-frontier-{suffix}"
        program_id = uuid.uuid4().hex
        context_ids = [f"ctx-{index}-{suffix}" for index in range(10)]
        first_refs = [_ref(context_id, 1) for context_id in context_ids]
        second_refs = [_ref(context_ids[index], 2) for index in range(2)]
        marker = f"LARGE_HISTORY_{suffix}"
        portfolio_id: str | None = None
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="frontier manifest",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        DesktopThread(
                            task_id=context_id,
                            workspace_id=workspace_id,
                            thread_id=f"thread-{context_id}",
                            title=f"Context {index}",
                        )
                        for index, context_id in enumerate(context_ids)
                    ]
                )
            async with sessions.begin() as session:
                await programs.create(session, workspace_id, program_id=program_id)
                for index, (context_id, ref) in enumerate(zip(context_ids, first_refs)):
                    await programs.subscribe(
                        session,
                        program_id,
                        context_id,
                        f"purpose-{index}",
                        index,
                    )
                    await revisions.insert(session, _contract(ref, marker))
                    await revisions.switch_current(session, ref, None)
                portfolio = await portfolios.create_revision(
                    session,
                    program_id,
                    1,
                    [
                        {"context_id": ref.context_id, "revision_id": ref.revision_id}
                        for ref in first_refs
                    ],
                    "f" * 64,
                    0,
                )
                portfolio_id = portfolio.portfolio_revision_id
                await portfolios.switch_current(
                    session,
                    program_id,
                    portfolio.portfolio_revision_id,
                    expected_current_id=None,
                    expected_program_revision=0,
                )
                for ref, previous in zip(second_refs, first_refs[:2]):
                    await revisions.insert(session, _contract(ref, marker))
                    await revisions.switch_current(session, ref, previous)
                session.add(
                    DesktopRun(
                        run_id=uuid.uuid4().hex,
                        task_id=context_ids[0],
                        agent_id=f"main:{context_ids[0]}",
                        kind="main",
                        status="success",
                        input_messages=[],
                    )
                )

            async with sessions() as session:
                manifest = await builder.build(session, program_id)
                assert len(manifest.entries) == 10
                assert sum(
                    entry.freshness is ContextFrontierFreshness.CHANGED
                    for entry in manifest.entries
                ) == 2
                assert sum(
                    entry.freshness is ContextFrontierFreshness.UNCHANGED
                    for entry in manifest.entries
                ) == 8
                assert manifest.entries[0].recent_result is not None
                payload = manifest.model_dump(mode="json")
                encoded = json.dumps(payload, ensure_ascii=False)
                assert marker not in encoded
                assert '"messages"' not in encoded
                assert len(encoded) < 32_000

                selected = manifest.entries[0]
                assert selected.expansion is not None
                expanded = await builder.expand(
                    session,
                    selected.expansion,
                    "display",
                    max_messages=25,
                    max_chars=30_000,
                )
                assert len(expanded.messages) == 20
                assert marker in expanded.messages[0]["content"]
                with pytest.raises(ContextFrontierBudgetExceeded):
                    await builder.expand(
                        session,
                        selected.expansion,
                        "display",
                        max_messages=5,
                    )
                with pytest.raises(ContextExpansionDenied):
                    await builder.expand(
                        session,
                        selected.expansion.model_copy(update={"handle_id": "0" * 64}),
                        "display",
                    )
                with pytest.raises(ContextFrontierBudgetExceeded):
                    await builder.build(session, program_id, max_entries=9)
                with pytest.raises(ContextFrontierBudgetExceeded):
                    await builder.build(session, program_id, max_chars=100)
        finally:
            async with sessions.begin() as session:
                program = await session.get(CurationProgram, program_id)
                if program is not None:
                    program.current_portfolio_revision_id = None
                await session.flush()
                if portfolio_id is not None:
                    await session.execute(
                        delete(PortfolioRevision).where(
                            PortfolioRevision.portfolio_revision_id == portfolio_id
                        )
                    )
                await session.execute(
                    delete(CurationSourceSubscription).where(
                        CurationSourceSubscription.program_id == program_id
                    )
                )
                await session.execute(
                    delete(CurationProgram).where(CurationProgram.program_id == program_id)
                )
                await session.execute(
                    delete(ContextRevision).where(
                        ContextRevision.context_id.in_(context_ids)
                    )
                )
                await session.execute(
                    delete(DesktopThread).where(DesktopThread.task_id.in_(context_ids))
                )
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id == workspace_id
                    )
                )
            await engine.dispose()

    asyncio.run(run())
