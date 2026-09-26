r"""本文件对外提供 ContextRecoveryOpportunityService，负责发现和持久化可安全执行的单来源恢复机会。

输入为冻结 frontier、已结算 Run、Loop/grant/workspace authority 与来源 authored view；输出为当前 round 的公开 opportunity 列表和
无法自动恢复时的等待原因。具体工作流为只检查最新失败 Run 对应的权威来源，要求共享协议编译定位无结果的终端工具交换，再由专用
compiler 生成 evidence-preserving plan 并持久化；非终端或部分结果等歧义损坏只返回等待原因。示例：`result = await service.discover(session, ...)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.context_recovery.compiler import ContextRecoveryPlanCompiler
from backend.app.desktop.agent_loop.context_recovery.contracts import ContextRecoveryOpportunityContract
from backend.app.desktop.agent_loop.context_recovery.repository import ContextRecoveryOpportunityRepository
from backend.app.desktop.context_evolution import ContextRevisionRef
from backend.app.desktop.context_protocol import compile_protocol_messages


@dataclass(frozen=True, slots=True)
class ContextRecoveryDiscovery:
    opportunities: tuple[dict[str, Any], ...]
    waiting_reason: str | None = None


class ContextRecoveryOpportunityService:
    def __init__(self, reader) -> None:
        self._reader = reader
        self._compiler = ContextRecoveryPlanCompiler()
        self._repository = ContextRecoveryOpportunityRepository()

    async def discover(
        self,
        session: AsyncSession,
        *,
        loop,
        round_row,
        grant,
        workspace_revision: int,
        frontier: tuple[dict[str, Any], ...],
        runs: tuple[Any, ...],
    ) -> ContextRecoveryDiscovery:
        existing = await self._repository.pending_for_round(session, round_row.round_id)
        if existing:
            return ContextRecoveryDiscovery(existing)
        by_context = {
            str(item.get("context_id")): item
            for item in frontier
            if item.get("revision")
        }
        failed = next(
            (
                run
                for run in runs
                if run.status in {"error", "cancelled", "interrupted"}
                and run.task_id in by_context
            ),
            None,
        )
        if failed is None:
            ambiguous = next(
                (item for item in frontier if item.get("projection_status") == "approval_required"),
                None,
            )
            reason = None
            if ambiguous is not None:
                reason = (
                    "Context 协议历史需要人工批准：缺少可证明的中断因果，"
                    f"source_revision={ambiguous.get('revision_id')}"
                )
            return ContextRecoveryDiscovery((), reason)
        source_item = by_context[str(failed.task_id)]
        source = ContextRevisionRef.model_validate(source_item["revision"])
        authored = await self._reader.read(session, source, "authored")
        projection = compile_protocol_messages(list(authored.messages))
        terminal_issues = bool(projection.issues) and all(
            issue.get("kind") == "unresolved_tool_call"
            and issue.get("before_index") == len(authored.messages)
            for issue in projection.issues
        )
        if projection.status != "approval_required" or not terminal_issues:
            if projection.status == "approval_required":
                return ContextRecoveryDiscovery(
                    (),
                    "Context 恢复需要人工批准：共享协议编译器无法同时保持证据和合法消息序列",
                )
            return ContextRecoveryDiscovery(())
        try:
            plan = self._compiler.compile(
                source,
                tuple(authored.messages),
                run_id=failed.run_id,
                reason=str(failed.error or failed.status),
            )
        except ValueError as exc:
            return ContextRecoveryDiscovery((), f"Context 恢复需要人工批准：{exc}")
        opportunity = ContextRecoveryOpportunityContract.create(
            loop_id=loop.loop_id,
            source=source,
            source_frontier_hash=round_row.frontier_hash,
            goal_revision=loop.goal_revision,
            workspace_revision=workspace_revision,
            authority_revision=loop.authority_revision,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            source_run_id=failed.run_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            safe_summary=(
                f"从 Context {source.context_id} revision {source.revision_id} 隔离无结果的终端工具交换，"
                "保留此前约束和可追溯证据；工具副作用未知。"
            ),
            plan=plan,
        )
        try:
            row = await self._repository.put(session, opportunity, round_row.round_id)
        except ValueError as exc:
            return ContextRecoveryDiscovery((), f"Context 恢复需要人工批准：{exc}")
        if row.status != "pending":
            return ContextRecoveryDiscovery((), "Context recovery opportunity 已不再待决")
        expires_at = row.expires_at if row.expires_at.tzinfo is not None else row.expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            row.status = "stale"
            row.payload = {**row.payload, "status": "stale"}
            return ContextRecoveryDiscovery(
                (),
                "Context recovery opportunity 已过期，需要新的权威 Run 或用户批准后才能继续",
            )
        persisted = ContextRecoveryOpportunityContract.model_validate(row.payload)
        return ContextRecoveryDiscovery((persisted.public_payload(),))
