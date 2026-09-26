r"""本文件对外提供 semantic index 旧缓存隔离与确定性重建的 PostgreSQL 集成测试。

输入为真实 Context revision、同 content hash 的 v1/v2 legacy artifact 和 v3 support-span index；输出为旧缓存不命中、新版本稳定命中、
旧 artifact 不被改写及重放不重复创建的断言。具体工作流为播种 Loop 后插入 legacy 行，经版本化 repository 查询触发 rebuild，
持久化 v3 index 并从新 session 验证新旧状态没有混合解释。示例：`pytest backend/tests/test_semantic_index_cache_compatibility.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.models import LoopSemanticIndexArtifact
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import RevisionSemanticIndexer
from backend.app.desktop.context_evolution import ContextRevisionRepository
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_legacy_semantic_cache_is_preserved_but_rebuilt_under_v3_contract(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(
            sessions,
            tmp_path,
            label="cache-v3",
            started_at=datetime.now(UTC),
        )
        artifacts = SemanticDerivationArtifactRepository()
        try:
            async with sessions.begin() as session:
                current = await ContextRevisionRepository().current(session, seeded["context_id"])
                legacy_id = "1" * 64
                legacy_payload = {"legacy_contract": "statement-as-substring-v1"}
                session.add(
                    LoopSemanticIndexArtifact(
                        index_id=legacy_id,
                        context_id=current.ref.context_id,
                        revision_id=current.ref.revision_id,
                        source_content_hash=current.content_hash,
                        index_schema_version="revision-semantic-index-v1",
                        segmenter_version="protocol-safe-segmenter-v1",
                        projector_version="supervised-segment-projector-v1",
                        status="ready",
                        payload=legacy_payload,
                        attempt_records=[],
                    )
                )
                session.add(
                    LoopSemanticIndexArtifact(
                        index_id="2" * 64,
                        context_id=current.ref.context_id,
                        revision_id=current.ref.revision_id,
                        source_content_hash=current.content_hash,
                        index_schema_version="revision-semantic-index-v2",
                        segmenter_version="protocol-safe-segmenter-v1",
                        projector_version="supervised-segment-projector-v2",
                        status="ready",
                        payload={"legacy_contract": "no-fallback-ledger-v2"},
                        attempt_records=[],
                    )
                )

            async with sessions.begin() as session:
                current = await ContextRevisionRepository().current(session, seeded["context_id"])
                miss = await artifacts.cached_index(
                    session,
                    revision_id=current.ref.revision_id,
                    source_content_hash=current.content_hash,
                    index_schema_version=RevisionSemanticIndexer.INDEX_SCHEMA_VERSION,
                    segmenter_version=RevisionSemanticIndexer().segmenter_version,
                    projector_version=RevisionSemanticIndexer.PROJECTOR_VERSION,
                )
                assert miss is None
                rebuilt = RevisionSemanticIndexer().index(
                    source=current.ref,
                    source_content_hash=current.content_hash,
                    context_role="primary",
                    active_objective="Rebuild under the support-span contract",
                    raw_messages=(
                        {
                            "id": "source-message",
                            "role": "human",
                            "content": "Preserve this frozen source evidence.",
                        },
                    ),
                )
                await artifacts.put_index(session, rebuilt)
                await artifacts.put_index(session, rebuilt)

            async with sessions() as session:
                legacy = await session.get(LoopSemanticIndexArtifact, legacy_id)
                cached = await artifacts.cached_index(
                    session,
                    revision_id=rebuilt.source.revision_id,
                    source_content_hash=rebuilt.source_content_hash,
                    index_schema_version=rebuilt.index_schema_version,
                    segmenter_version=rebuilt.segmenter_version,
                    projector_version=rebuilt.projector_version,
                )
                count = await session.scalar(
                    select(func.count()).select_from(LoopSemanticIndexArtifact).where(
                        LoopSemanticIndexArtifact.revision_id == rebuilt.source.revision_id
                    )
                )
            assert legacy.payload == legacy_payload
            assert cached == rebuilt
            assert int(count or 0) == 3
            assert cached.index_id != legacy_id
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())
