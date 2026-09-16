r"""本文件验证稳定 Lane 按 purpose、source frontier 与语义 fingerprint 复用现有 Context revision。

输入为已发布 Testing Lane、语义相同/不同的候选和新 purpose；输出为 keep/no_change、同 Lane update
及 create 决策。具体工作流为去除系统消息身份计算语义 fingerprint，再比较持久化 Lane frontier，
并确认 no_change 不创建 Context revision。示例：`pytest backend/tests/test_lane_reuse.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.context_curation import (
    CurationLane,
    CurationProgram,
    CurationProgramRepository,
    LaneReuseAction,
    LaneReuseResolver,
    MultiSourceEvidence,
    compile_lane,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def _ref(context_id: str, generation: int = 1) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=uuid.uuid4().hex,
        generation=generation,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{context_id}-{generation}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _compiled(source: ContextRevisionRef, content: str):
    source_payload = source.model_dump(mode="json")
    evidence = MultiSourceEvidence.model_validate(
        {
            "sources": [
                {
                    "source": source_payload,
                    "projection_hash": "a" * 64,
                    "content_hash": "b" * 64,
                    "messages": [
                        {
                            "ref": {"source": source_payload, "message_id": "goal"},
                            "role": "human",
                            "content": content,
                        }
                    ],
                }
            ]
        }
    )
    return compile_lane(
        {
            "action": "create",
            "lane_id": None,
            "purpose": "Testing",
            "source_frontier": [source_payload],
            "items": [
                {
                    "type": "copy_message",
                    "source": {"source": source_payload, "message_id": "goal"},
                }
            ],
        },
        evidence,
    )


def test_lane_reuse_returns_keep_without_creating_a_revision() -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        revisions = ContextRevisionRepository()
        programs = CurationProgramRepository()
        resolver = LaneReuseResolver(revisions)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-reuse-{suffix}"
        managed_context_id = f"ctx-managed-{suffix}"
        program_id = uuid.uuid4().hex
        lane_id = uuid.uuid4().hex
        managed = _ref(managed_context_id)
        source_v1 = _ref(f"ctx-source-{suffix}", 1)
        source_v2 = _ref(f"ctx-source-{suffix}", 2)
        candidate_v1 = _compiled(source_v1, "Run database tests.")
        candidate_v2 = _compiled(source_v2, "Run database tests.")
        changed_candidate = _compiled(source_v1, "Run all integration tests.")
        assert candidate_v1.semantic_fingerprint == candidate_v2.semantic_fingerprint
        try:
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=workspace_id,
                        path=f"/tmp/{workspace_id}",
                        display_name="lane reuse",
                    )
                )
                await session.flush()
                session.add(
                    DesktopThread(
                        task_id=managed_context_id,
                        workspace_id=workspace_id,
                        thread_id=f"thread-{managed_context_id}",
                        title="Managed Testing",
                    )
                )
                await programs.create(session, workspace_id, program_id=program_id)
                await revisions.insert(
                    session,
                    ContextRevisionContract(
                        ref=managed,
                        content_hash="c" * 64,
                        projection_status=ContextRevisionProjectionStatus.VALID,
                        origin_kind=ContextRevisionOriginKind.CURATION,
                        origin_id="reuse-test",
                        created_at=datetime(2026, 9, 14, tzinfo=UTC),
                    ),
                )
                await revisions.switch_current(session, managed, None)
                await programs.add_lane(
                    session,
                    program_id,
                    "Testing",
                    managed_context_id=managed_context_id,
                    current_source_frontier_hash=resolver.frontier_hash((source_v1,)),
                    current_semantic_fingerprint=candidate_v1.semantic_fingerprint,
                    lane_id=lane_id,
                )

            async with sessions() as session:
                keep = await resolver.resolve(
                    session,
                    program_id,
                    "  TESTING ",
                    (source_v1,),
                    candidate_v1.semantic_fingerprint,
                )
                changed_frontier = await resolver.resolve(
                    session,
                    program_id,
                    "Testing",
                    (source_v2,),
                    candidate_v2.semantic_fingerprint,
                )
                changed_semantics = await resolver.resolve(
                    session,
                    program_id,
                    "Testing",
                    (source_v1,),
                    changed_candidate.semantic_fingerprint,
                )
                new_purpose = await resolver.resolve(
                    session,
                    program_id,
                    "Architecture",
                    (source_v1,),
                    candidate_v1.semantic_fingerprint,
                )
                assert keep.action is LaneReuseAction.KEEP
                assert keep.no_change is True
                assert keep.base_context_revision == managed
                assert changed_frontier.action is LaneReuseAction.UPDATE
                assert changed_frontier.lane_id == lane_id
                assert changed_semantics.action is LaneReuseAction.UPDATE
                assert new_purpose.action is LaneReuseAction.CREATE
                assert await session.scalar(
                    select(func.count(ContextRevision.revision_id)).where(
                        ContextRevision.context_id == managed_context_id
                    )
                ) == 1
        finally:
            async with sessions.begin() as session:
                await session.execute(delete(CurationLane).where(CurationLane.lane_id == lane_id))
                await session.execute(
                    delete(CurationProgram).where(CurationProgram.program_id == program_id)
                )
                await session.execute(
                    delete(ContextRevision).where(ContextRevision.context_id == managed_context_id)
                )
                await session.execute(
                    delete(DesktopThread).where(DesktopThread.task_id == managed_context_id)
                )
                await session.execute(
                    delete(DesktopWorkspace).where(
                        DesktopWorkspace.workspace_id == workspace_id
                    )
                )
            await engine.dispose()

    asyncio.run(run())
