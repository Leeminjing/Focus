"""本文件对外提供冻结任务记忆模型的实际、预留与未知消费回归。

输入为隔离 PostgreSQL、生产记忆 Runtime、冻结 Observation 与可控模型用量；输出为逐调用账本、幂等回填和 Live accounting 一致断言。
具体工作流为经 Runtime 实际领取有效 work/fence/lease 后预留，读取执行中消费，再报告实际或未知用量，重复回填不重复消费；所有数据仅在测试数据库。
示例：pytest backend/tests/test_loop_memory_accounting.py，不把预留 Token 当成 Provider 报告的实际消费。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from focus.runtime.runs.usage import ModelUsage

from backend.app.desktop.agent_loop.accounting_query import LoopAccountingQuery
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.tests.config_helpers import app_config_for
from backend.tests.incremental_index_support import Checkpoints
from test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("reported", [True, False])
def test_memory_receipt_projects_reservation_and_actual_or_unknown(reported, tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="memory-receipt", started_at=datetime.now(UTC))
        loop_id = fixture["loop_id"]
        try:
            envelope = await LoopObservationService(sessions, Checkpoints()).capture(loop_id, fixture["round_id"])
            identity = envelope.decision_inputs_ref
            runtime = TaskProgressRuntime(sessions, app_config_for("memory-test", None))
            claimed_identity, fence = await runtime._claim(loop_id=loop_id)
            assert claimed_identity == identity
            await runtime._reserve(identity, fence, envelope.model_dump(mode="json"), 100, 50)
            async with sessions() as session:
                reserved = await LoopAccountingQuery().read(session, loop_id)
                assert reserved["consumption"]["actual"] == {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
                assert reserved["consumption"]["unreported_reservations"] == {"model_calls": 1, "input_tokens": 100, "output_tokens": 50}
                assert reserved["unknown_model_attempts"] == 0
            model = SimpleNamespace(last_usage_reported=reported,
                last_usage=ModelUsage(model_calls=1, input_tokens=17, output_tokens=9) if reported else ModelUsage())
            await runtime._record_usage(identity, fence, model)
            await runtime._record_usage(identity, fence, model)
            async with sessions() as session:
                settled = await LoopAccountingQuery().read(session, loop_id)
                actual = {"model_calls": 1, "input_tokens": 17, "output_tokens": 9} if reported else {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
                assert settled["consumption"]["actual"] == actual
                assert settled["consumption"]["occupied"]["model_calls"] == 1
                assert settled["unknown_model_attempts"] == int(not reported)
                projected = await LoopLiveSnapshotProjector().project(session, loop_id)
                assert projected.accounting.state["accounting"] == settled
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()

    asyncio.run(run())
