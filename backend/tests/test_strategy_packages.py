"""验证 Strategy 文件、安装冲突、工作区覆盖、作者工具与历史采用身份。

输入为真实三段正文、独立 PostgreSQL 和受控作者模型；输出为可分享格式与按轮身份的回归断言。
示例：python -m pytest backend/tests/test_strategy_packages.py -q。
"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.evolution_strategies import (
    EvolutionStrategy, freeze_strategies, strategy_adoption_records, validate_strategy_adoptions,
)
from backend.app.desktop.strategy_packages import StrategyMetadata, StrategyPackage, parse_strategy
from backend.app.desktop.memory import MemoryCreate, MemoryService, MemoryUpdate, MemorySelection, StrategyScenarios
from backend.app.desktop.memory_routes import memory_router
from backend.app.desktop.models import DesktopWorkspace
from backend.app.desktop.agent_loop.schemas import StrategyAdoption


def package(version="1.0.0"):
    return StrategyPackage(metadata=StrategyMetadata(id="tests/validate", name="先验证关键假设", version=version),
                           strategy=EvolutionStrategy(applicability="存在未验证的关键假设", evolution="先验证，等待真实结果，再推进。", inheritance="目标与证据；结论仍须注明适用范围。"))


def test_markdown_roundtrip_and_code_fence_headings():
    original = package()
    fenced = original.model_copy(update={"strategy": original.strategy.model_copy(update={"evolution": "示例：\n```markdown\n## applicability\n```\n等待真实结果。"})})
    assert parse_strategy(fenced.markdown()) == fenced
    assert parse_strategy("\ufeff" + original.markdown().replace("\n", "\r\n")) == original
    assert parse_strategy(original.markdown()).fingerprint() == original.fingerprint()


@pytest.mark.parametrize("change", [
    lambda text: text.replace("schema: focus.strategy/v1\n", ""),
    lambda text: text.replace("focus.strategy/v1", "focus.strategy/v2"),
    lambda text: text.replace("id: tests/validate", "id: tests/validate\nid: tests/other"),
    lambda text: text.replace("id: tests/validate", "id: ../outside"),
    lambda text: text.replace("version: 1.0.0", "version: 1.0.0\nworkspace_id: local"),
    lambda text: text.replace("name: 先验证关键假设", "name: !!python/object/apply:os.system ['echo unsafe']"),
    lambda text: text.replace("name: 先验证关键假设", "name: &name title\ndescription: *name"),
    lambda text: text.replace("## evolution", "## applicability"),
    lambda text: text.replace("## inheritance", "## unknown"),
    lambda text: text + "\n## applicability\n重复",
    lambda text: text + "字" * 60000,
])
def test_invalid_or_unsafe_packages_are_rejected(change):
    with pytest.raises(ValueError):
        parse_strategy(change(package().markdown()))


def test_install_scope_conflict_export_and_snapshot(isolated_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = MemoryService(sessions, None, None)
        workspace_id = uuid.uuid4().hex
        installed_ids = []
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="策略测试"))
            original = package()
            assert (await service.preview_strategy(original.markdown(), workspace_id))["existing"] is None
            first = await service.install_strategy(original.markdown(), None)
            global_id = first["memory"]["memory_id"]
            installed_ids.append(global_id)
            assert first["status"] == "installed" and not first["memory"]["locally_modified"]
            exported = await service.export_strategy(global_id)
            assert parse_strategy(exported["markdown"]) == original
            assert "workspace_id" not in exported["markdown"] and global_id not in exported["markdown"]
            local = await service.install_strategy(original.markdown(), workspace_id, enabled=False)
            local_id = local["memory"]["memory_id"]
            installed_ids.append(local_id)
            async with sessions() as session:
                masked = await freeze_strategies(session, workspace_id)
                other = await freeze_strategies(session, None)
            assert not any(item.get("package", {}).get("id") == original.metadata.id for item in masked)
            assert global_id in {item["memory_id"] for item in other}
            await service.update(local_id, MemoryUpdate(enabled=True))
            async with sessions() as session:
                before = await freeze_strategies(session, workspace_id)
            adopted = next(item for item in before if item["memory_id"] == local_id)
            assert global_id not in {item["memory_id"] for item in before}
            await service.update(local_id, MemoryUpdate(strategy={**original.strategy.model_dump(), "evolution": "继续已有工作，不扩张。"}))
            edited = await service.get(local_id)
            assert edited["locally_modified"]
            upgrade = package("2.0.0")
            preview = await service.preview_strategy(upgrade.markdown(), workspace_id)
            with pytest.raises(HTTPException, match="409"):
                await service.install_strategy(upgrade.markdown(), workspace_id)
            await service.update(local_id, MemoryUpdate(enabled=False))
            with pytest.raises(HTTPException, match="409"):
                await service.install_strategy(upgrade.markdown(), workspace_id, replace_memory_id=local_id, expected_hash=preview["existing"]["installation_hash"])
            preview = await service.preview_strategy(upgrade.markdown(), workspace_id)
            updated = await service.install_strategy(upgrade.markdown(), workspace_id, enabled=False,
                replace_memory_id=local_id, expected_hash=preview["existing"]["installation_hash"])
            assert updated["status"] == "updated" and not updated["memory"]["locally_modified"]
            duplicate = await service.install_strategy(upgrade.markdown(), workspace_id, enabled=True)
            assert duplicate["status"] == "already_installed" and not duplicate["memory"]["enabled"]
            assert adopted["strategy"] == original.strategy.model_dump()
            assert adopted["package"]["version"] == "1.0.0"
            with pytest.raises(HTTPException, match="409"):
                await service.create(MemoryCreate(title=original.metadata.name, category="evolution_strategy", strategy=original.strategy, strategy_package=original.metadata))
            with pytest.raises(HTTPException, match="422"):
                await service.preview_strategy(original.markdown(), "missing")
        finally:
            for identity in installed_ids:
                await service.delete(identity)
            await engine.dispose()
    asyncio.run(run())


def test_adoption_references_bind_frozen_content_and_actions():
    snapshot = {"memory_id": "strategy", "content_hash": "a" * 64, "title": "冻结名称", "package": {"id": "tests/validate", "version": "1.0.0"}}
    adoption = StrategyAdoption(memory_id="strategy", content_hash="a" * 64, reason="先完成验证", action_indexes=(0,))
    validate_strategy_adoptions((adoption,), (snapshot,), 1)
    for changes in ({"content_hash": "b" * 64}, {"action_indexes": (1,)}, {"action_indexes": (-1,)}, {"action_indexes": (0, 0)}):
        with pytest.raises(ValueError):
            validate_strategy_adoptions((adoption.model_copy(update=changes),), (snapshot,), 1)
    with pytest.raises(ValueError):
        validate_strategy_adoptions((adoption, adoption), (snapshot,), 1)
    assert strategy_adoption_records({"strategy_adoptions": [adoption.model_dump()]}, {"evolution_strategies": [snapshot]})[0]["title"] == "冻结名称"
    assert strategy_adoption_records({}, {}) == []
    assert strategy_adoption_records(None, {"evolution_strategies": [{"memory_id": "legacy"}]}) == []
    validate_strategy_adoptions((), ({"memory_id": "legacy"},), 1)


@pytest.mark.parametrize("bad_reference", [False, True])
def test_adoption_survives_kernel_events_live_and_audit(isolated_postgres_database, tmp_path, bad_reference):
    from datetime import UTC, datetime
    from sqlalchemy import select
    from test_agent_loop_round_liveness import _seed_loop, _stop
    from loop_run_boundary_support import freeze_patrol_intent
    from backend.app.desktop.agent_loop.kernel import LoopKernel
    from backend.app.desktop.agent_loop.models import AgentLoop, LoopDecision, LoopObservation, LoopRound
    from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
    from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection
    from backend.app.desktop.agent_loop.live_snapshot_overlay import LoopLiveProjectionOverlay
    from backend.app.desktop.agent_loop.query_routes import loop_audit
    from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seed = await _seed_loop(sessions, tmp_path, label="strategy", started_at=datetime.now(UTC))
        memory_service = MemoryService(sessions, None, None)
        memory_id = None
        try:
            async with sessions() as session:
                loop = await session.get(AgentLoop, seed["loop_id"])
                round_row = await session.get(LoopRound, seed["round_id"])
            installed = await memory_service.install_strategy(package().markdown(), loop.workspace_id)
            memory_id = installed["memory"]["memory_id"]
            snapshot = seed["snapshot"]
            intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
                loop_id=seed["loop_id"], loop_revision=snapshot["revision"], round_id=seed["round_id"],
                holder_id="patrol-strategy", grant_id=snapshot["grant"]["grant_id"], grant_revision=1, goal_revision=1,
                observed_frontier_hash=round_row.frontier_hash, observed_workspace_revision=round_row.workspace_revision,
                rationale="先在已有工作中完成最小验证", actions=({"action": "continue_context", "context_id": seed["context_id"],
                    "context_revision_id": seed["revision_id"], "message": "验证关键假设并发布真实证据。"},))
            frozen = await freeze_patrol_intent(sessions, intent)
            from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
            from backend.app.desktop.agent_loop.observation import observation_hash
            async with sessions.begin() as session:
                observation = await session.get(LoopObservation, frozen.observation_id)
                envelope = LoopObservationEnvelope.model_validate(observation.envelope).model_copy(update={
                    "evolution_strategies": await freeze_strategies(session, loop.workspace_id)})
                observation.envelope = envelope.model_dump(mode="json")
                observation.envelope_hash = observation_hash(envelope)
                frozen = frozen.model_copy(update={"observed_envelope_hash": observation.envelope_hash})
            strategy_snapshot = next(item for item in observation.envelope["evolution_strategies"] if item["memory_id"] == memory_id)
            adoption = StrategyAdoption(memory_id=memory_id, content_hash="f" * 64 if bad_reference else strategy_snapshot["content_hash"],
                                        reason="已有 Context 可以承担验证，无需新增", action_indexes=(0,))
            result = await LoopKernel(sessions).commit(frozen.model_copy(update={"strategy_adoptions": (adoption,)}))
            if bad_reference:
                assert result.status == "rejected" and "冻结" in result.reason
                return
            assert result.status == "committed", result.reason
            await memory_service.update(memory_id, MemoryUpdate(title="后来修改的名称"))
            async with sessions() as session:
                decision = await session.get(LoopDecision, result.decision_id)
                assert decision.intent["strategy_adoptions"][0]["content_hash"] == strategy_snapshot["content_hash"]
                event = await session.scalar(select(LoopJournalEvent).where(LoopJournalEvent.entity_id == seed["round_id"],
                    LoopJournalEvent.entity_type == "round").order_by(LoopJournalEvent.entity_revision.desc()))
                assert event.payload["strategy_adoptions"][0]["title"] == package().metadata.name
                projection = await LoopLiveProjectionOverlay().apply(session, LoopLiveProjection(loop_id=seed["loop_id"]), 1)
                assert projection.rounds[seed["round_id"]].state["strategy_adoptions"][0]["title"] == package().metadata.name
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(desktop_service=SimpleNamespace(session_factory=sessions))))
            audit = await loop_audit(seed["loop_id"], request)
            assert audit["decisions"][-1]["strategy_adoptions"][0]["package"]["version"] == "1.0.0"
        finally:
            await _stop(seed["service"], seed["loop_id"])
            if memory_id:
                await memory_service.delete(memory_id)
            await engine.dispose()
    asyncio.run(run())


def test_authoring_produces_drafts_and_three_simulations_without_saving(monkeypatch):
    from backend.app.desktop import memory as module
    calls = []
    result = {"adopted": True, "reason": "关键假设未验证", "organization": "先最小验证", "inheritance": "目标和相关证据", "missing_evidence": ["实验结果"], "stop_when": "证据充分"}
    rehearsal = {name: {**result, "adopted": name != "inapplicable"} for name in ("applicable", "inapplicable", "feedback")}

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            import json
            response = {"title": "先验证", "strategy": package().strategy.model_dump()} if len(calls) == 1 else rehearsal
            return SimpleNamespace(content=json.dumps(response, ensure_ascii=False))

    config = SimpleNamespace(name="controlled", curation_max_output_tokens=3000, curation_output_method="prompt_json")
    monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: Model())
    service = MemoryService(None, SimpleNamespace(get_model=lambda name: config, resolve_default_model_name=lambda: "controlled"), None)

    async def run():
        draft = await service.draft_strategy(MemorySelection(sources=[{"type": "manual", "text": "先验证关键假设，避免返工。"}]))
        assert draft["strategy"] == package().strategy.model_dump()
        actual = await service.rehearse_strategy(draft["title"], package().strategy,
            StrategyScenarios(applicable="未知关键假设", inapplicable="仅改文案", feedback="用户要求完全离线"))
        assert actual == rehearsal and len(calls) == 2
        with pytest.raises(ValueError):
            await service.draft_strategy(MemorySelection())
    asyncio.run(run())


def test_strategy_routes_reject_bad_files_before_install():
    class Service:
        async def preview_strategy(self, markdown, workspace_id):
            parsed = parse_strategy(markdown)
            return {"metadata": parsed.metadata.model_dump(by_alias=True), "strategy": parsed.strategy.model_dump(), "existing": None}

    app = FastAPI()
    app.state.desktop_service = SimpleNamespace(memory=Service())
    app.include_router(memory_router)
    with TestClient(app) as client:
        response = client.post("/desktop/api/strategies/preview", json={"markdown": package().markdown()})
        assert response.status_code == 200 and response.json()["metadata"]["id"] == "tests/validate"
        assert client.post("/desktop/api/strategies/preview", json={"markdown": "bad"}).status_code == 422
        assert client.post("/desktop/api/strategies/preview", json={"markdown": package().markdown(), "unexpected": True}).status_code == 422


def test_official_examples_are_valid_and_have_unique_identities():
    examples = Path(__file__).resolve().parents[2] / "docs/strategies/examples"
    packages = [parse_strategy(path.read_text(encoding="utf-8")) for path in examples.glob("*/STRATEGY.md")]
    assert len(packages) == 7
    assert len({item.metadata.id for item in packages}) == len(packages)


def test_package_migration_preserves_legacy_edits_and_reentry(isolated_postgres_database):
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from backend.app.desktop.models import Memory

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity = uuid.uuid4().hex
        service = MemoryService(sessions, None, None)

        def migrate(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration = import_module("focus.persistence.migrations.versions.d03a4b5c6e7f_strategy_packages")
                migration.upgrade()
                migration.downgrade()
                migration.upgrade()

        try:
            async with sessions.begin() as session:
                session.add(Memory(memory_id=identity, category="evolution_strategy", title="已有自定义方法", content="", enabled=False,
                                   strategy=package().strategy.model_dump(), strategy_package=None, segments=[], source_snapshot=[]))
            async with engine.begin() as connection:
                await connection.run_sync(migrate)
            saved = await service.get(identity)
            assert saved["strategy_package"]["id"] == f"local/{identity}"
            assert not saved["enabled"] and not saved["locally_modified"]
            await service.update(identity, MemoryUpdate(title="后来的修改"))
            async with engine.begin() as connection:
                await connection.run_sync(migrate)
            saved = await service.get(identity)
            assert saved["title"] == "后来的修改" and saved["locally_modified"]
        finally:
            await service.delete(identity)
            await engine.dispose()
    asyncio.run(run())
