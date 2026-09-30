r"""本文件对外提供显式启用的真实 provider 增量索引 smoke。

输入为宿主已配置的模型、隔离数据库及合成消息；输出为真实投影／独立验证质量、尾部输入局部性和 exact-hit 零调用证据。
工作流为仅在 FOCUS_INDEX_REAL_MODEL=1 时启用，完整构建一个已满段，再追加一条消息并验证旧段复用。
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


def test_real_provider_locality_support_and_cache(tmp_path):
    async def run(sessions, seed):
        config = get_app_config("config.yaml")
        calls = []
        usage = []

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

        service = PortfolioSemanticIndexService(
            sessions,
            Checkpoints(),
            semantic_projector_factory=lambda: ObservedModel(
                "semantic_index_projector"
            ),
            semantic_claim_verifier_factory=lambda: ObservedModel("claim_verifier"),
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
            p for role, p in calls if role == "semantic_index_projector"
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
