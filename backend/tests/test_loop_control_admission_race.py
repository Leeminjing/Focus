"""本文件对外提供暂停、停止、撤权与后代启动及工具效果的并发边界验收。

输入为每用例独立的 PostgreSQL、已准入的主 Run、并发 worker/teammate 准入及真实控制入口；输出为控制后无新启动或写意图、已受理执行最终清理的断言。
具体工作流为同时提交控制与后代准入，允许控制之前合法受理或控制之后明确拒绝，随后经 durable worker 的真实工作区绑定复检及 finalizer 清理。
示例：pytest backend/tests/test_loop_control_admission_race.py；不执行未授权 Shell 或写文件。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.schemas import RevokeLoopGrantRequest
from backend.app.desktop.execution_attempts.tool import ToolExecutionLedger
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.dispatch import DurableRunDispatchWorker
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_execution_ownership import _seed_launching_directive, _admit_run
from backend.tests.test_loop_failure_repair_transaction import ExecutionAssembler

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


@pytest.mark.parametrize("command", ["pause", "stop", "revoke"])
@pytest.mark.parametrize("kind", ["worker", "teammate"])
def test_control_fences_concurrent_descendant_and_queued_start(tmp_path, command, kind):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="control-race", started_at=datetime.now(UTC))
        identities = []
        try:
            directive_id = await _seed_launching_directive(sessions, fixture, label="control-race")
            parent = await _admit_run(sessions, fixture, directive_id)
            identities.append(parent)
            async with sessions() as session:
                slot_id = (await session.get(DesktopRun, parent)).workspace_anchor["slot_id"]

            async def control():
                if command == "revoke":
                    return await LoopAuthorityService(sessions).mutate(fixture["loop_id"], RevokeLoopGrantRequest(command="revoke"))
                return await fixture["service"].control(fixture["loop_id"], command)

            async def admit():
                async with sessions.begin() as session:
                    identity = uuid.uuid4().hex
                    await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, DesktopRun(
                        run_id=identity, task_id=fixture["context_id"], agent_id=identity, kind=kind,
                        status="pending", origin="delegated_patrol", loop_id=fixture["loop_id"], round_id=fixture["round_id"],
                        parent_run_id=parent, directive_id=directive_id, context_revision_id=fixture["revision_id"],
                        equipment={"permissions": ["read"]}, workspace_anchor={"slot_id": slot_id}))
                return identity

            controlled, admitted = await asyncio.gather(control(), admit(), return_exceptions=True)
            assert not isinstance(controlled, BaseException)
            if isinstance(admitted, BaseException):
                assert isinstance(admitted, ValueError)
            else:
                identities.append(admitted)
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture["loop_id"])).status != "running"
                assert all([(await session.get(DesktopRun, identity)).status == "interrupted" for identity in identities])
            ledger = ToolExecutionLedger(sessions, authority_validator=RunOwnershipPolicy().assert_live)
            with pytest.raises(ValueError):
                await ledger.claim(uuid.uuid4().hex, parent, {"id": "old-write", "name": "write_file", "args": {"path": "forbidden.txt", "content": "old"}})
            started = []

            async def start(assembly):
                started.append(assembly.run_id)

            worker = DurableRunDispatchWorker(sessions, "old-control-race", ExecutionAssembler(sessions, fixture, slot_id), start)
            assert await worker.drain(limit=4) == len(identities)
            assert not started
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            for identity in identities:
                await RunLifecycleFinalizer(sessions, SimpleNamespace()).abort_prepared(identity, "race cleanup")
            await engine.dispose()
    asyncio.run(run())
