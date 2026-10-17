"""已发布 Revision 的低优先级局部准备；只写局部缓存，共用真实授权与消费账本。

监督器按活动 Loop 轮询，缓存可跨重启恢复。失败版本在本进程不自动反复消费；
新 Revision、授权或模型合同可再次准备。整体解释始终由当前冻结 Observation 构建。
"""

import asyncio
import logging

from sqlalchemy import select

from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership
from backend.app.desktop.agent_loop.usage import LoopUsageDelta
from backend.app.desktop.context_evolution.models import ContextPublicationReceipt, ContextRevision

from .contracts import stable_expansion_hash
from .index_budget_repository import IndexBudgetReservationRepository
from .index_model_budget import IndexBudgetExceeded

_logger = logging.getLogger(__name__)


async def validate_prewarm_owner(session, loop, grant, owner):
    context_id = owner.get("context_id")
    member = await session.scalar(select(LoopContextMembership).where(
        LoopContextMembership.loop_id == loop.loop_id,
        LoopContextMembership.context_id == context_id,
        LoopContextMembership.status == "active",
    ))
    revision = await session.get(ContextRevision, owner.get("owner_id"))
    publication = await session.get(ContextPublicationReceipt, owner.get("owner_id"))
    if (member is None or (grant.context_scope and context_id not in grant.context_scope)
        or revision is None or revision.context_id != context_id or publication is None
        or publication.context_id != context_id or revision.projection_status != "valid"
        or revision.deleted_at is not None or revision.content_hash != owner.get("content_hash")):
        raise IndexBudgetExceeded("index prewarm source 未发布、已失效或超出授权 scope")


class PrewarmUsage:
    def __init__(self, sessions, loop_id, authority_revision, limits, usage, owner):
        self._args = (sessions, loop_id, authority_revision, limits, usage)
        self._owner = owner

    async def reserve(self, input_tokens, output_tokens, *, retry=False):
        receipt = IndexBudgetReservationRepository(*self._args, owner=self._owner)
        await receipt.reserve(input_tokens, output_tokens, retry=retry)
        return receipt

    @staticmethod
    async def settle(receipt, measured, reported):
        if reported:
            await receipt.settle(LoopUsageDelta.from_model_usage(measured))
        else:
            await receipt.mark_unknown()


class LocalIndexPrewarmer:
    def __init__(self, sessions, index_service):
        self._sessions = sessions
        self._service = index_service
        self._seen = {}
        self._lock = asyncio.Lock()

    async def drain(self, loop_id):
        if self._service is None or self._service._semantic_projector_factory is None:
            return 0
        # A single background Revision at a time across Loops in this service.
        async with self._lock:
            if self._service._inflight_builds:
                return 0
            async with self._sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                if loop is None or loop.status != "running":
                    self._seen.pop(loop_id, None)
                    return 0
                authority = loop.authority_revision
                members = tuple(await session.scalars(select(LoopContextMembership).where(
                    LoopContextMembership.loop_id == loop_id, LoopContextMembership.status == "active",
                ).order_by(LoopContextMembership.context_id)))
                revisions = [await self._service._revision_repository.current(session, m.context_id) for m in members]
            seen = self._seen.setdefault(loop_id, {})
            active_contexts = {m.context_id for m in members}
            for context_id in tuple(seen):
                if context_id not in active_contexts:
                    del seen[context_id]
            for revision in revisions:
                if revision is None:
                    continue
                key = stable_expansion_hash(
                    revision.ref.model_dump(mode="json"), revision.content_hash, authority,
                    self._service._indexer.segmenter_version,
                    self._service._model_identity(self._service._semantic_projector_factory),
                    self._service._model_identity(self._service._semantic_claim_verifier_factory),
                )
                if seen.get(revision.ref.context_id) == key:
                    continue
                try:
                    finished = await self._service.prewarm_revision(loop_id, authority, revision)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Foreground indexing still retries through its explicit recovery path.
                    seen[revision.ref.context_id] = key
                    _logger.exception("semantic_index_prewarm failed: revision=%s", revision.ref.revision_id)
                    return 0
                if finished:
                    seen[revision.ref.context_id] = key
                return int(finished)
            return 0
