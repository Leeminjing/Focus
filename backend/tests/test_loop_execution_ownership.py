r"""本文件对外提供 Run 定向认领、队列认领共享 fencing 语义、认领释放同步 Run 终态，以及 directive 启动端口
在「他人已接手」「尝试已终结」「该 Run 已被启动」三种状态下的收敛规则的回归测试。

输入为隔离 PostgreSQL 中经 RunAdmissionService 准入的 accepted RunDispatch、定向 owner `directive-launch:*` 与队列
worker 身份，以及由 _seed_loop 播种的运行中 Loop、launching Directive 与端口替身伪造的既有启动者/既有终态；
输出为"定向认领会从队列拿走该 Run""两条认领路径同样自增 fencing 并写入租约与时间戳""释放认领把 dispatch 与
DesktopRun 落到失败终态""他人持有的 Run 被幂等接受且不被中止/绑定""调度记录已终结的 Run 被拒绝启动并终结"
"已被启动的 Run 让本端口的认领随其收口为 running/settled，且不进入执行脊柱"断言。
具体工作流为在真实库里准入 Run，分别经 claim_run/claim 认领并读回 dispatch 行，再用 transition 释放认领并核对
Run 状态与错误文本；端口用例以 _StubDesktopService 准入 Run、伪造竞争或终态后调用真实 DesktopDirectiveLaunchPort，
并把 execute_prepared_run 替换为必失败桩以证明未进入执行脊柱。
示例：`pytest backend/tests/test_loop_execution_ownership.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import dispatch as agent_loop_dispatch
from backend.app.desktop.agent_loop.dispatch import DesktopDirectiveLaunchPort
from backend.app.desktop.agent_loop.models import LoopAction, LoopDecision, LoopDirective
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.dispatch import RunDispatchRepository
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.service import PreparedRun
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor
from backend.app.gateway.routers.thread_runs import RunCreateRequest

from test_agent_loop_round_liveness import _seed_loop, _stop


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

DIRECTIVE_OWNER = "directive-launch:test"


def _run(task_id: str, run_id: str, key: str, content: str) -> DesktopRun:
    return DesktopRun(
        run_id=run_id,
        task_id=task_id,
        agent_id=f"main:{task_id}",
        kind="main",
        status="pending",
        origin="delegated_patrol",
        execution_thread_id=f"thread-{task_id}",
        checkpoint_ns="",
        idempotency_key=key,
        input_messages=[{"role": "user", "content": content}],
        equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}},
        workspace_anchor={},
    )


async def _seed_task(sessions, root: Path, label: str) -> str:
    workspace_id = uuid.uuid4().hex
    task_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(root / label), display_name=label))
        await session.flush()
        session.add(
            DesktopThread(
                task_id=task_id,
                workspace_id=workspace_id,
                thread_id=f"thread-{task_id}",
                title=label,
            )
        )
    return task_id


async def _settle_active(sessions) -> None:
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        await session.execute(
            update(RunDispatch)
            .where(RunDispatch.status.in_(("accepted", "claimed", "running")))
            .values(status="settled", settled_at=now, lease_expires_at=None)
        )
        await session.execute(
            update(DesktopRun)
            .where(DesktopRun.status.in_(("pending", "running")))
            .values(status="success", settled_at=now)
        )


async def _admit(admission: RunAdmissionService, sessions, run: DesktopRun) -> None:
    async with sessions.begin() as session:
        result = await admission.admit(session, run)
    assert result.created is True, "准入必须新建 Run 与 accepted dispatch"


async def _seed_launching_directive(sessions, fixture: dict, *, label: str) -> str:
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
                rationale=f"{label} launch decision",
                status="committed",
                idempotency_key=f"decision-{directive_id}",
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
                content="Run the focused launch checks.",
                content_hash="c" * 64,
                actor_kind="patrol",
                actor_id=fixture["snapshot"]["holder_id"],
                grant_id=fixture["snapshot"]["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                status="launching",
                lifecycle_state="delivering",
                revision=3,
                origin_kind="patrol",
                correlation_id=f"correlation-{directive_id}",
                attempt=1,
                max_attempts=3,
                idempotency_key=f"directive-{directive_id}",
            )
        )
    return directive_id


async def _forbidden_spine(*args, **kwargs):
    raise AssertionError("端口在本用例中不得进入执行脊柱 execute_prepared_run")


class _StubDesktopService:
    """端口用例的最小 DesktopService 替身：为准入的 Run 建调度记录，并按需伪造既有启动者或既有终态。"""

    def __init__(
        self,
        sessions,
        *,
        preclaim_by: str | None = None,
        dispatch_state: str | None = None,
        run_status: str = "pending",
    ) -> None:
        self._sessions = sessions
        self._admission = RunAdmissionService()
        self._dispatches = RunDispatchRepository()
        self._preclaim_by = preclaim_by
        self._dispatch_state = dispatch_state
        self._run_status = run_status
        self.run_id: str | None = None
        self.bridge = SimpleNamespace()
        self.checkpointer = SimpleNamespace()
        self.run_manager = SimpleNamespace(cancel=lambda *args, **kwargs: None)
        self.store = SimpleNamespace()
        self.app_config = SimpleNamespace()

    async def start_main_run(self, **kwargs) -> PreparedRun:
        identity = kwargs["run_identity"]
        self.run_id = identity["run_id"]
        run = DesktopRun(
            run_id=self.run_id,
            task_id=kwargs["task_id"],
            agent_id=f"main:{kwargs['task_id']}",
            kind="main",
            status="pending",
            origin="delegated_patrol",
            execution_thread_id=f"thread-{kwargs['task_id']}",
            checkpoint_ns="",
            idempotency_key=identity["idempotency_key"],
            input_messages=[{"role": "user", "content": kwargs["message"]}],
            directive_id=identity["directive_id"],
            loop_id=identity["loop_id"],
            round_id=identity["round_id"],
            equipment={},
            workspace_anchor={},
        )
        async with self._sessions.begin() as session:
            admitted = await self._admission.admit(session, run)
            assert admitted.created is True, "端口替身必须为本次交付新建 Run 与 accepted dispatch"
            if self._preclaim_by is not None:
                await self._dispatches.claim_run(session, self.run_id, self._preclaim_by)
            if self._dispatch_state is not None:
                row = await self._dispatches.by_run(session, self.run_id)
                row.status = self._dispatch_state
                row.error = "process_restarted_with_uncertain_execution"
                row.settled_at = datetime.now(UTC)
                row.lease_expires_at = None
            if self._run_status != "pending":
                stored = await session.get(DesktopRun, self.run_id, with_for_update=True)
                stored.status = self._run_status
        return PreparedRun(
            body=RunCreateRequest(input={"messages": []}, context={"run_id": self.run_id}),
            thread_id=f"thread-{kwargs['task_id']}",
            agent_factory=None,
            payload={},
        )

    def attach_run_sync(self, record) -> None:
        raise AssertionError("端口在未认领成功的用例中不得进入执行脊柱")


def _message() -> SimpleNamespace:
    return SimpleNamespace(content="Run the focused launch checks.")


def test_directive_claim_takes_run_away_from_queue(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        admission = RunAdmissionService()
        repository = RunDispatchRepository()
        try:
            await _settle_active(sessions)
            task_id = await _seed_task(sessions, tmp_path, "directive-owned")
            admitted = _run(task_id, uuid.uuid4().hex, f"request-{uuid.uuid4().hex}", "定向启动端口负责的 Run")
            await _admit(admission, sessions, admitted)

            async with sessions() as session:
                before = await repository.by_run(session, admitted.run_id)
                assert before is not None, "准入必须同事务写入 RunDispatch"
                assert before.status == "accepted", "准入后 dispatch 必须是 accepted"
                assert before.fencing_token == 0, "准入时 fencing token 必须为零"
                before_token = before.fencing_token
                assert before.claimed_by is None, "准入时不得有认领者"

            async with sessions.begin() as session:
                directed = await repository.claim_run(session, admitted.run_id, DIRECTIVE_OWNER)
                assert directed is not None, "accepted dispatch 必须能被按 Run 定向认领"
                assert directed.status == "claimed", "定向认领后 dispatch 必须进入 claimed"
                assert directed.claimed_by == DIRECTIVE_OWNER, "定向认领必须记录给定的 owner"
                assert directed.fencing_token == before_token + 1, "定向认领必须自增 fencing token"

            async with sessions.begin() as session:
                queued = await repository.claim(session, "queue-worker")

            assert queued is None or queued.run_id != admitted.run_id, "已被定向认领的 Run 不得再被队列消费者领走"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_claim_run_and_queue_claim_share_fencing_semantics(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        admission = RunAdmissionService()
        repository = RunDispatchRepository()
        try:
            await _settle_active(sessions)
            directed_task = await _seed_task(sessions, tmp_path, "fencing-directed")
            queued_task = await _seed_task(sessions, tmp_path, "fencing-queued")
            directed_run = _run(directed_task, uuid.uuid4().hex, f"request-{uuid.uuid4().hex}", "定向认领的 Run")
            queued_run = _run(queued_task, uuid.uuid4().hex, f"request-{uuid.uuid4().hex}", "队列认领的 Run")
            await _admit(admission, sessions, directed_run)
            await _admit(admission, sessions, queued_run)

            async with sessions() as session:
                directed_before = await repository.by_run(session, directed_run.run_id)
                queued_before = await repository.by_run(session, queued_run.run_id)
                assert directed_before.fencing_token == 0 and queued_before.fencing_token == 0, "准入时的 fencing token 基线必须是零"

            async with sessions.begin() as session:
                directed = await repository.claim_run(session, directed_run.run_id, DIRECTIVE_OWNER)
                assert directed is not None, "定向认领必须成功"
                assert directed.run_id == directed_run.run_id, "定向认领必须命中指定 Run"
            async with sessions.begin() as session:
                queued = await repository.claim(session, "queue-worker")
                assert queued is not None, "队列认领必须领到仍可认领的 dispatch"
                assert queued.run_id == queued_run.run_id, "队列认领必须领到另一条 accepted dispatch"

            async with sessions() as session:
                for run_id, label in ((directed_run.run_id, "定向认领"), (queued_run.run_id, "队列认领")):
                    row = await repository.by_run(session, run_id)
                    assert row.status == "claimed", f"{label}后 dispatch 必须进入 claimed"
                    assert row.fencing_token == 1, f"{label}必须在准入的零基础上自增 1"
                    assert row.claimed_at is not None, f"{label}必须写入 claimed_at"
                    assert row.lease_expires_at is not None, f"{label}必须写入租约过期时间"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_released_directive_claim_marks_dispatch_and_run_failed(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        admission = RunAdmissionService()
        repository = RunDispatchRepository()
        reason = "delegated directive 在启动前已被用户权力或新状态取代"
        try:
            await _settle_active(sessions)
            task_id = await _seed_task(sessions, tmp_path, "released-claim")
            admitted = _run(task_id, uuid.uuid4().hex, f"request-{uuid.uuid4().hex}", "被拒绝启动的 Run")
            await _admit(admission, sessions, admitted)

            async with sessions.begin() as session:
                claim = await repository.claim_run(session, admitted.run_id, DIRECTIVE_OWNER)
                assert claim is not None, "定向认领必须成功"
                dispatch_id = claim.dispatch_id
                fencing_token = claim.fencing_token

            async with sessions.begin() as session:
                released = await repository.transition(session, dispatch_id, fencing_token, "failed_to_start", error=reason)
                assert released.status == "failed_to_start", "释放认领必须把 dispatch 转为 failed_to_start"
                assert released.error == reason, "释放认领必须记录错误文本"
                assert released.lease_expires_at is None, "释放认领必须清空租约"

            async with sessions() as session:
                dispatch_after = await repository.by_run(session, admitted.run_id)
                restored = await session.get(DesktopRun, admitted.run_id)
                assert dispatch_after.status == "failed_to_start", "释放后 dispatch 终态必须是 failed_to_start"
                assert dispatch_after.error == reason, "释放后 dispatch 必须保留错误文本"
                assert restored.status == "error", "释放认领必须把 Run 同步为 error"
                assert restored.error == reason, "Run 的错误文本必须与释放原因一致"
                assert restored.settled_at is not None, "失败终态必须写入 settled_at"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_launch_port_accepts_run_claimed_by_another_starter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            await _settle_active(sessions)
            fixture = await _seed_loop(sessions, tmp_path, label="port-held", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="port-held")
            desktop = _StubDesktopService(sessions, preclaim_by="queue-worker")
            port = DesktopDirectiveLaunchPort(sessions, desktop)
            monkeypatch.setattr(agent_loop_dispatch, "execute_prepared_run", _forbidden_spine)

            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
            launched = await port(directive, _message(), None)

            assert launched == desktop.run_id, "已被他人接手的 Run 必须被幂等接受为本次交付"
            async with sessions() as session:
                dispatch_row = await RunDispatchRepository().by_run(session, desktop.run_id)
                stored = await session.get(DesktopRun, desktop.run_id)
                anchor = await session.get(RunExecutionAnchor, desktop.run_id)
            assert dispatch_row.status == "claimed", "幂等接受不得改写他人持有的认领"
            assert dispatch_row.claimed_by == "queue-worker", "幂等接受不得把认领改成自己"
            assert dispatch_row.fencing_token == 1, "幂等接受不得再次自增 fencing token"
            assert stored.status == "pending", "幂等接受不得中止他人正在启动的 Run"
            assert anchor is None, "幂等接受不得为该 Run 绑定工作区"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_launch_port_rejects_run_whose_dispatch_is_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            await _settle_active(sessions)
            fixture = await _seed_loop(sessions, tmp_path, label="port-dead", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="port-dead")
            desktop = _StubDesktopService(sessions, dispatch_state="interrupted")
            port = DesktopDirectiveLaunchPort(sessions, desktop)
            monkeypatch.setattr(agent_loop_dispatch, "execute_prepared_run", _forbidden_spine)

            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
            with pytest.raises(LookupError) as rejected:
                await port(directive, _message(), None)

            assert "没有可用启动者" in str(rejected.value), "拒绝原因必须指明没有任何启动者会接手该 Run"
            async with sessions() as session:
                dispatch_row = await RunDispatchRepository().by_run(session, desktop.run_id)
                stored = await session.get(DesktopRun, desktop.run_id)
                anchor = await session.get(RunExecutionAnchor, desktop.run_id)
                created = int(
                    await session.scalar(
                        select(func.count()).select_from(DesktopRun).where(DesktopRun.directive_id == directive_id)
                    )
                    or 0
                )
            assert dispatch_row.status == "interrupted", "拒绝启动不得改动已终结的调度记录"
            assert stored.status == "error", "没有启动者的 Run 必须被终结为 error"
            assert stored.settled_at is not None, "被终结的 Run 必须写入 settled_at"
            assert "没有可用启动者接手" in (stored.error or ""), "被终结的 Run 必须记录拒绝原因"
            assert anchor is None, "拒绝启动不得绑定工作区"
            assert created == 1, "拒绝启动不得产生额外的 Run"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def _racing_bind(port: DesktopDirectiveLaunchPort, sessions, target_status: str):
    original = port._workspace.bind

    async def bind(**kwargs):
        result = await original(**kwargs)
        async with sessions.begin() as session:
            row = await session.get(DesktopRun, kwargs["run_id"], with_for_update=True)
            row.status = target_status
            if target_status != "running":
                row.settled_at = datetime.now(UTC)
        return result

    return bind


def test_launch_port_retires_claim_when_bound_run_is_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            await _settle_active(sessions)
            fixture = await _seed_loop(sessions, tmp_path, label="port-running", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="port-running")
            desktop = _StubDesktopService(sessions)
            port = DesktopDirectiveLaunchPort(sessions, desktop)
            monkeypatch.setattr(agent_loop_dispatch, "execute_prepared_run", _forbidden_spine)
            monkeypatch.setattr(port._workspace, "bind", _racing_bind(port, sessions, "running"))

            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
            launched = await port(directive, _message(), None)

            assert launched == desktop.run_id, "已被其它启动者启动的 Run 必须被接受为本次交付"
            async with sessions() as session:
                dispatch_row = await RunDispatchRepository().by_run(session, desktop.run_id)
                stored = await session.get(DesktopRun, desktop.run_id)
                persisted = await session.get(LoopDirective, directive_id)
            assert stored.status == "running", "已被启动的 Run 不得被中止"
            assert dispatch_row.status == "running", "本端口的认领必须随既有 Run 收口为 running"
            assert dispatch_row.lease_expires_at is not None, "收口为 running 的认领仍持有租约"
            assert persisted.status == "launching", "端口不得替交付路径改写 Directive 状态"
            assert persisted.launched_run_id is None, "端口不得替交付路径写入尝试绑定"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_launch_port_closes_claim_when_bound_run_already_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            await _settle_active(sessions)
            fixture = await _seed_loop(sessions, tmp_path, label="port-finished", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="port-finished")
            desktop = _StubDesktopService(sessions)
            port = DesktopDirectiveLaunchPort(sessions, desktop)
            monkeypatch.setattr(agent_loop_dispatch, "execute_prepared_run", _forbidden_spine)
            monkeypatch.setattr(port._workspace, "bind", _racing_bind(port, sessions, "success"))

            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
            launched = await port(directive, _message(), None)

            assert launched == desktop.run_id, "已结束的 Run 必须被接受为本次交付"
            async with sessions() as session:
                dispatch_row = await RunDispatchRepository().by_run(session, desktop.run_id)
                stored = await session.get(DesktopRun, desktop.run_id)
            assert stored.status == "success", "端口不得改写已结束 Run 的状态"
            assert dispatch_row.status == "settled", "已结束 Run 的认领必须关闭为 settled"
            assert dispatch_row.lease_expires_at is None, "关闭后的认领不得保留租约"
        finally:
            if fixture is not None:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())
