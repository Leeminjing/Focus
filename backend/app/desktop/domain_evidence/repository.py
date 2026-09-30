"""本文件对外提供 DomainResultRepository.record，不可变地保存类型化领域结果。

输入为调用方事务、kind/id、领域 payload 与独立审计定位；输出为稳定 result_key。
具体工作流为规范内容哈希，按 kind/id/version 幂等插入；不消费 Fact 或 Progress 状态，不提交调用方事务。
Loop 关联仅绑定确实存在的实体；旧 Run 的悬空 Loop 标识保留在审计，领域结果不因该关联缺失而阻止结算。
示例：await repository.record(session, kind="test", source_id="test-id", payload=result, run_id="r", ...)。
"""

from __future__ import annotations

from sqlalchemy import String, column, select, table
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.domain_evidence.identity import canonical_hash, result_identity
from backend.app.desktop.domain_evidence.models import DesktopDomainResult


class DomainResultRepository:
    async def record(
        self,
        session: AsyncSession,
        *,
        kind: str,
        source_id: str,
        payload: dict,
        loop_id: str | None,
        context_id: str | None,
        run_id: str | None = None,
        audit: dict | None = None,
    ) -> str:
        if kind not in {
            "run_outcome",
            "test",
            "workspace",
            "artifact",
            "user_revision",
        }:
            raise ValueError("不支持的领域结果类型")
        version = canonical_hash(payload)
        identity = result_identity(kind, source_id, version)
        loops = table("agent_loops", column("loop_id", String(32)))
        loop_ref = (
            select(loops.c.loop_id).where(loops.c.loop_id == loop_id).scalar_subquery()
            if loop_id is not None
            else None
        )
        await session.execute(
            insert(DesktopDomainResult)
            .values(
                result_key=identity,
                loop_id=loop_ref,
                kind=kind,
                source_id=source_id,
                version=version,
                context_id=context_id,
                run_id=run_id,
                payload=payload,
                audit={**(audit or {}), "declared_loop_id": loop_id},
            )
            .on_conflict_do_nothing(index_elements=["kind", "source_id", "version"])
        )
        return identity
