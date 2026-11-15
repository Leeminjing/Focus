"""Original/new service comparison on identical frozen synthetic Revisions and real PostgreSQL."""

import asyncio
import json
import logging
import math
import os
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import delete, select

from backend.app.desktop.contexts.evolution import (
    ContextRevisionContract,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.contracts import (
    stable_expansion_hash,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.independent_portfolio_index import (
    FocusIndexAdmission,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.models import (
    LoopSegmentProjectionRecord,
    LoopSemanticIndexArtifact,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopDelegationGrant,
)
from backend.app.desktop.persistence.models import DesktopThread
from backend.tests.incremental_index_support import (
    ModelProbe,
    messages,
    observation,
    revision,
)
from backend.tests.test_incremental_revision_index import exercise
from backend.tests.test_independent_context_index import make_service

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def p95(values):
    return sorted(values)[max(0, math.ceil(0.95 * len(values)) - 1)] if values else None


@pytest.mark.parametrize("context_count", [1, 4, 16])
@pytest.mark.parametrize(
    "repeat", range(int(os.environ.get("FOCUS_INDEX_COMPARISON_REPEATS", "1")))
)
def test_old_and_independent_service_contention(
    tmp_path, monkeypatch, context_count, repeat
):
    async def run(sessions, seed):
        contexts = [seed["context_id"]]
        targets = [await revision(sessions, contexts[0], messages(55))]
        async with sessions.begin() as session:
            loop = await session.get(AgentLoop, seed["loop_id"])
            for _ in range(1, context_count):
                context_id = uuid.uuid4().hex
                contexts.append(context_id)
                session.add(
                    DesktopThread(
                        task_id=context_id,
                        workspace_id=loop.workspace_id,
                        thread_id=f"benchmark-{context_id}",
                        title="Index benchmark",
                    )
                )
                await session.flush()
                ref = ContextRevisionRef(
                    context_id=context_id,
                    revision_id=uuid.uuid4().hex,
                    generation=1,
                    execution_thread_id=f"benchmark-{context_id}",
                    checkpoint_id=uuid.uuid4().hex,
                    payload_mode="checkpoint",
                )
                raw = tuple(messages(2))
                target = ContextRevisionContract(
                    ref=ref,
                    authored_messages=raw,
                    execution_messages=raw,
                    content_hash=stable_expansion_hash("test-execution", raw),
                    projection_status="valid",
                    origin_kind="root",
                    created_at=datetime.now(UTC),
                )
                await ContextRevisionRepository().insert(session, target)
                await ContextRevisionRepository().switch_current(session, ref, None)
                targets.append(target)
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == seed["loop_id"]
                )
            )
            grant.context_scope = contexts
        appended = [
            await revision(
                sessions, c, messages(57 if i == 0 else 3), parent=targets[i]
            )
            for i, c in enumerate(contexts)
        ]
        reports, expected, queue = [], {}, []

        class QueueHandler(logging.Handler):
            def emit(self, record):
                if hasattr(record, "queue_wait_ms"):
                    queue.append(record.queue_wait_ms)

        logger = logging.getLogger(
            "backend.app.desktop.patrol.agent_loop.context_expansion.index_model_budget"
        )
        handler, level = QueueHandler(), logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        settle = FocusIndexAdmission.settle

        async def record_settle(self, call, receipt):
            queue.append(receipt.queue_ms)
            await settle(self, call, receipt)

        monkeypatch.setattr(FocusIndexAdmission, "settle", record_settle)
        order = ["original", "independent"]
        random.Random(20261031 + repeat * 100 + context_count).shuffle(order)
        try:
            for implementation in order:
                async with sessions.begin() as session:
                    # Only this test's explicitly seeded synthetic Contexts are cleared.
                    await session.execute(
                        delete(LoopSemanticIndexArtifact).where(
                            LoopSemanticIndexArtifact.context_id.in_(contexts)
                        )
                    )
                    await session.execute(
                        delete(LoopSegmentProjectionRecord).where(
                            LoopSegmentProjectionRecord.context_id.in_(contexts)
                        )
                    )

                async def latency(role, payload):
                    await asyncio.sleep(0.002)

                probe = ModelProbe(gate=latency)
                service = (
                    probe.service(sessions, concurrency=4)
                    if implementation == "original"
                    else make_service(sessions, probe, concurrency=4)
                )
                identities = {
                    r: service._model_identity(
                        getattr(service, f"_semantic_{r}_factory")
                    )
                    for r in ("projector", "claim_verifier", "interpreter")
                }
                for phase, versions in (
                    ("cold", targets),
                    ("append", appended),
                    ("exact", appended),
                ):
                    queue.clear()
                    before = len(probe.calls)
                    async with sessions() as session:
                        charged_before = (
                            await session.get(LoopBudgetUsage, seed["loop_id"])
                        ).model_calls
                    inputs = [observation(seed, target) for target in versions]

                    async def build_one(i, service=service, inputs=inputs, phase=phase):
                        started = perf_counter()
                        result = await service.build(inputs[i])
                        elapsed = (perf_counter() - started) * 1000
                        assert result.blocker_code is None, result.blocker_summary
                        units = [
                            u.model_dump(mode="json")
                            for u in result.indexes[0].semantic_units
                        ]
                        if (phase, i) not in expected:
                            expected[(phase, i)] = units
                        else:
                            assert units == expected[(phase, i)]
                        return elapsed

                    started = perf_counter()
                    elapsed = await asyncio.gather(
                        *(build_one(i) for i in range(context_count))
                    )
                    batch_ms = (perf_counter() - started) * 1000
                    async with sessions() as session:
                        charged = (
                            await session.get(LoopBudgetUsage, seed["loop_id"])
                        ).model_calls - charged_before
                    actual = len(probe.calls) - before
                    assert charged == actual
                    assert phase != "exact" or actual == 0
                    assert probe.max_active <= 4
                    reports.append(
                        {
                            "implementation": implementation,
                            "phase": phase,
                            "context_count": context_count,
                            "batch_duration_ms": batch_ms,
                            "caller_duration_ms": elapsed,
                            "short_caller_p95_ms": p95(elapsed[1:]),
                            "queue_p95_ms": p95(queue) or 0,
                            "model_calls": actual,
                            "charged_model_calls": charged,
                            "model_identities": identities,
                            "semantic_protocol_parity": True,
                            "quality_pass": None,
                            "human_semantic_review": "pending",
                        }
                    )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)
        output = {
            "schema": "focus-index-service-comparison-v1",
            "mode": "offline-scripted-postgresql",
            "context_count": context_count,
            "repeat": repeat,
            "execution_order": order,
            "concurrency": 4,
            "simulated_model_latency_ms": 2,
            "frozen_sources": [
                t.ref.model_dump(mode="json") for t in targets + appended
            ],
            "content_hashes": [t.content_hash for t in targets + appended],
            "real_history_comparison": "pending_data",
            "runs": reports,
        }
        directory = Path(
            os.environ.get("FOCUS_INDEX_COMPARISON_REPORT_DIR", str(tmp_path))
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"contexts-{context_count}-repeat-{repeat}.json").write_text(
            json.dumps(output, indent=2) + "\n", encoding="utf-8"
        )

    exercise(tmp_path, run)
