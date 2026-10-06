"""本文件对外提供持久 synthesis 引用恢复与 ready dossier 缓存回归。
输入为六种类型化证据引用及真实 PostgreSQL 阶段产物；输出为 JSON 往返身份不变、来源校验仍严格且恢复零新模型调用的断言。
具体工作流为验证原始 JSON 经 Pydantic 类型化后排序，再从新连接恢复已核验 Dossier；示例：pytest backend/tests/test_synthesis_citation_restore.py -q。
仅替换远端响应，不改生产引用身份、缓存资格或未知来源的拒绝规则。
"""

import asyncio
from datetime import UTC, datetime
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.compiler import ContextExpansionPlanCompiler
from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity
from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisClaim
from backend.app.desktop.context_curation import MaterialEvidenceRef, MissionEvidenceRef, RunResultEvidenceRef, WorkspaceEffectEvidenceRef
from backend.tests._semantic_context_fixtures import single_source_fixture
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_synthesis_claim_feedback import _service


@pytest.mark.parametrize("ref", [
    single_source_fixture().evidence_frontier[0],
    MissionEvidenceRef(loop_id="loop", goal_revision=1, item_id="check", content_hash="a" * 64),
    MissionEvidenceRef(identity_version="mission-section-v2", loop_id="loop", goal_revision=1,
        section_kind="outcome", item_id="outcome", content_hash="a" * 64),
    RunResultEvidenceRef(run_id="run", context_id="context", result_id="result", content_hash="a" * 64),
    MaterialEvidenceRef(material_id="material", version_id="version", content_hash="a" * 64),
    WorkspaceEffectEvidenceRef(workspace_id="workspace", revision=1, effect_id="effect", content_hash="a" * 64),
])
def test_claim_citation_json_restores_typed_identity(ref):
    claim = ContextSynthesisClaim.create(statement="Frozen source statement", authority="confirmed", citations=(ref,))
    assert ContextSynthesisClaim.model_validate(claim.model_dump(mode="json")) == claim
    assert ContextSynthesisClaim.model_validate_json(claim.model_dump_json()) == claim


def test_compiler_restores_verified_dossier_without_new_model_call(tmp_path, monkeypatch, runtime_postgres_database):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label="synref", started_at=datetime.now(UTC))
        try:
            service, spec, bundle, _, _, calls = _service(monkeypatch)
            opportunity = ExpansionOpportunity.create(loop_id=seeded["loop_id"], round_id=seeded["round_id"],
                observation_hash="a" * 64, work_spec=spec, manifest_sources=bundle.source_frontier)
            first = await ContextExpansionPlanCompiler(sessions, None, synthesizer=service)._synthesize_dossier_stage(
                None, opportunity, bundle, [], artifact_expansion_id=None, persist_artifacts=True)
            assert first.claims and len(calls) == 2
            recovered = await ContextExpansionPlanCompiler(sessions, None, synthesizer=service)._synthesize_dossier_stage(
                None, opportunity, bundle, [], artifact_expansion_id=None, persist_artifacts=True)
            assert recovered == first and len(calls) == 2
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()
    asyncio.run(run())
