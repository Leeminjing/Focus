"""本文件对外提供 TaskProgressQuery 的只读长期记忆诊断。

输入为 Desktop 会话中的 Loop identity 和可选精确 progress_id；输出为当前及选定历史版本、最近贡献修正链、冻结 refs/hash 和工作 readiness/用量。
具体工作流为读取独立记忆表，隐藏原始审计内容；没有拓扑、任务或执行写入口。
示例：await TaskProgressQuery().read(session, loop_id)。
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy, LoopLiveRedactionPolicy
from backend.app.desktop.agent_loop.task_progress.models import (
    LoopDecisionInputs,
    LoopProgressWork,
    LoopTaskProgress,
)
from backend.app.desktop.agent_loop.task_progress.repository import (
    TaskProgressRepository,
)


class TaskProgressQuery:
    async def read(self, session: AsyncSession, loop_id: str, progress_id: str | None = None) -> dict:
        if await session.get(AgentLoop, loop_id) is None:
            raise HTTPException(404, "Agent Loop 不存在")
        current = await TaskProgressRepository().current(session, loop_id)
        selected = await session.get(LoopTaskProgress, progress_id) if progress_id else current
        if progress_id and (selected is None or selected.loop_id != loop_id):
            raise HTTPException(404, "Task Progress 不属于此 Loop")
        access = await LoopLiveAccessPolicy().resolve(session, loop_id)
        rows = (
            await session.execute(
                select(LoopProgressWork, LoopDecisionInputs)
                .join(
                    LoopDecisionInputs,
                    LoopDecisionInputs.observation_id
                    == LoopProgressWork.observation_id,
                )
                .where(LoopProgressWork.loop_id == loop_id)
                .order_by(LoopProgressWork.created_at.desc())
                .limit(32)
            )
        ).all()
        history = tuple(
            (
                await session.scalars(
                    select(LoopTaskProgress)
                    .where(LoopTaskProgress.loop_id == loop_id)
                    .order_by(LoopTaskProgress.generation.desc())
                    .limit(33)
                )
            ).all()
        )
        result = {
            "loop_id": loop_id,
            "current": None
            if current is None
            else {
                "progress_id": current.progress_id,
                "generation": current.generation,
                "content_hash": current.content_hash,
                "previous_progress_id": current.previous_progress_id,
                "document": current.document,
            },
            "work": [self._work_view(work, frozen) for work, frozen in rows],
            "history_has_more": len(history) > 32,
            "history": [
                {
                    "progress_id": version.progress_id,
                    "generation": version.generation,
                    "previous_progress_id": version.previous_progress_id,
                    "observation_id": version.observation_id,
                    "content_hash": version.content_hash,
                    "contribution": version.contribution,
                }
                for version in history[:32]
            ],
        }
        if selected is not None:
            result["selected"] = {
                "progress_id": selected.progress_id, "generation": selected.generation,
                "content_hash": selected.content_hash, "previous_progress_id": selected.previous_progress_id,
                "document": selected.document, "observation_id": selected.observation_id,
            }
        return LoopLiveRedactionPolicy.redact_value(result, access.permissions)

    @staticmethod
    def _work_view(work: LoopProgressWork, frozen: LoopDecisionInputs) -> dict:
        payload = frozen.payload
        return {
            "observation_id": work.observation_id,
            "round_id": frozen.round_id,
            "state": work.state,
            "attempts": work.attempts,
            "fence": work.fence,
            "lease_expires_at": work.lease_expires_at.isoformat()
            if work.lease_expires_at
            else None,
            "failure_kind": work.error.split(":", 1)[0] if work.error else None,
            "usage": work.usage,
            "retry_budget_authorization": work.retry_budget_authorization,
            "attempt_events": work.attempt_events,
            "inputs_hash": frozen.content_hash,
            "observation_hash": payload["observation_hash"],
            "previous_progress_id": payload["previous_progress_id"],
            "previous_progress_hash": payload["previous_progress_hash"],
            "manifest_hash": payload["manifest_hash"],
            "manifest_complete": payload["task_delta"]["complete"],
            "topology_hash": payload["topology_hash"],
            "source_refs": [
                {
                    key: source.get(key)
                    for key in (
                        "source_key",
                        "kind",
                        "source_id",
                        "version",
                        "run_id",
                        "execution_round_id",
                    )
                }
                for source in payload["task_delta"]["sources"]
            ],
        }
