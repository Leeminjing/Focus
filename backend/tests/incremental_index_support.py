r"""本文件对外提供隔离索引测试的 Revision 播种、冻结 Observation 和计数模型端口。

输入为真实隔离 PostgreSQL sessionmaker 与执行消息；输出为已提交的 Context Revision、最小冻结输入和可观测模型调用。
工作流为真实 Loop bootstrap，发布权威后继，模型只从请求原文生成 drafts；默认综合完成空结果。
calls 记录全部尝试；interpretation_calls 为综合回合，local_calls 仅排除综合回合，联合 verifier 通过 payload 或 index_phase 另行识别。
示例：probe = ModelProbe(); service = probe.service(sessions)。测试模型不访问 provider，不隐藏模型 payload。
"""

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.portfolio_index import (
    PortfolioSemanticIndexService,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SegmentSemanticUnitDraft,
    SemanticClaimSupportAssessment,
    SemanticClaimSupportProposal,
    SemanticProjectionProposal,
    SemanticSupportSpan,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    RevisionSemanticIndexer,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)


class Checkpoints:
    async def aget_tuple(self, config):
        return SimpleNamespace(
            config=config, checkpoint={"channel_values": {"messages": []}}
        )


async def revision(
    sessions,
    context_id,
    messages,
    *,
    parent=None,
    origin="run_settled",
    published=True,
    sources=None,
):
    repository = ContextRevisionRepository()
    async with sessions.begin() as session:
        expected = await repository.current(session, context_id)
        generation = await repository.next_generation(session, context_id)
        ref = ContextRevisionRef(
            context_id=context_id,
            revision_id=uuid.uuid4().hex,
            generation=generation,
            execution_thread_id=f"index:{uuid.uuid4().hex}",
            checkpoint_ns="",
            checkpoint_id=uuid.uuid4().hex,
            payload_mode="checkpoint",
        )
        contract = ContextRevisionContract(
            ref=ref,
            sources=tuple(
                ContextRevisionSourceContract(source=s, position=i)
                for i, s in enumerate(
                    sources if sources is not None else ([parent.ref] if parent else [])
                )
            ),
            authored_messages=tuple(messages),
            execution_messages=tuple(messages),
            content_hash=stable_expansion_hash("test-execution", messages),
            projection_status="valid",
            origin_kind=origin if parent or sources else "root",
            created_at=datetime.now(UTC),
        )
        await repository.insert(session, contract)
        if published:
            await repository.switch_current(session, ref, expected.ref)
        return contract


def observation(seed, target, *, budget=None, scope=None, hash_override=None):
    return SimpleNamespace(
        loop_id=seed["loop_id"],
        round_id=seed["round_id"],
        portfolio_frontier=(
            {
                "revision": target.ref.model_dump(mode="json"),
                "content_hash": hash_override or target.content_hash,
                "role": "primary",
            },
        ),
        observed_frontier_hash=stable_expansion_hash(
            "index-test-frontier", target.ref.revision_id
        ),
        grant={
            "context_scope": scope if scope is not None else [target.ref.context_id]
        },
        goal={"outcome": "index tests"},
        mission={},
        authority_revision=1,
        budget={
            "limits": {
                "max_model_calls": 10000,
                "max_input_tokens": 10000000,
                "max_output_tokens": 10000000,
                **(budget or {}),
            },
            "usage": {},
        },
    )


def messages(count, start=0):
    return [
        {"id": f"m-{i}", "role": "human", "content": f"Evidence number {i}"}
        for i in range(start, start + count)
    ]


class ModelProbe:
    @property
    def local_calls(self):
        return tuple(call for call in self.calls if call[0] != "interpretation")

    @property
    def interpretation_calls(self):
        return tuple(call for call in self.calls if call[0] == "interpretation")

    def __init__(
        self,
        *,
        verdict="supported",
        variant="",
        invalid=None,
        gate=None,
        fail=False,
        version="v1",
        authority="confirmed",
    ):
        self.calls = []
        self.verdict = verdict
        self.variant = variant
        self.invalid = invalid
        self.gate = gate
        self.fail = fail
        self.version = version
        self.authority = authority
        self.active = 0
        self.max_active = 0

    def factory(self, role):
        probe = self

        class Model:
            last_attempt_records = ()

            @property
            def cache_identity(self):
                return f"test-{role}-{probe.version}"

            async def invoke(self, schema, system, payload):
                actual_role = (
                    "interpretation"
                    if schema.__name__ == "RevisionInterpretationProposal"
                    else role
                )
                probe.calls.append((actual_role, payload))
                probe.active += 1
                probe.max_active = max(probe.max_active, probe.active)
                try:
                    if probe.gate:
                        await probe.gate(actual_role, payload)
                    self.last_attempt_records = (
                        {
                            "role": actual_role,
                            "model_calls": 1,
                            "input_tokens": len(json.dumps(payload).encode()) // 4,
                            "output_tokens": 10,
                            "attempt": 1,
                        },
                    )
                    if probe.fail:
                        raise ValueError("test model failure")
                    if actual_role == "interpretation":
                        return schema(action="complete", units=())
                    if role == "projector":
                        drafts = []
                        for raw in payload["segments"][0]["messages"]:
                            text = RevisionSemanticIndexer.message_content(
                                type("Message", (), raw)()
                            )
                            if not text or probe.invalid == "empty":
                                continue
                            drafts.append(
                                SegmentSemanticUnitDraft(
                                    kind="claim",
                                    authority=probe.authority,
                                    statement=text + probe.variant,
                                    supports=(
                                        SemanticSupportSpan(
                                            message_id="foreign"
                                            if probe.invalid == "foreign"
                                            else raw["message_id"],
                                            quote="fabricated"
                                            if probe.invalid == "quote"
                                            else text,
                                        ),
                                    ),
                                )
                            )
                        return SemanticProjectionProposal(units=tuple(drafts))
                    return SemanticClaimSupportProposal(
                        assessments=tuple(
                            SemanticClaimSupportAssessment(
                                claim_key=c["claim_key"],
                                verdict=probe.verdict,
                                reason="isolated deterministic verifier",
                            )
                            for c in payload["claims"]
                        )
                    )
                finally:
                    probe.active -= 1

        return Model

    def service(self, sessions, **kwargs):
        options = {
            "semantic_projector_factory": self.factory("projector"),
            "semantic_claim_verifier_factory": self.factory("verifier"),
            **kwargs,
        }
        return PortfolioSemanticIndexService(
            sessions,
            Checkpoints(),
            **options,
        )
