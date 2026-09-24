r"""本文件对外提供 PersistedWorkSpecReconciliationStage。

输入为 Loop/round identity、冻结 WorkContextSpec candidate set、required candidate identities、relation evaluator 与 deterministic
reconciler；输出为可恢复 WorkSpecReconciliation。具体工作流为按顺序无关 candidate identity 集合查询 stage artifact，命中时严格
反序列化复用；未命中时调用受监督 relation evaluator、累计其 attempt/model usage、执行保守 canonicalization 并立即持久化 immutable
result，使进程重启不再重新判定同一冻结输入。示例：`result = await stage.reconcile(loop_id, round_id, specs, required)`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import WorkContextSpec
from backend.app.desktop.agent_loop.context_expansion.reconciliation import (
    WorkSpecReconciler,
    WorkSpecReconciliation,
    WorkSpecRelationEvaluatorPort,
)
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger


class PersistedWorkSpecReconciliationStage:
    VERSION = WorkSpecReconciler.VERSION

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        evaluator: WorkSpecRelationEvaluatorPort,
        reconciler: WorkSpecReconciler | None = None,
    ) -> None:
        self._sessions = sessions
        self._evaluator = evaluator
        self._reconciler = reconciler or WorkSpecReconciler()
        self._artifacts = SemanticDerivationArtifactRepository()

    async def reconcile(
        self,
        loop_id: str,
        round_id: str,
        candidates: tuple[WorkContextSpec, ...],
        required_candidate_ids: tuple[str, ...],
    ) -> WorkSpecReconciliation:
        inputs = self._input_identities(candidates, required_candidate_ids)
        async with self._sessions() as session:
            row = await self._artifacts.stage_artifact(
                session,
                loop_id=loop_id,
                round_id=round_id,
                stage="work_reconciliation",
                input_identities=inputs,
                version=self.VERSION,
            )
            if row is not None and row.outcome == "ready":
                return WorkSpecReconciliation.model_validate(row.payload)
        relations = await self._evaluator.evaluate(candidates)
        attempts = tuple(getattr(self._evaluator, "attempt_records", ()))
        await self._record_attempt_usage(loop_id, attempts)
        result = self._reconciler.reconcile(
            candidates,
            relations=relations,
            required_candidate_ids=required_candidate_ids,
        )
        async with self._sessions.begin() as session:
            await self._artifacts.put_stage_artifact(
                session,
                loop_id=loop_id,
                round_id=round_id,
                stage="work_reconciliation",
                input_identities=inputs,
                version=self.VERSION,
                outcome="ready",
                payload=result.model_dump(mode="json"),
                attempt_records=attempts
                or ({"role": "work_spec_reconciler", "outcome": "ready"},),
            )
        return result

    async def _record_attempt_usage(
        self,
        loop_id: str,
        attempts: tuple[dict, ...],
    ) -> None:
        if not attempts:
            return
        await LoopUsageLedger(self._sessions).record(
            loop_id,
            LoopUsageDelta(
                model_calls=sum(max(0, int(item.get("model_calls") or 0)) for item in attempts),
                input_tokens=sum(max(0, int(item.get("input_tokens") or 0)) for item in attempts),
                output_tokens=sum(max(0, int(item.get("output_tokens") or 0)) for item in attempts),
                retries=sum(1 for item in attempts if int(item.get("attempt") or 1) > 1),
            ),
        )

    @staticmethod
    def _input_identities(
        candidates: tuple[WorkContextSpec, ...],
        required_candidate_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        candidates_by_id = {item.work_spec_id for item in candidates}
        required = set(required_candidate_ids)
        if not required.issubset(candidates_by_id):
            raise ValueError("reconciliation required candidate identity 未知")
        return tuple(
            sorted(
                (*candidates_by_id, *(f"required:{identity}" for identity in required))
            )
        )
