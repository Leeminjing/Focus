r"""本文件对外提供显式启用的真实 provider 增量索引 smoke。

输入为宿主已配置模型、隔离数据库和合成消息；输出为真实局部复用、联合原文综合、独立 verdict、provider 用量与 exact 零调用证据。
工作流为 FOCUS_INDEX_REAL_MODEL=1 启用两项演练：稳定旧段追加、四段怀疑／否定／诊断综合并展开冻结旧原文。
示例：FOCUS_INDEX_REAL_MODEL=1 python -m pytest backend/tests/test_incremental_index_real_model.py -q。
"""

import json
import os
from pathlib import Path

import pytest
from focus.config.app_config import get_app_config

from backend.app.desktop.agent_loop.context_expansion.portfolio_index import (
    PortfolioSemanticIndexService,
)
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.tests.incremental_index_support import Checkpoints, observation, revision
from backend.tests.test_incremental_revision_index import exercise

pytestmark = [
    pytest.mark.usefixtures("isolated_postgres_database"),
    pytest.mark.skipif(
        os.environ.get("FOCUS_INDEX_REAL_MODEL") != "1",
        reason="explicit real-provider smoke",
    ),
]


def observed_model_factory(config, role, calls, usage):
    class ObservedModel:
        def __init__(self, role):
            self._model = RoleBoundStructuredModel(config, role)
            self._role = role

        @property
        def cache_identity(self):
            return self._model.cache_identity

        @property
        def last_attempt_records(self):
            return self._model.last_attempt_records

        def bind_request_guard(self, guard):
            self._model.bind_request_guard(guard)

        async def invoke_validated(self, schema, system, payload, validator):
            calls.append((self._role, payload))
            try:
                return await self._model.invoke_validated(
                    schema, system, payload, validator
                )
            finally:
                usage.extend(self.last_attempt_records)

    return lambda: ObservedModel(role)


def test_real_provider_locality_support_and_cache(tmp_path):
    async def run(sessions, seed):
        config = get_app_config("config.yaml")
        calls = []
        usage = []

        service = PortfolioSemanticIndexService(
            sessions,
            Checkpoints(),
            semantic_projector_factory=observed_model_factory(
                config, "semantic_index_projector", calls, usage
            ),
            semantic_claim_verifier_factory=observed_model_factory(
                config, "claim_verifier", calls, usage
            ),
        )
        history = [
            {
                "id": f"smoke-{i}",
                "role": "human",
                "content": f"Test batch {i} completed: 41 tests passed and 2 tests failed.",
            }
            for i in range(12)
        ]
        first = await revision(sessions, seed["context_id"], history)
        a = await service.build(observation(seed, first))
        assert a.blocker_code is None, a.blocker_summary
        assert (
            a.indexes[0].projected_unit_ids and a.indexes[0].quality_state == "complete"
        )
        calls.clear()
        next_message = {
            "id": "smoke-12",
            "role": "human",
            "content": "The authentication defect was fixed. Two failing tests still need investigation.",
        }
        new = await revision(
            sessions, seed["context_id"], history + [next_message], parent=first
        )
        b = await service.build(observation(seed, new))
        assert b.blocker_code is None, b.blocker_summary
        assert len(b.indexes[0].inheritance.reused_segment_ids) == 1
        projector_inputs = [
            p
            for role, p in calls
            if role == "semantic_index_projector" and "inventory" not in p
        ]
        assert len(projector_inputs) == 1
        assert [
            m["message_id"] for m in projector_inputs[0]["segments"][0]["messages"]
        ] == ["smoke-12"]
        assert b.indexes[0].quality_state == "complete"
        calls.clear()
        c = await service.build(observation(seed, new))
        assert c.indexes[0] == b.indexes[0] and calls == []
        evidence = {
            "provider": "configured real model",
            "first_quality": a.indexes[0].quality_state,
            "next_quality": b.indexes[0].quality_state,
            "reused_segments": 1,
            "recomputed_segments": 1,
            "exact_hit_calls": 0,
            "model_calls": sum(r.get("model_calls", 0) for r in usage),
            "input_tokens": sum(r.get("input_tokens", 0) for r in usage),
            "output_tokens": sum(r.get("output_tokens", 0) for r in usage),
        }
        Path(".tmp/index-real-smoke.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )

    exercise(tmp_path, run)


def test_real_provider_cross_segment_diagnosis_and_incremental_discovery(tmp_path):
    async def run(sessions, seed):
        from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
            ProtocolSafeRevisionSegmenter,
            RevisionSemanticIndexer,
        )
        from backend.tests.test_revision_interpretation import NARRATIVE

        config = get_app_config("config.yaml")
        calls, usage = [], []
        service = PortfolioSemanticIndexService(
            sessions,
            Checkpoints(),
            semantic_projector_factory=observed_model_factory(
                config, "semantic_index_projector", calls, usage
            ),
            semantic_claim_verifier_factory=observed_model_factory(
                config, "claim_verifier", calls, usage
            ),
        )
        service._indexer = RevisionSemanticIndexer(
            ProtocolSafeRevisionSegmenter(max_messages=1)
        )
        first = await revision(sessions, seed["context_id"], NARRATIVE[:2])
        before = await service.build(observation(seed, first))
        assert before.blocker_code is None, before.blocker_summary
        target = await revision(sessions, seed["context_id"], NARRATIVE, parent=first)
        calls.clear()
        result = await service.build(observation(seed, target))
        assert result.blocker_code is None, result.blocker_summary
        index = result.indexes[0]
        required = {"suspect", "reject", "diagnosis"}
        joint = [
            u
            for u in index.semantic_units
            if u.authority == "confirmed"
            and required.issubset({r.message_id for r in u.evidence_refs})
        ]
        assert joint, [
            (u.statement, [r.message_id for r in u.evidence_refs])
            for u in index.semantic_units
        ]
        assert (
            index.interpretation.completed
            and len(index.inheritance.reused_segment_ids) == 2
        )
        assert index.interpretation.read_requests
        assert any(len(p["segments"]) >= 3 for _, p in calls if "inventory" in p)
        assert any(
            len({s["message_id"] for s in c["supports"]}) >= 3
            for _, p in calls
            if "claims" in p
            for c in p["claims"]
        )
        assert all(r.source == target.ref for u in joint for r in u.evidence_refs)
        text = " ".join(u.statement.lower() for u in joint)
        assert any(w in text for w in ("thread", "线程")) and any(
            w in text for w in ("connection", "连接")
        )
        assert any(w in text for w in ("ruled", "reject", "排除", "否定", "excluded"))
        calls.clear()
        assert (await service.build(observation(seed, target))).indexes[
            0
        ] == index and not calls
        evidence = {
            "provider": "configured real model",
            "joint_statements": [u.statement for u in joint],
            "joint_supports": [[r.message_id for r in u.evidence_refs] for u in joint],
            "read_rounds": len(index.interpretation.read_requests),
            "reused_local_segments": 2,
            "quality": index.quality_state,
            "model_calls": sum(r.get("model_calls", 0) for r in usage),
            "input_tokens": sum(r.get("input_tokens", 0) for r in usage),
            "output_tokens": sum(r.get("output_tokens", 0) for r in usage),
            "exact_hit_calls": 0,
        }
        Path(".tmp/cross-real-smoke.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    exercise(tmp_path, run)
