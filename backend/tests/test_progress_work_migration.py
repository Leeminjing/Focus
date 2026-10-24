"""本文件验证进度迁移保留旧工作输入、已发布版本及 receipts。

输入为独立数据库的真实 Observation 和旧格式工作内容；输出为迁移后独立身份、固定输入与失效 fence 断言。
工作流在已验证建表迁移上重放数据转换，覆盖已发布、非当前 claimed 与当前 blocked 工作；不运行不可逆 downgrade。
示例：python -m pytest backend/tests/test_progress_work_migration.py -q。
"""

import asyncio
from datetime import UTC, datetime
import importlib
import os
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_round_task_progress import _Checkpointer

from backend.app.desktop.patrol.agent_loop.observations.observation_capture import LoopObservationService
from backend.app.desktop.patrol.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.patrol.agent_loop.task_progress.models import LoopProgressHead, LoopProgressReceipt, LoopProgressWork, LoopTaskProgress
from backend.app.desktop.patrol.agent_loop.task_progress.repository import TaskProgressRepository


def test_data_conversion_preserves_history_and_fences_only_obsolete_work(tmp_path, runtime_postgres_database):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="work-migrate", started_at=datetime.now(UTC))
        try:
            service = LoopObservationService(sessions, _Checkpointer())
            v1 = await service.capture(fixture["loop_id"], fixture["round_id"])
            v2 = await service.refresh(fixture["loop_id"], fixture["round_id"])
            v3 = await service.refresh(fixture["loop_id"], fixture["round_id"])
            original = {}
            async with sessions.begin() as session:
                repo = TaskProgressRepository()
                for observation, state in ((v1, "published"), (v2, "claimed"), (v3, "blocked")):
                    identity = observation.decision_inputs_ref
                    original[identity] = await repo.inputs(session, identity)
                    session.add(LoopProgressWork(work_id=identity, observation_id=identity,
                        loop_id=fixture["loop_id"], boundary_key=uuid.uuid4().hex, source_manifest={},
                        state=state, fence=3, usage=[{"fence": 3, "reserved_calls": 1, "accounted": False}]))
                prior = await repo.current(session, fixture["loop_id"])
                version = LoopTaskProgress(progress_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
                    generation=1, observation_id=v1.decision_inputs_ref, previous_progress_id=prior.progress_id,
                    document=prior.document, content_hash=prior.content_hash, contribution={"legacy": True})
                session.add(version)
                await session.flush()
                (await session.get(LoopProgressHead, fixture["loop_id"])).progress_id = version.progress_id
                key = original[v1.decision_inputs_ref].task_delta.sources[0].source_key
                session.add(LoopProgressReceipt(loop_id=fixture["loop_id"], source_key=key, progress_id=version.progress_id))
                version_id = version.progress_id
            migration = importlib.import_module("focus.persistence.migrations.versions.a70d1e2f3b4c_round_observation_versions")

            def migrate(connection):
                with Operations.context(MigrationContext.configure(connection)):
                    migration._progress_work()

            async with engine.begin() as connection:
                await connection.run_sync(migrate)
            async with sessions() as session:
                for observation, expected in ((v1, "published"), (v2, "superseding"), (v3, "blocked")):
                    identity = observation.decision_inputs_ref
                    work = await session.get(LoopProgressWork, identity)
                    assert work.state == expected and work.work_id == work.observation_id == identity
                    assert work.fence == (4 if expected == "superseding" else 3)
                    inputs = await TaskProgressRepository().consolidation_inputs(session, identity)
                    assert inputs.previous_progress_id == original[identity].previous_progress_id
                    assert inputs.task_delta == original[identity].task_delta
                    assert inputs.mission == observation.mission and inputs.origin_observation_hash == original[identity].observation_hash
                    assert work.consolidation_hash == canonical_hash(inputs)
                    assert await TaskProgressRepository().inputs(session, identity) == original[identity]
                version = await session.get(LoopTaskProgress, version_id)
                assert version.work_id == v1.decision_inputs_ref and version.contribution == {"legacy": True}
                assert (await session.get(LoopProgressReceipt, (fixture["loop_id"], key))).progress_id == version_id
                assert (await session.get(LoopProgressHead, fixture["loop_id"])).progress_id == version_id
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(exercise())
