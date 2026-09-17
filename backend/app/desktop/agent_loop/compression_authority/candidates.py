r"""本文件对外提供 CompressionCandidateService 的有界 manifest 与非权威候选准备。

输入为 pending decision、精确 Context revision、可选 message ids、当前 app config 与 Loop budget；
输出为不含正文的 manifest 或持久化 prepared candidate。具体工作流为短事务冻结身份，读取精确
revision 后规范化协议范围，事务外复用既有摘要模型，再在第二个短事务重验版本并持久化候选与用量；
全过程不修改 checkpoint、Context current revision 或 Loop 控制状态。示例：
`candidate = await service.prepare(loop_id, request)`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.compression_authority.contracts import AutonomousCompressionPolicy, CompressionCandidateRequest
from backend.app.desktop.agent_loop.compression_authority.evidence import CompressionEvidenceVerifier
from backend.app.desktop.agent_loop.compression_authority.manifest import CompressionManifestBuilder
from backend.app.desktop.agent_loop.compression_authority.models import LoopCompressionCandidate
from backend.app.desktop.agent_loop.compression_authority.repository import CompressionAuthorityRepository
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant, LoopEventOutbox, LoopPendingDecision, LoopRound
from backend.app.desktop.compression import summarize_messages
from backend.app.desktop.context_evolution import ContextRevisionReader, ContextRevisionRepository
from focus.messages.usage import estimate_messages_tokens
from focus.runtime.runs.events import deserialize_messages


class CompressionCandidateService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        app_config: Any,
    ) -> None:
        self._sessions = sessions
        self._reader = ContextRevisionReader(ContextRevisionRepository(), checkpointer)
        self._revisions = ContextRevisionRepository()
        self._app_config = app_config
        self._manifest = CompressionManifestBuilder()
        self._repository = CompressionAuthorityRepository()

    async def manifest(self, loop_id: str, request: CompressionCandidateRequest, *, offset: int = 0, limit: int = 80) -> dict[str, Any]:
        async with self._sessions() as session:
            _, _, pending, revision, messages, _ = await self._load(session, loop_id, request)
            protected = self._protected_message_ids(pending)
            page = self._manifest.page(messages, protected_message_ids=protected, offset=offset, limit=limit)
            return {**page, "context_id": request.context_id, "revision_id": revision.ref.revision_id, "checkpoint_id": revision.ref.checkpoint_id}

    async def prepare(self, loop_id: str, request: CompressionCandidateRequest) -> LoopCompressionCandidate:
        async with self._sessions.begin() as session:
            loop, grant, pending, revision, messages, policy = await self._load(session, loop_id, request, lock=True)
            live = await self._repository.live_candidate(session, pending.pending_decision_id)
            if live is not None and live.expires_at > datetime.now(UTC) and live.base_context_revision_id == revision.ref.revision_id:
                return live
            if live is not None:
                live.status = "expired" if live.expires_at <= datetime.now(UTC) else "superseded"
            attempts = int(
                await session.scalar(
                    select(func.count()).select_from(LoopEventOutbox).where(
                        LoopEventOutbox.loop_id == loop_id,
                        LoopEventOutbox.idempotency_key.like(
                            f"compression-candidate-attempt:{pending.pending_decision_id}:%"
                        ),
                    )
                )
                or 0
            )
            if attempts >= policy.max_attempts_per_gate:
                raise ValueError("该 compression gate 的候选准备次数已耗尽")
            usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
            if usage is None:
                raise ValueError("Loop budget usage 不存在")
            if usage.model_calls >= int(grant.budgets.get("max_model_calls", 0)):
                raise ValueError("Loop model call budget 已耗尽")
            normalized = self._manifest.normalize(
                messages,
                request.source_message_ids,
                policy,
                protected_message_ids=self._protected_message_ids(pending),
            )
            self._require_candidate_budget(usage, grant.budgets, normalized.before_tokens)
            await self._record_preparation_start(session, loop_id, pending, request, attempts + 1)
            frozen = {
                "loop_revision": loop.revision,
                "authority_revision": loop.authority_revision,
                "goal_revision": loop.goal_revision,
                "round_id": loop.current_round_id,
                "frontier_hash": (await session.get(LoopRound, loop.current_round_id)).frontier_hash,
                "revision_id": revision.ref.revision_id,
                "checkpoint_id": revision.ref.checkpoint_id,
                "pending_id": pending.pending_decision_id,
            }
        try:
            summary = await summarize_messages(list(normalized.messages), (loop.equipment or {}).get("patrol_model_name"), self._app_config)
        except Exception:
            await self._record_failed_attempt(loop_id, normalized.before_tokens, 0)
            raise
        after_tokens = estimate_messages_tokens(deserialize_messages([{"id": "candidate-summary", "role": "human", "content": summary}], validate=False))
        if normalized.before_tokens - after_tokens < policy.min_reduction_tokens:
            await self._record_failed_attempt(loop_id, normalized.before_tokens, after_tokens)
            raise ValueError("候选摘要未达到 delegation policy 的最小 token 减量")
        ranges = CompressionEvidenceVerifier.bind_ranges(
            normalized.messages,
            [{"source_ids": list(normalized.source_ids), "replacement": summary}],
        )
        replacement_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        candidate_hash = hashlib.sha256(
            json.dumps({**frozen, "ranges": ranges, "policy": policy.model_dump(mode="json")}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        try:
            return await self._persist_candidate(
                loop_id,
                request,
                frozen,
                normalized,
                ranges,
                replacement_hash,
                candidate_hash,
                after_tokens,
                policy,
            )
        except Exception:
            await self._record_failed_attempt(loop_id, normalized.before_tokens, after_tokens)
            raise

    async def _persist_candidate(
        self,
        loop_id: str,
        request: CompressionCandidateRequest,
        frozen: dict[str, Any],
        normalized,
        ranges: list[dict[str, Any]],
        replacement_hash: str,
        candidate_hash: str,
        after_tokens: int,
        policy: AutonomousCompressionPolicy,
    ) -> LoopCompressionCandidate:
        async with self._sessions.begin() as session:
            loop_now, grant_now, pending_now, revision_now, _, policy_now = await self._load(session, loop_id, request, lock=True)
            round_now = await session.get(LoopRound, loop_now.current_round_id, with_for_update=True)
            if {
                "loop_revision": loop_now.revision,
                "authority_revision": loop_now.authority_revision,
                "goal_revision": loop_now.goal_revision,
                "round_id": loop_now.current_round_id,
                "frontier_hash": round_now.frontier_hash,
                "revision_id": revision_now.ref.revision_id,
                "checkpoint_id": revision_now.ref.checkpoint_id,
                "pending_id": pending_now.pending_decision_id,
            } != frozen or policy_now != policy:
                raise ValueError("候选准备期间 Loop authority、goal 或 Context frontier 已变化")
            existing = await session.scalar(select(LoopCompressionCandidate).where(LoopCompressionCandidate.candidate_hash == candidate_hash))
            usage = await session.get(LoopBudgetUsage, loop_id, with_for_update=True)
            if usage is None:
                raise ValueError("Loop budget usage 不存在")
            self._require_candidate_budget(usage, grant_now.budgets, normalized.before_tokens, after_tokens)
            usage.model_calls += 1
            usage.input_tokens += normalized.before_tokens
            usage.output_tokens += after_tokens
            if existing is not None:
                return existing
            candidate = LoopCompressionCandidate(
                candidate_id=uuid.uuid4().hex,
                loop_id=loop_id,
                pending_decision_id=pending_now.pending_decision_id,
                round_id=loop_now.current_round_id,
                context_id=request.context_id,
                base_context_revision_id=revision_now.ref.revision_id,
                base_checkpoint_id=revision_now.ref.checkpoint_id,
                frontier_hash=round_now.frontier_hash,
                authority_revision=loop_now.authority_revision,
                goal_revision=loop_now.goal_revision,
                policy_revision=policy.version,
                normalized_ranges=ranges,
                replacement_hash=replacement_hash,
                candidate_hash=candidate_hash,
                before_tokens=normalized.before_tokens,
                after_tokens=after_tokens,
                protection_evidence=list(normalized.protection_evidence),
                expires_at=datetime.now(UTC) + timedelta(seconds=policy.candidate_ttl_seconds),
            )
            session.add(candidate)
            session.add(
                LoopEventOutbox(
                    event_id=uuid.uuid4().hex,
                    loop_id=loop_id,
                    sequence=int(
                        await session.scalar(
                            select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(
                                LoopEventOutbox.loop_id == loop_id
                            )
                        )
                        or 0
                    ) + 1,
                    event_type="CompressionCandidatePrepared",
                    payload={
                        "candidate_id": candidate.candidate_id,
                        "pending_decision_id": pending_now.pending_decision_id,
                        "estimated_reduction": normalized.before_tokens - after_tokens,
                    },
                    idempotency_key=f"compression-candidate-prepared:{candidate.candidate_id}",
                )
            )
            return candidate

    @staticmethod
    async def _record_preparation_start(
        session: AsyncSession,
        loop_id: str,
        pending: LoopPendingDecision,
        request: CompressionCandidateRequest,
        attempt: int,
    ) -> None:
        sequence = int(
            await session.scalar(
                select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(
                    LoopEventOutbox.loop_id == loop_id
                )
            )
            or 0
        ) + 1
        session.add(
            LoopEventOutbox(
                event_id=uuid.uuid4().hex,
                loop_id=loop_id,
                sequence=sequence,
                event_type="CompressionCandidatePreparing",
                payload={
                    "pending_decision_id": pending.pending_decision_id,
                    "context_id": request.context_id,
                    "context_revision_id": request.context_revision_id,
                    "attempt": attempt,
                },
                idempotency_key=f"compression-candidate-attempt:{pending.pending_decision_id}:{attempt}",
            )
        )

    async def _load(self, session, loop_id, request, *, lock: bool = False):
        loop_query = select(AgentLoop).where(AgentLoop.loop_id == loop_id)
        if lock:
            loop_query = loop_query.with_for_update()
        loop = await session.scalar(loop_query)
        if loop is None or loop.status != "running":
            raise ValueError("Loop 不存在或未运行")
        grant_query = select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop_id,
            LoopDelegationGrant.revision == loop.authority_revision,
            LoopDelegationGrant.status == "active",
        )
        if lock:
            grant_query = grant_query.with_for_update()
        grant = await session.scalar(grant_query)
        if grant is None or "compression" not in set(grant.delegable_gates or ()):
            raise ValueError("当前 delegation 未授权自主压缩")
        if not grant.compression_policy or "apply_context_compression" not in set(grant.capabilities or ()):
            raise ValueError("当前 delegation 只保留旧 compression gate，不包含自主压缩权力")
        if request.context_id not in set(grant.context_scope or ()):
            raise ValueError("目标 Context 不在 delegation scope")
        policy = AutonomousCompressionPolicy.model_validate(grant.compression_policy)
        pending = await session.get(LoopPendingDecision, request.pending_decision_id, with_for_update=lock)
        if pending is None or pending.loop_id != loop_id or pending.kind != "compression" or pending.status != "pending" or not pending.delegable:
            raise ValueError("compression pending decision 不可用")
        revision = await self._revisions.current(session, request.context_id)
        if revision is None or revision.ref.revision_id != request.context_revision_id:
            raise ValueError("Context revision 已变化")
        pending_identity = pending.payload or {}
        expected_identity = {
            "context_id": request.context_id,
            "context_revision_id": revision.ref.revision_id,
            "checkpoint_id": revision.ref.checkpoint_id,
            "round_id": loop.current_round_id,
            "authority_revision": loop.authority_revision,
            "goal_revision": loop.goal_revision,
        }
        if any(pending_identity.get(key) != value for key, value in expected_identity.items()):
            raise ValueError("compression pending decision 与当前执行身份不一致")
        view = await self._reader.read(session, revision.ref, "execution")
        return loop, grant, pending, revision, tuple(view.messages), policy

    async def _record_failed_attempt(self, loop_id: str, input_tokens: int, output_tokens: int) -> None:
        async with self._sessions.begin() as session:
            usage = await session.get(LoopBudgetUsage, loop_id, with_for_update=True)
            if usage is not None:
                usage.model_calls += 1
                usage.input_tokens += max(0, input_tokens)
                usage.output_tokens += max(0, output_tokens)
                usage.retries += 1

    @staticmethod
    def _require_candidate_budget(
        usage: LoopBudgetUsage,
        budgets: dict[str, Any],
        input_tokens: int,
        output_tokens: int = 0,
    ) -> None:
        if usage.model_calls >= int(budgets.get("max_model_calls", 0)):
            raise ValueError("Loop model call budget 已耗尽")
        if usage.input_tokens + input_tokens > int(budgets.get("max_input_tokens", 0)):
            raise ValueError("Loop input token budget 不足以准备压缩候选")
        if usage.output_tokens + output_tokens > int(budgets.get("max_output_tokens", 0)):
            raise ValueError("Loop output token budget 不足以接受压缩候选")
        if usage.retries > 0 and usage.retries >= int(budgets.get("max_retries", 0)):
            raise ValueError("Loop candidate preparation attempt budget 已耗尽")

    @staticmethod
    def _protected_message_ids(pending: LoopPendingDecision | None) -> tuple[str, ...]:
        message_id = str((pending.payload or {}).get("origin_message_id") or "") if pending else ""
        return (message_id,) if message_id else ()
