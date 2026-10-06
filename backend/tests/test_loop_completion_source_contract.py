"""本文件对外提供必要检查来源的真实命令与反例准入验证。

输入为隔离数据库、真实 pytest 结果及逐项变更的来源；输出为允许真实通过、拒绝空证据/自述/失败/过期/缺权限断言。
具体工作流为经公共 Validator 读取持久证据，逐一替换证据事实并验证确定性拒绝，再恢复合法事实。
示例：pytest backend/tests/test_loop_completion_source_contract.py；所有文件都位于临时测试工作区。
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.completion_sources import CompletionSourceValidator
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun, DesktopWorkspace
from completion_evidence_support import record_verified_fixture
from test_agent_loop_round_liveness import _seed_loop, _stop


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_required_checks_reject_unexecuted_failed_stale_and_self_asserted_sources(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="proof", started_at=datetime.now(UTC))
        try:
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                root = (await session.get(DesktopWorkspace, loop.workspace_id)).path
            source_id = await record_verified_fixture(sessions, fixture["loop_id"], fixture["context_id"], fixture["round_id"], root, 1)
            evidence = SimpleNamespace(kind="test", source_id=source_id)
            criterion = SimpleNamespace(check_id="tests", status="satisfied", evidence=[evidence])
            contract = SimpleNamespace(loop_id=fixture["loop_id"], workspace_revision=1, criteria=[criterion])
            checks = [{"check_id": "tests", "required": True}]
            validator = CompletionSourceValidator()
            async with sessions.begin() as session:
                source = await session.get(DesktopDomainResult, source_id)
                owner = await session.get(DesktopRun, source.run_id)
                payload, equipment = deepcopy(source.payload), deepcopy(owner.equipment)
                await validator.validate(session, checks, contract)
                criterion.evidence = []
                with pytest.raises(ValueError, match="缺少证据"):
                    await validator.validate(session, checks, contract)
                criterion.evidence = [evidence]
                for patch in ({"status": "failed"}, {"status": "not-run"}, {"workspace_revision": 2}, {"execution_proof": {}}):
                    source.payload = {**payload, **patch}
                    await session.flush()
                    with pytest.raises(ValueError):
                        await validator.validate(session, checks, contract)
                for patch in ({"exit_code": 1}, {"bound": False}, {"workspace": str(tmp_path)}, {"status": "timeout"}):
                    source.payload = {**payload, "execution_proof": {**payload["execution_proof"], **patch}}
                    await session.flush()
                    with pytest.raises(ValueError):
                        await validator.validate(session, checks, contract)
                source.payload = payload
                owner.equipment = {**equipment, "permissions": ["read"]}
                await session.flush()
                with pytest.raises(ValueError, match="缺少能力"):
                    await validator.validate(session, checks, contract)
                owner.equipment = equipment
                owner.settled_at = None
                with pytest.raises(ValueError, match="未结算"):
                    await validator.validate(session, checks, contract)
                owner.settled_at = datetime.now(UTC)
                source.kind = "run_outcome"
                await session.flush()
                with pytest.raises(ValueError, match="自述"):
                    await validator.validate(session, checks, contract)
                source.kind = "test"
                await session.flush()
                await validator.validate(session, checks, contract)
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
