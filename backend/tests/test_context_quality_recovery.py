"""本文件对外提供质量评估的实际PostgreSQL恢复回归。
输入为生产编译阶段、隔离运行库和受控fail/unknown；输出为相同blocker与零重复评估断言。
具体工作流为持久失败评估，再由新compiler读取同冻结产物；示例：pytest backend/tests/test_context_quality_recovery.py -q。
受控判定只验证恢复合同，不替代原生语义验收。
"""
import asyncio
from datetime import UTC, datetime
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from backend.app.desktop.agent_loop.context_expansion.compiler import ContextExpansionPlanCompiler
from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionOpportunity
from backend.app.desktop.agent_loop.context_expansion.quality import ContextQualityVerifier
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.tests.test_synthesis_claim_feedback import _inputs
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop


@pytest.mark.parametrize('dimension,verdict', [('minimality', 'fail'), ('sufficiency', 'unknown')])
def test_failed_quality_assessment_restores_same_blocker_without_new_evaluation(tmp_path, runtime_postgres_database, dimension, verdict):
    async def run():
        engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seeded = await _seed_loop(sessions, tmp_path, label='qrest', started_at=datetime.now(UTC))
        try:
            spec, bundle = _inputs()
            synthesized = await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)
            dossier = synthesized.dossier
            assert dossier is not None
            calls = []

            class Quality:
                VERSION = 'quality-restore-test-v1'

                async def verify(self, work, evidence, candidate):
                    calls.append(1)
                    return ContextQualityVerifier().verify(work, evidence, candidate, {'dimensions': [
                        {'dimension': name, 'verdict': verdict if name == dimension else 'pass', 'reasons': ['Controlled cached assessment']}
                        for name in ('minimality', 'sufficiency', 'coherence')]})

            quality = Quality()
            opportunity = ExpansionOpportunity.create(loop_id=seeded['loop_id'], round_id=seeded['round_id'],
                observation_hash='a' * 64, work_spec=spec, manifest_sources=bundle.source_frontier)
            first = await ContextExpansionPlanCompiler(sessions, None, quality_service=quality)._verify_quality_stage(
                opportunity, bundle, dossier, [], artifact_expansion_id=None, persist_artifacts=True)
            assert first.code == 'context_quality_failed' and calls == [1]
            restored = await ContextExpansionPlanCompiler(sessions, None, quality_service=quality)._verify_quality_stage(
                opportunity, bundle, dossier, [], artifact_expansion_id=None, persist_artifacts=True)
            assert restored.code == first.code and restored.summary == first.summary and calls == [1]
        finally:
            await _stop(seeded['service'], seeded['loop_id'])
            await engine.dispose()
    asyncio.run(run())
