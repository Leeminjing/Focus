"""策略集合容量准入：真实 CRUD、覆盖、回滚和并发安装，不调用外部模型。"""

import asyncio
import os
import uuid
import json
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.evolution_strategies import EvolutionStrategy, freeze_strategies
from backend.app.desktop.memory import MemoryCreate, MemoryService, MemoryUpdate
from backend.app.desktop.models import DesktopWorkspace, DesktopThread, Memory
from backend.app.desktop.strategy_packages import StrategyMetadata, StrategyPackage
from backend.tests.config_helpers import app_config_for
from backend.app.desktop.strategy_capacity import strategy_capacity_report, strategy_input_bound


def package(name, size=2500):
    return StrategyPackage(metadata=StrategyMetadata(id=f"capacity/{name}", name=name, version="1.0.0"),
        strategy=EvolutionStrategy(**{key: "中文策略" * size for key in ("applicability", "evolution", "inheritance")}))


def config():
    app = app_config_for("capacity-test", None)
    app.models[0].context_window = 120000
    app.models[0].curation_max_output_tokens = 4096
    app.models[0].curation_output_method = "prompt_json"
    return app


@asynccontextmanager
async def services(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = MemoryService(sessions, config(), None)
    scopes = [uuid.uuid4().hex for _ in range(2)]
    owned = []
    try:
        async with sessions.begin() as session:
            for index, scope in enumerate(scopes):
                session.add(DesktopWorkspace(workspace_id=scope, path=str(tmp_path / str(index)), display_name=scope))
        yield service, sessions, scopes, owned
    finally:
        async with sessions.begin() as session:
            rows = (await session.scalars(select(Memory).where(
                Memory.workspace_id.in_(scopes) | Memory.memory_id.in_(owned)))).all()
            for row in rows:
                await session.delete(row)
            for scope in scopes:
                await session.delete(await session.get(DesktopWorkspace, scope))
        await engine.dispose()


@pytest.mark.parametrize("body", ['中文' * 1000, '"\\\n\t' * 1000])
def test_size_matches_patrol_request_serialization(body):
    from langchain_core.messages import HumanMessage
    from backend.app.desktop.agent_loop.patrol_request_capacity import PatrolRequestCapacity

    capacity = PatrolRequestCapacity(config().models[0], {})
    snapshots = [{"memory_id": "s", "title": "中文", "content_hash": "a" * 64, "strategy": {"evolution": body}}]
    def messages(items):
        return [HumanMessage(content=json.dumps({"evolution_strategies": items}, ensure_ascii=False, separators=(",", ":")))]
    assert strategy_input_bound(snapshots) == capacity.estimate(messages(snapshots), EvolutionStrategy) - capacity.estimate(messages([]), EvolutionStrategy)


def test_exact_limit_output_reserve_and_unlimited_policy():
    snapshot = ({"memory_id": "s", "title": "策略", "content_hash": "a" * 64, "strategy": {"evolution": "中文" * 2000}},)
    model = config().models[0]
    report = strategy_capacity_report(snapshot, model, {})
    # Leave exactly the fixed request, dynamic reserve and strategy footprint.
    model.context_window = report["fixed_input_bound"] + report["observation_input_reserve"] + report["input_bound"] + model.curation_max_output_tokens
    assert strategy_capacity_report(snapshot, model, {})["fits"]
    model.context_window -= 1
    assert not strategy_capacity_report(snapshot, model, {})["fits"]
    model.context_window = None
    assert strategy_capacity_report(snapshot, model, {})["strategy_input_limit"] is None
    limits = {"expansion_resources": {"max_request_input_tokens": 100000, "output_token_reserve": 9000}}
    limited = strategy_capacity_report(snapshot, model, limits)
    assert limited["request_input_limit"] == 100000 and limited["output_reserve"] == 9000
    assert limited["strategy_input_limit"] is not None


def test_global_change_checks_all_effective_scopes_and_unmasking(isolated_postgres_database, tmp_path):
    async def run():
        async with services(tmp_path) as (service, sessions, scopes, owned):
            a, b = scopes
            global_row = (await service.install_strategy(package("shared", 1).markdown(), None))["memory"]
            owned.append(global_row["memory_id"])
            mask = (await service.install_strategy(package("shared", 1).markdown(), a, enabled=False))["memory"]
            await service.install_strategy(package("local-a", 1600).markdown(), a)
            heavy_b = (await service.install_strategy(package("local-b", 1600).markdown(), b))["memory"]
            with pytest.raises(HTTPException) as failure:
                await service.update(global_row["memory_id"], MemoryUpdate(strategy=package("shared", 1600).strategy))
            assert b in failure.value.detail and "local-b" in failure.value.detail
            assert (await service.get(global_row["memory_id"]))["strategy"] == package("shared", 1).strategy.model_dump()
            await service.update(heavy_b["memory_id"], MemoryUpdate(enabled=False))
            await service.update(global_row["memory_id"], MemoryUpdate(strategy=package("shared", 1600).strategy))
            # Removing or moving the disabled mask would expose the large global
            # strategy alongside local-a in the original workspace.
            with pytest.raises(HTTPException):
                await service.delete(mask["memory_id"])
            with pytest.raises(HTTPException):
                await service.update(mask["memory_id"], MemoryUpdate(workspace_id=b))
            assert (await service.get(mask["memory_id"]))["workspace_id"] == a
            async with sessions() as session:
                assert global_row["memory_id"] not in {row["memory_id"] for row in await freeze_strategies(session, a)}
    asyncio.run(run())


def test_replacement_preview_disabled_state_and_rejected_write(isolated_postgres_database, tmp_path):
    async def run():
        async with services(tmp_path) as (service, sessions, scopes, owned):
            scope = scopes[0]
            original = (await service.install_strategy(package("replace", 1).markdown(), scope))["memory"]
            large = package("replace").markdown()
            preview = await service.preview_strategy(large, scope)
            assert not preview["capacity"][0]["fits"]
            with pytest.raises(HTTPException):
                await service.install_strategy(large, scope, replace_memory_id=original["memory_id"], expected_hash=original["installation_hash"])
            assert (await service.get(original["memory_id"]))["installation_hash"] == original["installation_hash"]
            disabled_preview = await service.preview_strategy(large, scope, enabled=False)
            assert all(report["fits"] for report in disabled_preview["capacity"])
            saved = await service.install_strategy(large, scope, enabled=False,
                replace_memory_id=original["memory_id"], expected_hash=original["installation_hash"])
            assert not saved["memory"]["enabled"]
    asyncio.run(run())


def test_concurrent_independent_installs_cannot_overfill_the_set(isolated_postgres_database, tmp_path):
    async def run():
        async with services(tmp_path) as (service, sessions, scopes, owned):
            outcomes = await asyncio.gather(*(service.install_strategy(package(name, 1600).markdown(), scopes[0])
                for name in ("concurrent-a", "concurrent-b")), return_exceptions=True)
            assert sum(isinstance(result, HTTPException) and result.status_code == 422 for result in outcomes) == 1
            assert sum(isinstance(result, dict) and result["status"] == "installed" for result in outcomes) == 1
            async with sessions() as session:
                assert len([row for row in await freeze_strategies(session, scopes[0]) if row["title"].startswith("concurrent-")]) == 1
    asyncio.run(run())


@pytest.mark.parametrize("equipment,limits", [
    ({"patrol_model_name": "small"}, {}),
    ({"model_name": "small"}, {}),
    ({}, {"expansion_resources": {"max_request_input_tokens": 100000}}),
    ({}, {"expansion_resources": {"output_token_reserve": 50000}}),
])
def test_existing_patrol_model_and_grant_limit_admission(isolated_postgres_database, tmp_path, equipment, limits):
    from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant

    async def run():
        async with services(tmp_path) as (service, sessions, scopes, owned):
            small = service._app_config.models[0].model_copy(update={"name": "small", "context_window": 105000, "default": False})
            service._app_config.models.append(small)
            loop_id, context_id = uuid.uuid4().hex, uuid.uuid4().hex
            try:
                async with sessions.begin() as session:
                    session.add(DesktopThread(task_id=context_id, thread_id=context_id, workspace_id=scopes[0], title="capacity"))
                    await session.flush()
                    session.add(AgentLoop(loop_id=loop_id, workspace_id=scopes[0], initial_context_id=context_id,
                        holder_id="patrol", status="paused", equipment=equipment))
                    await session.flush()
                    session.add(LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=1,
                        holder_id="patrol", capabilities=[], context_scope=[], permission_scope=[], budgets=limits))
                with pytest.raises(HTTPException) as failure:
                    await service.install_strategy(package("limited", 1600).markdown(), scopes[0])
                assert scopes[0] in failure.value.detail
                assert ("small" if equipment else "capacity-test") in failure.value.detail
                # An unrelated workspace uses the default model and is unaffected.
                await service.install_strategy(package("limited", 1600).markdown(), scopes[1])
            finally:
                async with sessions.begin() as session:
                    await session.delete(await session.get(AgentLoop, loop_id))
                    await session.delete(await session.get(DesktopThread, context_id))
    asyncio.run(run())


def test_oversized_existing_set_can_be_reduced_after_model_change(isolated_postgres_database, tmp_path):
    async def run():
        async with services(tmp_path) as (service, sessions, scopes, owned):
            service._app_config.models[0].context_window = 300000
            first = (await service.install_strategy(package("reduce-a", 2500).markdown(), scopes[0]))["memory"]
            second = (await service.install_strategy(package("reduce-b", 2500).markdown(), scopes[0]))["memory"]
            service._app_config.models[0].context_window = 120000
            # Both the original set and the remainder are too large, but recovery
            # must allow disabling or shrinking one strategy at a time.
            await service.update(first["memory_id"], MemoryUpdate(enabled=False))
            await service.update(second["memory_id"], MemoryUpdate(strategy=package("reduce-b", 1600).strategy))
            assert (await service.get(first["memory_id"]))["enabled"] is False
    asyncio.run(run())


def test_enabled_install_rejects_combined_chinese_strategies_and_rolls_back(isolated_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = MemoryService(sessions, config(), None)
        workspace = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace, path=str(tmp_path), display_name="capacity"))
            # Legal Chinese sections fit individually; the combined request needs
            # room for the actual Patrol contract/schema and dynamic Observation.
            first = await service.install_strategy(package("first", 1600).markdown(), workspace)
            preview = await service.preview_strategy(package("second", 1600).markdown(), workspace)
            assert preview["capacity"][0]["fits"] is False
            with pytest.raises(HTTPException) as failure:
                await service.install_strategy(package("second", 1600).markdown(), workspace)
            assert failure.value.status_code == 422
            assert "first" in failure.value.detail and "second" in failure.value.detail
            assert "UTF-8" in failure.value.detail
            async with sessions() as session:
                frozen = await freeze_strategies(session, workspace)
            assert first["memory"]["memory_id"] in {item["memory_id"] for item in frozen}
            assert not any(item["title"] == "second" for item in frozen)
            disabled = await service.install_strategy(package("second", 1600).markdown(), workspace, enabled=False)
            identity = disabled["memory"]["memory_id"]
            with pytest.raises(HTTPException):
                await service.update(identity, MemoryUpdate(enabled=True))
            assert (await service.get(identity))["enabled"] is False
        finally:
            async with sessions.begin() as session:
                for row in (await session.scalars(select(Memory).where(Memory.workspace_id == workspace))).all():
                    await session.delete(row)
                await session.delete(await session.get(DesktopWorkspace, workspace))
            await engine.dispose()
    asyncio.run(run())


def test_disabled_storage_and_enabled_edit_share_admission(isolated_postgres_database, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = MemoryService(sessions, config(), None)
        workspace = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace, path=str(tmp_path), display_name="edit"))
            draft = package("oversized")
            stored = await service.create(MemoryCreate(title="oversized", category="evolution_strategy",
                workspace_id=workspace, enabled=False, strategy=draft.strategy))
            with pytest.raises(HTTPException):
                await service.update(stored["memory_id"], MemoryUpdate(enabled=True))
            small = package("small", 1)
            enabled = await service.install_strategy(small.markdown(), workspace)
            with pytest.raises(HTTPException):
                await service.update(enabled["memory"]["memory_id"], MemoryUpdate(strategy=draft.strategy))
            assert (await service.get(enabled["memory"]["memory_id"]))["strategy"] == small.strategy.model_dump()
        finally:
            async with sessions.begin() as session:
                for row in (await session.scalars(select(Memory).where(Memory.workspace_id == workspace))).all():
                    await session.delete(row)
                await session.delete(await session.get(DesktopWorkspace, workspace))
            await engine.dispose()
    asyncio.run(run())
