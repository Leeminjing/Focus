"""本文件验证策略记忆 CRUD、作用域与冻结输入的兼容性。

输入为隔离 PostgreSQL 和真实 MemoryService；输出为预置不覆盖、范围隔离和冻结 hash 的断言。
工作流创建普通/策略记忆，冻结后编辑并复核旧快照和新版本；不调用外部模型。
受控模型验证策略和用户反馈进入规划载荷；生产 Session 隔离由 test_planning_observation_binding 的数据库回归覆盖。
示例：python -m pytest backend/tests/test_evolution_strategies.py -q。
"""

import asyncio
import os
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.memory import MemoryService, MemoryCreate, MemoryUpdate
from backend.app.desktop.models import DesktopWorkspace
from backend.app.desktop.evolution_strategies import freeze_strategies, strategy_catalog
from backend.app.desktop.agent_loop.observation import observation_hash, LoopObservationBuilder
from backend.app.desktop.agent_loop.decision_context import scoped_memory
from test_semantic_context_planning import _observation


def test_strategy_crud_scope_and_snapshot(isolated_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = MemoryService(sessions, None, None)
        workspace_id = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="策略"))
            presets = [row for row in await service.list() if row["category"] == "evolution_strategy"]
            assert len(presets) == 6
            await service.update(presets[0]["memory_id"], MemoryUpdate(title="用户编辑的预置", enabled=False))
            from importlib import import_module
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            def reenter(connection):
                with Operations.context(MigrationContext.configure(connection)):
                    migration = import_module("focus.persistence.migrations.versions.b81e2f3a4c5d_evolution_strategies")
                    migration.downgrade()
                    migration.upgrade()
            async with engine.begin() as connection:
                await connection.run_sync(reenter)
            retained = await service.get(presets[0]["memory_id"])
            assert retained["title"] == "用户编辑的预置" and retained["enabled"] is False
            ordinary = await service.create(MemoryCreate(title="旧记忆", content="保留"))
            body = dict(applicability="否定方案", evolution="先诊断再派生", inheritance="要求和有效纠正")
            created = await service.create(MemoryCreate(title="自定义", category="evolution_strategy",
                workspace_id=workspace_id, strategy=body))
            async with sessions() as session:
                before = await freeze_strategies(session, workspace_id)
                outside = await freeze_strategies(session, uuid.uuid4().hex)
            assert created["memory_id"] in {row["memory_id"] for row in before}
            assert created["memory_id"] not in {row["memory_id"] for row in outside}
            await service.update(created["memory_id"], MemoryUpdate(strategy={**body, "evolution": "继续已有 Context"}))
            async with sessions() as session:
                after = await freeze_strategies(session, workspace_id)
            old = next(row for row in before if row["memory_id"] == created["memory_id"])
            new = next(row for row in after if row["memory_id"] == created["memory_id"])
            assert old["applicability"] == "否定方案" and "strategy" not in old
            assert old["content_hash"] != new["content_hash"]
            await service.update(created["memory_id"], MemoryUpdate(workspace_id=None, enabled=False))
            async with sessions() as session:
                assert created["memory_id"] not in {row["memory_id"] for row in await freeze_strategies(session, workspace_id)}
            assert (await service.get(ordinary["memory_id"]))["content"] == "保留"
            with pytest.raises(HTTPException):
                await service.create(MemoryCreate(title="无效", category="evolution_strategy"))
            with pytest.raises(HTTPException):
                await service.create(MemoryCreate(title="越界", category="evolution_strategy", strategy=body, workspace_id="missing"))
            with pytest.raises(HTTPException):
                await service.freeze_selection([created["memory_id"]])
            await service.delete(created["memory_id"])
            await service.delete(ordinary["memory_id"])
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_frozen_strategies_are_full_and_old_hash_is_compatible():
    base = _observation()
    strategy = {"memory_id": "s", "content_hash": "a" * 64, "strategy": {"evolution": "长" * 9000}}
    frozen = LoopObservationBuilder(max_text=4000).build(**{**base.model_dump(), "evolution_strategies": (strategy,)})
    assert frozen.evolution_strategies[0] == strategy
    assert scoped_memory(frozen, set())["evolution_strategies"] == strategy_catalog((strategy,))
    assert observation_hash(frozen) != observation_hash(base)
    assert observation_hash(base) == observation_hash(base.model_copy(update={"evolution_strategies": ()}))


@pytest.mark.parametrize("strategy,scenario,objectives", [
    ("diagnose", "诊断执行失败，尚无已发布原因", ()),
    ("diagnose", "原 A 正在诊断，用户又纠正为完全离线", ()),
    ("diagnose", "只是修改措辞，在原 Context 继续即可", ()),
    ("explore", "独立考察未确定的导出格式", ("考察格式",)),
    ("compete", "资源仅允许逐个执行，比较两个独立候选", ("本地数据库", "本地文件")),
    ("split", "B 依赖尚未发布的 A，只推进 A", ("先确定接口",)),
    ("revise", "新决定替代云端依赖，保留导出约束", ("重做离线方案",)),
    ("combine", "两个已发布成果需综合并保留分歧", ("综合有效成果",)),
])
def test_strategy_scenarios_use_frozen_planning_and_existing_candidate_contract(strategy, scenario, objectives):
    from importlib import import_module
    from backend.tests.test_complete_semantic_context_derivation import _long_index
    from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import (
        RetrievalBackedCognitiveAdvisor, PlannerQueryProposal, PlannerReadProposal, LaneAdviceProposal)
    from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
        PlanningRetrievalSession, PortfolioIndexCatalog, AuthorizedSemanticRetriever, RetrievalBudget)

    seed = next(row for row in import_module("focus.persistence.migrations.versions.b81e2f3a4c5d_evolution_strategies")._SEEDS if row[0] == strategy)
    snapshot = {"memory_id": strategy, "content_hash": "a" * 64, "strategy": dict(zip(("applicability", "evolution", "inheritance"), seed[2:]))}
    feedback = {"scope": "portfolio", "content": scenario, "file_refs": [{"path": "plan.md", "sha256": "b" * 64}]}
    seen = []

    class ControlledModel:
        async def invoke(self, schema, system, payload):
            assert payload["derivation"]["evolution_strategies"] == strategy_catalog((snapshot,))
            assert payload["derivation"]["user_intents"] == (feedback,)
            seen.append(schema)
            if schema is PlannerQueryProposal:
                return schema(rationale="读取冻结要求", queries=({"text": "durable lock requirement", "kinds": ("semantic_unit",), "limit": 8},))
            if schema is PlannerReadProposal:
                return schema(rationale="精读实际来源", candidate_ids=(payload["candidates"][0]["candidate_id"],))
            assert schema is LaneAdviceProposal
            unit_id = payload["allowed_candidate_unit_ids"][0]
            return schema.model_validate({"rationale": scenario, "work_specs": tuple({
                "objective": objective, "separation_reason": scenario, "questions": ("哪些约束有效？",),
                "completion_criteria": ("依据已发布证据回答",), "workspace_requirement": "read_only",
                "evidence_requirements": ({"requirement_id": "requirements", "role": "requirement",
                    "question": "冻结要求是什么？", "coverage_criterion": "精确原文", "candidate_unit_ids": (unit_id,)},),
            } for objective in objectives)})

    async def run():
        index = _long_index()
        catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
        planning = PlanningRetrievalSession.create(observation_hash="o" * 64, frontier_hash="f" * 64, catalog=catalog,
            planner_version=RetrievalBackedCognitiveAdvisor.VERSION, retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(max_queries=2, max_candidates=16, max_exact_reads=4, max_model_calls=3, max_tokens=1000))
        payload = {"mission": {"outcome": scenario}, "frontier_hash": "f" * 64,
            "scope": {"derivation_input": {"portfolio_index_catalog": catalog.model_dump(mode="json"),
                "evolution_strategies": (snapshot,), "user_intents": (feedback,)}}}
        advisor = RetrievalBackedCognitiveAdvisor(ControlledModel())
        result = await advisor.plan(payload, planning, (index,))
        assert result.session.state == "planned", result.blocker_summary
        assert tuple(item.objective for item in result.proposal.work_specs) == objectives
        assert len(seen) == 3
        resumed = await advisor.plan(payload, result.session, (index,))
        assert resumed.proposal == result.proposal and len(seen) == 3
        assert index.index_id == catalog.descriptors[0].index_id
    asyncio.run(run())
