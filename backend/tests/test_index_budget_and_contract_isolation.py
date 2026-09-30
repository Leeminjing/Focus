r"""本文件对外提供正式索引预算与完整配置隔离回归测试。

输入为隔离 PostgreSQL 和确定性计数模型；输出为切分／schema record 隔离与跨 build 预算合同断言。
工作流为由 fixture 创建随机数据库、发布真实 Revision、经过完整服务读写并校验持久化赢家和用量。
示例：python -m pytest backend/tests/test_index_budget_and_contract_isolation.py -q。
"""

import asyncio
import copy
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.desktop.agent_loop.context_expansion.index_budget_repository import (
    IndexBudgetReservationRepository,
)
from backend.app.desktop.agent_loop.context_expansion.index_model_budget import (
    IndexBudgetExceeded,
)
from backend.app.desktop.agent_loop.context_expansion.models import (
    LoopIndexBudgetReservation,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    ProtocolSafeRevisionSegmenter,
    RevisionSemanticIndexer,
)
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopDelegationGrant,
)
from backend.app.desktop.agent_loop.usage import LoopUsageDelta
from backend.tests.incremental_index_support import (
    ModelProbe,
    messages,
    observation,
    revision,
)
from backend.tests.test_incremental_revision_index import exercise

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("change", ["segmenter", "schema", "record_schema"])
def test_new_contract_does_not_adopt_old_projection_record(
    tmp_path, change, monkeypatch
):
    async def run(sessions, seed):
        first = await revision(sessions, seed["context_id"], messages(2))
        a = ModelProbe(variant=" A")
        old = (await a.service(sessions).build(observation(seed, first))).indexes[0]
        new_revision = await revision(
            sessions, seed["context_id"], messages(2), parent=first
        )
        b = ModelProbe(variant=" B")
        service = b.service(sessions)
        if change == "segmenter":
            service._indexer = RevisionSemanticIndexer(
                ProtocolSafeRevisionSegmenter(max_messages=24)
            )
        elif change == "schema":

            class NextSchemaIndexer(RevisionSemanticIndexer):
                INDEX_SCHEMA_VERSION = "revision-semantic-index-next"

            service._indexer = NextSchemaIndexer()
        else:
            from backend.app.desktop.agent_loop.context_expansion.segment_projection import (
                SegmentProjectionRecord,
            )

            monkeypatch.setattr(
                SegmentProjectionRecord,
                "SCHEMA_VERSION",
                "segment-projection-record-next",
            )
        result = await service.build(observation(seed, new_revision))
        assert result.blocker_code is None, result.blocker_summary
        current = result.indexes[0]
        assert current.inheritance.mode == "full" and len(b.calls) == 2
        assert all(unit.statement.endswith(" B") for unit in current.semantic_units)
        assert current.inheritance.record_ids != old.inheritance.record_ids, (
            "Full build under different schema/segmentation adopted the previous contract record"
        )

    exercise(tmp_path, run)


@pytest.mark.parametrize("dimension", ["input_tokens", "output_tokens"])
def test_shared_token_reservations_survive_reopen_and_settle_once(tmp_path, dimension):
    async def run(sessions, seed):
        limits = {f"max_{dimension}": 100}
        first = IndexBudgetReservationRepository(
            sessions, seed["loop_id"], 1, limits, {}
        )
        await first.reserve(60, 60)
        reopened = IndexBudgetReservationRepository(
            sessions, seed["loop_id"], 1, limits, {}
        )
        with pytest.raises(IndexBudgetExceeded):
            await reopened.reserve(60, 60)
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert len(rows) == 1 and rows[0].settled_at is None
        actual = LoopUsageDelta(model_calls=1, input_tokens=20, output_tokens=20)
        await asyncio.gather(first.settle(actual), first.settle(actual))
        await reopened.reserve(60, 60)
        await reopened.settle(actual)
        async with sessions() as session:
            usage = await session.get(LoopBudgetUsage, seed["loop_id"])
            assert usage.model_calls == 2
            assert usage.input_tokens == usage.output_tokens == 40
            rows = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert len(rows) == 2 and all(row.settled_at is not None for row in rows)

    exercise(tmp_path, run)


@pytest.mark.parametrize(
    "catalog_limit,expected_blocker",
    [(4_000_000, None), (1, "portfolio_catalog_overflow")],
)
def test_explicit_new_authorization_can_retry_without_changing_frozen_source(
    tmp_path, catalog_limit, expected_blocker
):
    async def run(sessions, seed):
        async with sessions.begin() as session:
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == seed["loop_id"]
                )
            )
            grant.budgets = {**grant.budgets, "max_model_calls": 2}
        target = await revision(sessions, seed["context_id"], messages(2))
        probe = ModelProbe(fail=True)
        service = probe.service(sessions)
        frozen = observation(seed, target, budget={"max_model_calls": 2})
        original = copy.deepcopy(vars(frozen))
        assert (await service.build(frozen)).blocker_code is not None
        probe.fail = False
        retry = SimpleNamespace(**copy.deepcopy(vars(frozen)))
        retry.budget["limits"]["max_model_calls"] = 4
        assert (await service.build(retry)).blocker_code == "portfolio_index_budget"
        async with sessions.begin() as session:
            old = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == seed["loop_id"]
                )
            )
            values = {
                name: getattr(old, name)
                for name in (
                    "holder_id",
                    "capabilities",
                    "context_scope",
                    "permission_scope",
                    "delegable_gates",
                    "compression_policy",
                )
            }
            old.status = "revoked"
            await session.flush()
            session.add(
                LoopDelegationGrant(
                    grant_id=uuid.uuid4().hex,
                    loop_id=seed["loop_id"],
                    revision=2,
                    budgets={
                        **old.budgets,
                        "max_model_calls": 4,
                        "expansion_resources": {
                            "max_catalog_descriptor_chars": catalog_limit
                        },
                    },
                    **values,
                )
            )
            (await session.get(AgentLoop, seed["loop_id"])).authority_revision = 2
        assert (await service.build(retry)).blocker_code == "portfolio_index_budget"
        result = await service.build(frozen, budget_authority_revision=2)
        assert result.blocker_code == expected_blocker, result.blocker_summary
        if expected_blocker is None:
            assert result.indexes[0].source == target.ref
        else:
            from backend.app.desktop.agent_loop.context_expansion.models import (
                LoopSemanticIndexArtifact,
            )

            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(LoopSemanticIndexArtifact.index_id).where(
                            LoopSemanticIndexArtifact.revision_id
                            == target.ref.revision_id
                        )
                    )
                    is None
                )
        assert vars(frozen) == original
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(LoopIndexBudgetReservation).where(
                        LoopIndexBudgetReservation.loop_id == seed["loop_id"]
                    )
                )
            ).all()
            assert {row.grant_revision for row in rows} == {1, 2}
            assert all(row.settled_at is not None for row in rows)
            assert (
                await session.get(LoopBudgetUsage, seed["loop_id"])
            ).model_calls == 4
        assert len(probe.calls) == 4

    exercise(tmp_path, run)


def test_concurrent_builds_share_remaining_authorized_budget(tmp_path):
    async def run(sessions, seed):
        arrivals = 0
        ready = asyncio.Event()

        async def gate(role, payload):
            nonlocal arrivals
            if role == "projector":
                arrivals += 1
                if arrivals == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)

        first = await revision(sessions, seed["context_id"], messages(2))
        a, b = ModelProbe(gate=gate), ModelProbe(gate=gate)
        obs = observation(seed, first, budget={"max_model_calls": 2})
        results = await asyncio.gather(
            a.service(sessions).build(obs), b.service(sessions).build(obs)
        )
        assert any(r.blocker_code == "portfolio_index_budget" for r in results)
        async with sessions() as session:
            used = (await session.get(LoopBudgetUsage, seed["loop_id"])).model_calls
        assert used <= 2, (
            f"Authorized max 2 model calls but concurrent builds billed {used}"
        )

    exercise(tmp_path, run)


def test_retry_cannot_reset_remaining_authorized_budget(tmp_path):
    async def run(sessions, seed):
        target = await revision(sessions, seed["context_id"], messages(2))
        probe = ModelProbe(fail=True)
        service = probe.service(sessions)
        obs = observation(seed, target, budget={"max_model_calls": 2})
        failed = await service.build(obs)
        assert failed.blocker_code and len(probe.calls) == 2
        probe.fail = False
        retried = await service.build(obs)
        assert retried.blocker_code == "portfolio_index_budget"
        async with sessions() as session:
            used = (await session.get(LoopBudgetUsage, seed["loop_id"])).model_calls
        assert used <= 2, (
            f"Unchanged authorization was exhausted but retry billed {used} calls"
        )

    exercise(tmp_path, run)
