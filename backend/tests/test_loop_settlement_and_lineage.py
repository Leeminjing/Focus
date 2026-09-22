r"""本文件验证 Directive 启动 Run 绑定的不可改写性、崩溃恢复的重新绑定，以及跨 Context 派生关系从权威 revision
来源到 Live 投影的完整链路。

本文件对外提供五类回归证据：终态转换不改写 directive.launched_run_id、被取代 Run 的迟到结算不改写 directive
的状态与绑定、启动前 launching Directive 的恢复绑定、ContextLineageResolver 与 LoopConsoleQueryService 的派生边
解析规则、以及派生边在 revision 落库时产出 journal 事件并经 reducer/overlay 进入 Live 投影。输入为真实
PostgreSQL 中经 _seed_loop 播种的 Loop 与最小 Directive/Decision/Action/Run 行，以及真实的
DirectiveLifecycleRepository、LoopCoordinator、ContextLineageResolver、LoopEventJournal、LoopLiveProjectionReducer、
LoopLiveProjectionOverlay 与 LoopConsoleQueryService 调用；输出为「run_started 上携带外来 Run 的 failed 转换保持
runA 绑定、且不可变 transition 仍记录 runB」「迟到结算后 directive 仍为 run_started/runA」「恢复后绑定指向被观察
Run / 可重试中断清空绑定」「第 N 代目标仍返回跨 Context 派生边、同 Context revision 链零边、重复来源只出现一次」
「跨 Context 来源落库即产出 context.lineage.derived 事件并归约进投影 lineage，同链来源不产事件」断言。
具体工作流为复用 round liveness 的播种入口建立 Loop，再直接驱动生命周期仓储、协调者与 lineage 链路；lineage 用例
另以 ContextRevisionRepository 建立多 Context revision 图后分别调用解析器、read、reducer 与 overlay。示例：
`python -m pytest backend/tests/test_loop_settlement_and_lineage.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import LoopCoordinator
from backend.app.desktop.agent_loop.console_query import LoopConsoleQueryService
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.lineage_events import ContextLineageEventRecorder
from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection
from backend.app.desktop.agent_loop.live_projection_reducer import LoopLiveProjectionReducer
from backend.app.desktop.agent_loop.live_snapshot_overlay import LoopLiveProjectionOverlay
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopAction,
    LoopContextMembership,
    LoopDecision,
    LoopDirective,
)
from backend.app.desktop.context_evolution import (
    ContextLineageResolver,
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace

from test_agent_loop_round_liveness import _seed_loop, _stop


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def _sessions_for(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_directive(
    sessions,
    fixture: dict,
    *,
    label: str,
    launched_run_id: str | None,
    status: str = "launched",
    lifecycle_state: str = "run_started",
    revision: int = 5,
) -> str:
    directive_id = uuid.uuid4().hex
    decision_id = uuid.uuid4().hex
    action_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(
            LoopDecision(
                decision_id=decision_id,
                loop_id=fixture["loop_id"],
                round_id=fixture["round_id"],
                holder_id=fixture["snapshot"]["holder_id"],
                intent={},
                rationale=f"{label} directive decision",
                status="committed",
                idempotency_key=f"decision-{label}-{uuid.uuid4().hex}",
            )
        )
        await session.flush()
        session.add(
            LoopAction(
                action_id=action_id,
                decision_id=decision_id,
                loop_id=fixture["loop_id"],
                position=0,
                action_type="continue_context",
                payload={},
                status="committed",
            )
        )
        await session.flush()
        session.add(
            LoopDirective(
                directive_id=directive_id,
                loop_id=fixture["loop_id"],
                round_id=fixture["round_id"],
                decision_id=decision_id,
                action_id=action_id,
                target_context_id=fixture["context_id"],
                target_context_revision_id=fixture["revision_id"],
                message_id=f"message-{directive_id}",
                content="Run the focused checks.",
                content_hash="c" * 64,
                actor_kind="patrol",
                actor_id=fixture["snapshot"]["holder_id"],
                grant_id=fixture["snapshot"]["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                status=status,
                lifecycle_state=lifecycle_state,
                revision=revision,
                origin_kind="patrol",
                correlation_id=f"correlation-{directive_id}",
                launched_run_id=launched_run_id,
                idempotency_key=f"directive-{label}-{directive_id}",
            )
        )
    return directive_id


def _settlement_event(run_id: str) -> SimpleNamespace:
    return SimpleNamespace(run_id=run_id, payload={}, event_id=uuid.uuid4().hex)


def _context_revision(
    context_id: str,
    generation: int,
    origin: ContextRevisionOriginKind,
    sources: tuple[ContextRevisionSourceContract, ...] = (),
) -> ContextRevisionContract:
    revision_id = uuid.uuid4().hex
    ref = ContextRevisionRef(
        context_id=context_id,
        revision_id=revision_id,
        generation=generation,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{revision_id}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )
    message = {"id": f"message-{revision_id}", "role": "human", "content": context_id}
    return ContextRevisionContract(
        ref=ref,
        sources=sources,
        authored_messages=(message,),
        execution_messages=(message,),
        initial_message_ids=(message["id"],),
        content_hash="c" * 64,
        projection_status=ContextRevisionProjectionStatus.VALID,
        origin_kind=origin,
        created_at=datetime.now(UTC),
    )


def _source(contract: ContextRevisionContract, position: int) -> ContextRevisionSourceContract:
    return ContextRevisionSourceContract(source=contract.ref, position=position)


async def _seed_revision_graph(
    sessions,
    tmp_path: Path,
    *,
    label: str,
    contexts: tuple[str, ...],
    revisions: tuple[ContextRevisionContract, ...],
    current: dict[str, ContextRevisionContract],
) -> dict[str, ContextRevision]:
    workspace_id = f"ws-{uuid.uuid4().hex[:24]}"
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name=label))
        await session.flush()
        session.add_all(
            [
                DesktopThread(
                    task_id=context_id,
                    workspace_id=workspace_id,
                    thread_id=f"thread-{context_id}",
                    title=context_id,
                )
                for context_id in contexts
            ]
        )
        await session.flush()
        repository = ContextRevisionRepository()
        await repository.insert_many(session, revisions)
        for contract in current.values():
            await repository.switch_current(session, contract.ref, None)
    async with sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(ContextRevision).where(
                        ContextRevision.revision_id.in_([item.ref.revision_id for item in revisions])
                    )
                )
            ).all()
        )
    by_revision_id = {row.revision_id: row for row in rows}
    return {context_id: by_revision_id[contract.ref.revision_id] for context_id, contract in current.items()}


def _edges_between(edges: list[dict], source_context_id: str, target_context_id: str) -> list[dict]:
    return [
        edge
        for edge in edges
        if edge.source_context_id == source_context_id and edge.target_context_id == target_context_id
    ]


def test_terminal_transition_keeps_launched_run_binding(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="bind", started_at=datetime.now(UTC))
            run_a = f"run-a-{uuid.uuid4().hex[:8]}"
            run_b = f"run-b-{uuid.uuid4().hex[:8]}"
            directive_id = await _seed_directive(sessions, fixture, label="bind", launched_run_id=run_a)
            repository = DirectiveLifecycleRepository()

            async with sessions.begin() as session:
                updated = await repository.transition(
                    session,
                    directive_id,
                    "failed",
                    run_id=run_b,
                    reason="迟到结算",
                )

            assert updated.lifecycle_state == "failed", "run_started 可以合法转入 failed"
            assert updated.launched_run_id == run_a, "终态转换即使携带外来 Run 也不得改写启动 Run 绑定"
            assert updated.revision == 6, "终态转换仍必须推进 directive revision"
            async with sessions() as session:
                stored = await session.get(LoopDirective, directive_id)
                history = await repository.history(session, directive_id)
            assert stored.launched_run_id == run_a, "持久化后的绑定仍必须是 runA"
            assert stored.terminal_reason == "迟到结算"
            failed = [item for item in history if item.to_state == "failed"]
            assert len(failed) == 1, "终态转换必须留下唯一一条不可变 transition"
            assert failed[0].from_state == "run_started"
            assert failed[0].run_id == run_b, "该次转换携带的 Run 仍必须写入不可变 transition 供审计"
            assert failed[0].reason == "迟到结算"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_late_run_settlement_does_not_rewrite_directive(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="late-settlement", started_at=datetime.now(UTC))
            run_a = f"run-a-{uuid.uuid4().hex[:8]}"
            run_b = f"run-b-{uuid.uuid4().hex[:8]}"
            directive_id = await _seed_directive(sessions, fixture, label="late-settlement", launched_run_id=run_a)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert loop.status == "running", "handle_run_settled 只在 Loop 仍 running 时推进 directive"
                session.add(
                    DesktopRun(
                        run_id=run_b,
                        task_id=fixture["context_id"],
                        agent_id=f"main:{fixture['context_id']}",
                        kind="delegated",
                        status="error",
                        origin="delegated_patrol",
                        context_revision_id=fixture["revision_id"],
                        error="被取代尝试的迟到失败",
                        directive_id=directive_id,
                        loop_id=fixture["loop_id"],
                        round_id=fixture["round_id"],
                    )
                )
                session.add(
                    DesktopRun(
                        run_id=run_a,
                        task_id=fixture["context_id"],
                        agent_id=f"main:{fixture['context_id']}",
                        kind="delegated",
                        status="error",
                        origin="delegated_patrol",
                        context_revision_id=fixture["revision_id"],
                        error="run A 自身失败",
                        directive_id=directive_id,
                        loop_id=fixture["loop_id"],
                        round_id=fixture["round_id"],
                    )
                )

            coordinator = LoopCoordinator(sessions)
            async with sessions.begin() as session:
                await coordinator.handle_run_settled(_settlement_event(run_b), session)

            async with sessions() as session:
                after_late = await session.get(LoopDirective, directive_id)
            assert after_late.lifecycle_state == "run_started", "被取代尝试的迟到结算不得改写 directive 状态"
            assert after_late.launched_run_id == run_a, "被取代尝试的迟到结算不得改写 directive 绑定"
            assert after_late.revision == 5, "被取代尝试的迟到结算不得推进 directive revision"

            async with sessions.begin() as session:
                await coordinator.handle_run_settled(_settlement_event(run_a), session)

            async with sessions() as session:
                settled = await session.get(LoopDirective, directive_id)
                history = await DirectiveLifecycleRepository().history(session, directive_id)
            assert settled.lifecycle_state == "failed", "当前绑定 Run 的失败结算必须把 directive 推进为 failed"
            assert settled.launched_run_id == run_a, "结算不得把绑定改写成其它 Run"
            assert settled.terminal_reason == "run A 自身失败"
            assert [item.to_state for item in history] == ["failed"]
            assert history[0].run_id == run_a
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


async def _seed_recovery_run(
    sessions,
    fixture: dict,
    directive_id: str,
    *,
    run_id: str,
    status: str,
    reconciliation: dict | None = None,
) -> None:
    async with sessions.begin() as session:
        session.add(
            DesktopRun(
                run_id=run_id,
                task_id=fixture["context_id"],
                agent_id=f"main:{fixture['context_id']}",
                kind="delegated",
                status=status,
                origin="delegated_patrol",
                context_revision_id=fixture["revision_id"],
                directive_id=directive_id,
                loop_id=fixture["loop_id"],
                round_id=fixture["round_id"],
                workspace_result={"reconciliation": reconciliation or {}},
                created_at=datetime.now(UTC),
            )
        )


def test_recovery_rebinds_launching_directive_to_observed_run(tmp_path: Path) -> None:
    """崩溃恢复必须以实际被观察到的 Run 重新绑定 launching Directive，并补齐交付与启动转换。"""

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="recovery-live", started_at=datetime.now(UTC))
            directive_id = await _seed_directive(
                sessions,
                fixture,
                label="recovery-live",
                launched_run_id=None,
                status="launching",
                lifecycle_state="delivering",
                revision=3,
            )
            observed = f"observed-{uuid.uuid4().hex[:8]}"
            await _seed_recovery_run(sessions, fixture, directive_id, run_id=observed, status="running")

            coordinator = LoopCoordinator(sessions)
            async with sessions.begin() as session:
                recovered = await coordinator._recover_launching_directives(session)

            assert recovered == 1, "恢复必须处理这条 launching Directive"
            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
                history = await DirectiveLifecycleRepository().history(session, directive_id)
            assert directive.launched_run_id == observed, "恢复后的绑定必须指向实际被观察到的 Run"
            assert directive.status == "launched", "被观察 Run 仍活跃时 Directive 必须回到 launched"
            assert directive.lifecycle_state == "run_started", "恢复必须补齐 delivered 与 run_started 转换"
            assert [item.to_state for item in history] == ["delivered", "run_started"], "恢复必须留下两次不可变转换"
            assert [item.run_id for item in history] == [observed, observed], "两次转换都必须携带被观察到的 Run"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_recovery_clears_binding_for_retry_safe_interrupted_run(tmp_path: Path) -> None:
    """恢复遇到可安全重试的中断 Run 时必须清空尝试绑定并退回 authorized，使重试以新尝试开始。"""

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="recovery-retry", started_at=datetime.now(UTC))
            abandoned = f"abandoned-{uuid.uuid4().hex[:8]}"
            directive_id = await _seed_directive(
                sessions,
                fixture,
                label="recovery-retry",
                launched_run_id=abandoned,
                status="launching",
                lifecycle_state="delivering",
                revision=3,
            )
            await _seed_recovery_run(
                sessions,
                fixture,
                directive_id,
                run_id=abandoned,
                status="interrupted",
                reconciliation={"retry_safe": True},
            )

            coordinator = LoopCoordinator(sessions)
            async with sessions.begin() as session:
                recovered = await coordinator._recover_launching_directives(session)

            assert recovered == 1, "恢复必须处理这条 launching Directive"
            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
                history = await DirectiveLifecycleRepository().history(session, directive_id)
            assert directive.launched_run_id is None, "可重试的旧尝试不得继续占据当前尝试身份"
            assert directive.status == "created", "可重试恢复必须让 Directive 重新排队"
            assert directive.lifecycle_state == "authorized", "可重试恢复必须退回 authorized"
            assert [item.to_state for item in history] == ["authorized"], "恢复必须留下退回 authorized 的不可变转换"
            assert history[0].reason == "retry_safe_recovery"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


async def _derive_context_from_external_source(sessions, fixture: dict, *, label: str) -> dict:
    """为 fixture 的 Context 造一条带外部来源的 revision，并让来源 Context 成为同一 Loop 成员。"""

    source = f"{label}-source-{uuid.uuid4().hex[:8]}"
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"])
        session.add(
            DesktopThread(
                task_id=source,
                workspace_id=loop.workspace_id,
                thread_id=f"thread-{source}",
                title=source,
            )
        )
        await session.flush()
        repository = ContextRevisionRepository()
        target_gen1 = await repository.current(session, fixture["context_id"])
        source_gen1 = _context_revision(source, 1, ContextRevisionOriginKind.ROOT)
        target_gen2 = _context_revision(
            fixture["context_id"],
            2,
            ContextRevisionOriginKind.RUN_SETTLED,
            (_source(source_gen1, 0), _source(target_gen1, 1)),
        )
        await repository.insert_many(session, (source_gen1, target_gen2))
        await repository.switch_current(session, target_gen2.ref, target_gen1.ref)
        await repository.switch_current(session, source_gen1.ref, None)
        session.add(
            LoopContextMembership(
                membership_id=uuid.uuid4().hex,
                loop_id=fixture["loop_id"],
                context_id=source,
                lane_id=None,
                role="contributor",
                status="active",
                required_barrier=False,
            )
        )
    return {
        "source_context_id": source,
        "source_revision_id": source_gen1.ref.revision_id,
        "target_revision_id": target_gen2.ref.revision_id,
        "target_generation": target_gen2.ref.generation,
    }


def test_derived_revision_records_lineage_event_for_loop(tmp_path: Path) -> None:
    """Loop 登记成员的派生事实时，必须在同一 Loop 的 journal 留下可归约的派生边事件。"""

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="lineage-event", started_at=datetime.now(UTC))
            derived = await _derive_context_from_external_source(sessions, fixture, label="lineage-event")
            async with sessions.begin() as session:
                recorded_count = await ContextLineageEventRecorder().record_loop_members(
                    session, loop_id=fixture["loop_id"]
                )
            assert recorded_count == 1, "一个带外部来源的成员只登记一条派生边"

            async with sessions() as session:
                events = await LoopEventJournal().read(session, fixture["loop_id"], 0, 100)

            recorded = [event for event in events if event.kind == ContextLineageEventRecorder.KIND]
            assert len(recorded) == 1, "一条跨 Context 来源只登记一条派生边事件"
            edge = recorded[0]
            assert edge.entity_type == "context_lineage"
            assert edge.entity_id == f"{derived['source_context_id']}:{fixture['context_id']}"
            assert edge.entity_revision == derived["target_generation"]
            assert edge.payload["source_context_id"] == derived["source_context_id"]
            assert edge.payload["source_revision_id"] == derived["source_revision_id"]
            assert edge.payload["target_context_id"] == fixture["context_id"]
            assert edge.payload["target_revision_id"] == derived["target_revision_id"]

            projection = LoopLiveProjection(loop_id=fixture["loop_id"])
            for item in events:
                projection = LoopLiveProjectionReducer().reduce(projection, item)
            assert edge.entity_id in projection.lineage, "派生边事件必须归约进 Live 投影的 lineage 集合"
            assert projection.lineage[edge.entity_id].state["source_context_id"] == derived["source_context_id"]
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_same_context_revision_records_no_lineage_event(tmp_path: Path) -> None:
    """同一 Context 的 revision 链不得登记派生边事件。"""

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="lineage-chain", started_at=datetime.now(UTC))
            async with sessions.begin() as session:
                repository = ContextRevisionRepository()
                target_gen1 = await repository.current(session, fixture["context_id"])
                target_gen2 = _context_revision(
                    fixture["context_id"],
                    2,
                    ContextRevisionOriginKind.RUN_SETTLED,
                    (_source(target_gen1, 0),),
                )
                await repository.insert_many(session, (target_gen2,))
                await repository.switch_current(session, target_gen2.ref, target_gen1.ref)
            async with sessions.begin() as session:
                recorded_count = await ContextLineageEventRecorder().record_loop_members(
                    session, loop_id=fixture["loop_id"]
                )
            assert recorded_count == 0, "只有一个成员的 Loop 不产生派生边"

            async with sessions() as session:
                events = await LoopEventJournal().read(session, fixture["loop_id"], 0, 100)

            assert [event for event in events if event.kind == ContextLineageEventRecorder.KIND] == []
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_loop_entry_points_record_lineage_facts() -> None:
    """Loop 纳成员（创建）与发布 portfolio 两条入口都必须登记成员派生事实。"""

    root = Path(__file__).resolve().parents[2] / "backend" / "app" / "desktop" / "agent_loop"
    for name in ("service.py", "portfolio_publication.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "record_loop_members" in source, f"{name} 必须在 Loop 拓扑变化时登记 lineage 事实"


def test_live_snapshot_overlay_projects_lineage_baseline(tmp_path: Path) -> None:
    """快照边界上的权威 overlay 必须给出完整派生边，使新连接的客户端无需先回读控制台。"""

    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="lineage-overlay", started_at=datetime.now(UTC))
            derived = await _derive_context_from_external_source(sessions, fixture, label="lineage-overlay")

            async with sessions() as session:
                snapshot = await LoopLiveProjectionOverlay().apply(
                    session,
                    LoopLiveProjection(loop_id=fixture["loop_id"]),
                    1,
                )

            key = f"{derived['source_context_id']}:{fixture['context_id']}"
            assert key in snapshot.lineage, "快照必须携带跨 Context 派生边"
            entity = snapshot.lineage[key]
            assert entity.state["source_context_id"] == derived["source_context_id"]
            assert entity.state["source_revision_id"] == derived["source_revision_id"]
            assert entity.state["target_context_id"] == fixture["context_id"]
            assert entity.revision == derived["target_generation"]
            assert all(":" in item and item.split(":")[0] != item.split(":")[1] for item in snapshot.lineage), "快照不得包含自环边"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_lineage_keeps_cross_context_edge_after_target_advanced_generation(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        suffix = uuid.uuid4().hex[:8]
        target = f"lineage-gen2-target-{suffix}"
        source = f"lineage-gen2-source-{suffix}"
        try:
            source_gen1 = _context_revision(source, 1, ContextRevisionOriginKind.ROOT)
            target_gen1 = _context_revision(target, 1, ContextRevisionOriginKind.ROOT)
            target_gen2 = _context_revision(
                target,
                2,
                ContextRevisionOriginKind.RUN_SETTLED,
                (_source(source_gen1, 0), _source(target_gen1, 1)),
            )
            current = await _seed_revision_graph(
                sessions,
                tmp_path,
                label=f"lineage-gen2-{suffix}",
                contexts=(target, source),
                revisions=(source_gen1, target_gen1, target_gen2),
                current={target: target_gen2, source: source_gen1},
            )

            async with sessions() as session:
                edges = await ContextLineageResolver().resolve(
                    session, {context_id: revision.revision_id for context_id, revision in current.items()}
                )

            assert len(edges) == 1, "目标推进到 run_settled 产生的 gen2 后仍必须返回唯一一条跨 Context 派生边"
            edge = edges[0]
            assert edge.source_context_id == source, "派生边必须指向真实来源 Context"
            assert edge.target_context_id == target
            assert edge.source_revision_id == source_gen1.ref.revision_id
            assert edge.target_revision_id == target_gen2.ref.revision_id, "边记录来源首次出现的位置，与目标代次无关"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_lineage_reports_source_for_target_without_any_run(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        suffix = uuid.uuid4().hex[:8]
        target = f"lineage-gen1-target-{suffix}"
        source = f"lineage-gen1-source-{suffix}"
        try:
            source_gen1 = _context_revision(source, 1, ContextRevisionOriginKind.ROOT)
            target_gen1 = _context_revision(
                target,
                1,
                ContextRevisionOriginKind.MANUAL_DERIVE,
                (_source(source_gen1, 0),),
            )
            current = await _seed_revision_graph(
                sessions,
                tmp_path,
                label=f"lineage-gen1-{suffix}",
                contexts=(target, source),
                revisions=(source_gen1, target_gen1),
                current={target: target_gen1, source: source_gen1},
            )

            async with sessions() as session:
                edges = await ContextLineageResolver().resolve(
                    session, {context_id: revision.revision_id for context_id, revision in current.items()}
                )

            assert len(edges) == 1, "目标尚无 Run、只有 gen1 时仍必须返回派生边"
            edge = edges[0]
            assert edge.source_context_id == source
            assert edge.target_context_id == target
            assert edge.source_revision_id == source_gen1.ref.revision_id
            assert edge.target_revision_id == target_gen1.ref.revision_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_lineage_ignores_same_context_revision_chain(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        suffix = uuid.uuid4().hex[:8]
        target = f"lineage-self-{suffix}"
        try:
            target_gen1 = _context_revision(target, 1, ContextRevisionOriginKind.ROOT)
            target_gen2 = _context_revision(
                target,
                2,
                ContextRevisionOriginKind.RUN_SETTLED,
                (_source(target_gen1, 0),),
            )
            current = await _seed_revision_graph(
                sessions,
                tmp_path,
                label=f"lineage-self-{suffix}",
                contexts=(target,),
                revisions=(target_gen1, target_gen2),
                current={target: target_gen2},
            )

            async with sessions() as session:
                edges = await ContextLineageResolver().resolve(
                    session, {context_id: revision.revision_id for context_id, revision in current.items()}
                )

            assert edges == (), "同一 Context 的 revision 链不得成为拓扑边"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_lineage_deduplicates_repeated_source_context(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        suffix = uuid.uuid4().hex[:8]
        target = f"lineage-multi-target-{suffix}"
        source_a = f"lineage-multi-a-{suffix}"
        source_b = f"lineage-multi-b-{suffix}"
        try:
            a_gen1 = _context_revision(source_a, 1, ContextRevisionOriginKind.ROOT)
            a_gen2 = _context_revision(source_a, 2, ContextRevisionOriginKind.RUN_SETTLED)
            b_gen1 = _context_revision(source_b, 1, ContextRevisionOriginKind.ROOT)
            target_gen1 = _context_revision(
                target,
                1,
                ContextRevisionOriginKind.MANUAL_DERIVE,
                (_source(a_gen1, 0), _source(a_gen2, 1), _source(b_gen1, 2)),
            )
            current = await _seed_revision_graph(
                sessions,
                tmp_path,
                label=f"lineage-multi-{suffix}",
                contexts=(target, source_a, source_b),
                revisions=(a_gen1, a_gen2, b_gen1, target_gen1),
                current={target: target_gen1, source_a: a_gen1, source_b: b_gen1},
            )

            async with sessions() as session:
                edges = await ContextLineageResolver().resolve(
                    session, {context_id: revision.revision_id for context_id, revision in current.items()}
                )

            pairs = [(edge.source_context_id, edge.target_context_id) for edge in edges]
            assert len(pairs) == 2, "两个来源 Context 必须各产生一条边，同一来源 Context 不得重复"
            assert set(pairs) == {(source_a, target), (source_b, target)}
            from_a = _edges_between(edges, source_a, target)
            assert len(from_a) == 1, "同一 (source, target) 只允许出现一次"
            assert from_a[0].source_revision_id == a_gen1.ref.revision_id, "去重保留按 position 排序后的首个来源 revision"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_console_read_projects_cross_context_lineage_edges(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = _sessions_for(engine)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="console-lineage", started_at=datetime.now(UTC))
            source = f"console-lineage-source-{uuid.uuid4().hex[:8]}"
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                workspace_id = loop.workspace_id
            async with sessions.begin() as session:
                session.add(
                    DesktopThread(
                        task_id=source,
                        workspace_id=workspace_id,
                        thread_id=f"thread-{source}",
                        title=source,
                    )
                )
                await session.flush()
                repository = ContextRevisionRepository()
                target_gen1 = await repository.current(session, fixture["context_id"])
                source_gen1 = _context_revision(source, 1, ContextRevisionOriginKind.ROOT)
                target_gen2 = _context_revision(
                    fixture["context_id"],
                    2,
                    ContextRevisionOriginKind.RUN_SETTLED,
                    (_source(source_gen1, 0), _source(target_gen1, 1)),
                )
                await repository.insert_many(session, (source_gen1, target_gen2))
                await repository.switch_current(session, target_gen2.ref, target_gen1.ref)
                await repository.switch_current(session, source_gen1.ref, None)
                session.add(
                    LoopContextMembership(
                        membership_id=uuid.uuid4().hex,
                        loop_id=fixture["loop_id"],
                        context_id=source,
                        lane_id=None,
                        role="contributor",
                        status="active",
                        required_barrier=False,
                    )
                )

            async with sessions() as session:
                console = await LoopConsoleQueryService().read(session, fixture["loop_id"])

            assert {node["context_id"] for node in console["nodes"]} == {fixture["context_id"], source}
            assert len(console["edges"]) == 1, "控制台 edges 必须投影出唯一一条跨 Context 派生边"
            edge = console["edges"][0]
            assert edge["source_context_id"] == source
            assert edge["target_context_id"] == fixture["context_id"]
            assert edge["source_revision_id"] == source_gen1.ref.revision_id
            assert edge["target_revision_id"] == target_gen2.ref.revision_id
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())
