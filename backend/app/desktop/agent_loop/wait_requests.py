r"""本文件对外提供 WaitRequestDraft、LoopWaitRequestFactory 与 LoopWaitRequestService。

输入为等待场景、Loop 行、类型化响应和幂等标识；输出为规范化的等待请求、唯一响应与受约束的 Loop 状态转换。
具体工作流为 factory 只声明交互策略，service 在锁定 Loop/请求后原子执行 open、resolve、cancel 或 supersede，
所有 JSON/text 写入先经过统一持久化安全边界。示例：`draft = LoopWaitRequestFactory.clarification("请选择目标")`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.persistence_safety import PersistencePayloadNormalizer
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse


@dataclass(frozen=True, slots=True)
class WaitRequestDraft:
    kind: str
    prompt: str
    response_mode: str
    response_contract: dict[str, Any]
    scope: dict[str, Any] = field(default_factory=dict)


class LoopWaitRequestFactory:
    @staticmethod
    def clarification(prompt: str, scope: dict[str, Any] | None = None) -> WaitRequestDraft:
        return WaitRequestDraft(
            kind="clarification",
            prompt=prompt,
            response_mode="text",
            response_contract={"min_length": 1, "max_length": 12000},
            scope=scope or {},
        )

    @staticmethod
    def choice(prompt: str, options: list[dict[str, Any]], *, multiple: bool = False) -> WaitRequestDraft:
        return WaitRequestDraft(
            kind="choice",
            prompt=prompt,
            response_mode="multiple_choice" if multiple else "single_choice",
            response_contract={"options": options, "min_items": 1, "max_items": len(options) if multiple else 1},
        )

    @staticmethod
    def budget_action(reason: str, current_budget: dict[str, Any]) -> WaitRequestDraft:
        return WaitRequestDraft(
            kind="budget_action",
            prompt=f"Loop 已达到预算边界：{reason}",
            response_mode="action",
            response_contract={
                "actions": [
                    {"action": "revise_budget", "label": "调整预算", "input_schema": current_budget},
                    {"action": "stop", "label": "停止 Loop"},
                ]
            },
            scope={"reason": reason, "current_budget": current_budget},
        )

    @staticmethod
    def retry_or_stop(reason: str, scope: dict[str, Any] | None = None) -> WaitRequestDraft:
        return WaitRequestDraft(
            kind="recovery_action",
            prompt=reason,
            response_mode="action",
            response_contract={"actions": [{"action": "retry", "label": "重试"}, {"action": "stop", "label": "停止 Loop"}]},
            scope=scope or {},
        )

    @staticmethod
    def legacy_recovery(reason: str) -> WaitRequestDraft:
        draft = LoopWaitRequestFactory.retry_or_stop(reason, {"legacy": True})
        return WaitRequestDraft(
            kind="legacy_recovery",
            prompt=draft.prompt,
            response_mode=draft.response_mode,
            response_contract=draft.response_contract,
            scope=draft.scope,
        )


class WaitRequestConflict(RuntimeError):
    def __init__(self, message: str, committed: LoopWaitResponse | None = None) -> None:
        super().__init__(message)
        self.committed = committed


class LoopWaitRequestService:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def open(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        draft: WaitRequestDraft,
        *,
        created_by: str,
        correlation_id: str,
        causation_id: str | None = None,
        round_id: str | None = None,
    ) -> LoopWaitRequest:
        existing = await self.active(session, loop.loop_id, lock=True)
        if existing is not None:
            return existing
        prompt = PersistencePayloadNormalizer.normalize(draft.prompt, "loop-wait-request.prompt")
        contract = PersistencePayloadNormalizer.normalize(draft.response_contract, "loop-wait-request.contract")
        scope = PersistencePayloadNormalizer.normalize(draft.scope, "loop-wait-request.scope")
        request = LoopWaitRequest(
            request_id=uuid.uuid4().hex,
            loop_id=loop.loop_id,
            round_id=round_id,
            kind=draft.kind,
            prompt=prompt.value,
            response_mode=draft.response_mode,
            response_contract=contract.value,
            scope=scope.value,
            status="open",
            revision=1,
            correlation_id=correlation_id,
            causation_id=causation_id,
            created_by=created_by,
        )
        loop.status = "waiting_user"
        loop.waiting_reason = prompt.value
        loop.revision += 1
        session.add(request)
        await session.flush()
        await self._append_opened(session, request)
        return request

    async def resolve(
        self,
        session: AsyncSession,
        request_id: str,
        *,
        answer: dict[str, Any],
        actor_id: str,
        request_revision: int,
        idempotency_key: str,
    ) -> tuple[LoopWaitRequest, LoopWaitResponse, bool]:
        existing = await session.scalar(select(LoopWaitResponse).where(LoopWaitResponse.idempotency_key == idempotency_key))
        if existing is not None:
            safe_answer = PersistencePayloadNormalizer.normalize(answer, "loop-wait-response.answer").value
            if existing.request_id != request_id or existing.request_revision != request_revision or existing.actor_id != actor_id or existing.answer != safe_answer:
                raise WaitRequestConflict("idempotency key 已用于不同等待响应", existing)
            request = await session.get(LoopWaitRequest, existing.request_id)
            if request is None:
                raise LookupError(existing.request_id)
            return request, existing, False
        request = await session.get(LoopWaitRequest, request_id, with_for_update=True)
        if request is None:
            raise LookupError(request_id)
        committed = await session.scalar(select(LoopWaitResponse).where(LoopWaitResponse.request_id == request_id))
        if committed is not None:
            raise WaitRequestConflict("等待请求已经提交响应", committed)
        if request.status != "open" or request.revision != request_revision:
            raise WaitRequestConflict(f"等待请求状态或版本已变化: {request.status}@{request.revision}")
        safe = PersistencePayloadNormalizer.normalize(answer, "loop-wait-response.answer")
        request.status = "resolving"
        request.revision += 1
        response = LoopWaitResponse(
            response_id=uuid.uuid4().hex,
            request_id=request_id,
            actor_id=actor_id,
            answer=safe.value,
            request_revision=request_revision,
            idempotency_key=idempotency_key,
        )
        request.status = "resolved"
        request.revision += 1
        request.resolved_at = datetime.now(UTC)
        loop = await session.get(AgentLoop, request.loop_id, with_for_update=True)
        if loop is not None and loop.status == "waiting_user":
            loop.status = "running"
            loop.waiting_reason = None
            loop.revision += 1
        session.add(response)
        await session.flush()
        await self._append_resolved(session, request, response)
        return request, response, True

    async def _append_opened(self, session: AsyncSession, request: LoopWaitRequest) -> None:
        await self._journal.append(
            session,
            request.loop_id,
            CanonicalEventDraft(
                kind="loop.wait.request_opened",
                entity_type="loop_wait_request",
                entity_id=request.request_id,
                entity_revision=request.revision,
                correlation_id=request.correlation_id,
                causation_id=request.causation_id,
                payload={"request_id": request.request_id, "kind": request.kind, "prompt": request.prompt, "response_mode": request.response_mode, "response_contract": request.response_contract, "scope": request.scope, "status": request.status, "round_id": request.round_id},
                idempotency_key=f"loop-wait:{request.request_id}:opened",
            ),
        )

    async def _append_resolved(
        self,
        session: AsyncSession,
        request: LoopWaitRequest,
        response: LoopWaitResponse,
    ) -> None:
        await self._journal.append(
            session,
            request.loop_id,
            CanonicalEventDraft(
                kind="loop.wait.response_committed",
                entity_type="loop_wait_response",
                entity_id=response.response_id,
                entity_revision=1,
                correlation_id=request.correlation_id,
                causation_id=request.request_id,
                payload={"request_id": request.request_id, "response_id": response.response_id, "answer": response.answer, "status": "committed"},
                idempotency_key=f"loop-wait-response:{response.response_id}",
            ),
        )
        await self._journal.append(
            session,
            request.loop_id,
            CanonicalEventDraft(
                kind="loop.wait.request_resolved",
                entity_type="loop_wait_request",
                entity_id=request.request_id,
                entity_revision=request.revision,
                correlation_id=request.correlation_id,
                causation_id=response.response_id,
                payload={"request_id": request.request_id, "response_id": response.response_id, "status": request.status},
                idempotency_key=f"loop-wait:{request.request_id}:resolved",
            ),
        )

    async def cancel(self, session: AsyncSession, request_id: str, *, superseded: bool = False) -> LoopWaitRequest:
        request = await session.get(LoopWaitRequest, request_id, with_for_update=True)
        if request is None:
            raise LookupError(request_id)
        if request.status not in {"open", "resolving"}:
            raise WaitRequestConflict(f"等待请求不能从 {request.status} 转换")
        request.status = "superseded" if superseded else "cancelled"
        request.revision += 1
        request.resolved_at = datetime.now(UTC)
        await session.flush()
        await self._journal.append(
            session,
            request.loop_id,
            CanonicalEventDraft(
                kind="loop.wait.request_superseded" if superseded else "loop.wait.request_cancelled",
                entity_type="loop_wait_request",
                entity_id=request.request_id,
                entity_revision=request.revision,
                correlation_id=request.correlation_id,
                causation_id=request.causation_id,
                payload={"request_id": request.request_id, "status": request.status},
                idempotency_key=f"loop-wait:{request.request_id}:{request.status}",
            ),
        )
        return request

    @staticmethod
    async def active(session: AsyncSession, loop_id: str, *, lock: bool = False) -> LoopWaitRequest | None:
        query = select(LoopWaitRequest).where(
            LoopWaitRequest.loop_id == loop_id,
            LoopWaitRequest.status.in_(("open", "resolving")),
        ).order_by(LoopWaitRequest.created_at.desc()).limit(1)
        if lock:
            query = query.with_for_update()
        return await session.scalar(query)


async def open_recovery_wait(
    session: AsyncSession,
    loop: AgentLoop,
    reason: str,
    *,
    source: str,
    round_id: str | None = None,
    correlation_id: str | None = None,
    scope: dict[str, Any] | None = None,
) -> LoopWaitRequest:
    return await LoopWaitRequestService().open(
        session,
        loop,
        LoopWaitRequestFactory.retry_or_stop(reason, scope),
        created_by=source,
        correlation_id=correlation_id or f"{source}:{loop.loop_id}:{loop.revision}",
        round_id=round_id,
    )
