r"""本文件对外提供 SingleLaneCurationProgramService 与稳定 Program/Lane identity。

输入为现有一对一 Context Curator binding、目标控制状态或待清理 Context identities；输出为通用
Curation Program、来源订阅与单个稳定 Lane。具体工作流为仅在兼容 API 边界创建或更新 Program
拓扑，发布历史统一交给 PortfolioFreezer、PortfolioCandidatePreparer 和 AtomicPortfolioPublisher，
不再从旧 revision/attempt 表镜像 Portfolio。示例：`ids = await service.provision(session, binding)`。
"""

from __future__ import annotations

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_curation.models import (
    CurationLane,
    CurationProgram,
    CurationSourceSubscription,
)
from backend.app.desktop.models import DesktopThread, PatrolAgent, PatrolContextBinding


class SingleLaneCurationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    program_id: str
    subscription_id: str
    lane_id: str


class SingleLaneCurationProgramService:
    async def provision(
        self,
        session: AsyncSession,
        binding: PatrolContextBinding,
    ) -> SingleLaneCurationIdentity:
        await session.flush()
        ids = self.identities(binding.binding_id)
        root = await session.get(DesktopThread, binding.root_context_id)
        agent = await session.get(PatrolAgent, binding.agent_id)
        if root is None or agent is None:
            raise ValueError("Context Curator binding 缺少 root 或 Patrol identity")
        program = await self._program(session, ids, binding, root, agent)
        await self._subscription(session, ids, program, binding)
        await self._lane(session, ids, program, binding)
        await session.flush()
        return ids

    async def set_control_state(
        self,
        session: AsyncSession,
        binding: PatrolContextBinding,
        target: Literal["following", "paused", "stopped"],
    ) -> None:
        ids = await self.provision(session, binding)
        program = await session.get(CurationProgram, ids.program_id)
        lane = await session.get(CurationLane, ids.lane_id)
        if program is None or lane is None:
            raise ValueError("单 Lane Curation Program 不完整")
        lifecycle = self._lifecycle(target)
        changed = program.control_state != target or lane.lifecycle != lifecycle
        program.control_state = target
        if lane.lifecycle != lifecycle:
            lane.lifecycle = lifecycle
            lane.publisher_epoch += 1
        if changed:
            program.revision += 1

    async def attach_managed_context(
        self,
        session: AsyncSession,
        binding: PatrolContextBinding,
        context_id: str,
    ) -> None:
        ids = await self.provision(session, binding)
        lane = await session.get(CurationLane, ids.lane_id)
        if lane is None:
            raise ValueError("单 Lane Curation Lane 不存在")
        if lane.managed_context_id not in {None, context_id}:
            raise ValueError("单 Lane Curation Lane 已绑定其他 Context")
        lane.managed_context_id = context_id

    async def delete_for_contexts(
        self,
        session: AsyncSession,
        context_ids: list[str],
    ) -> int:
        if not context_ids:
            return 0
        source_programs = select(CurationSourceSubscription.program_id).where(
            CurationSourceSubscription.source_context_id.in_(context_ids)
        )
        lane_programs = select(CurationLane.program_id).where(
            CurationLane.managed_context_id.in_(context_ids)
        )
        result = await session.execute(
            delete(CurationProgram).where(
                or_(
                    CurationProgram.program_id.in_(source_programs),
                    CurationProgram.program_id.in_(lane_programs),
                )
            )
        )
        await session.flush()
        return int(result.rowcount or 0)

    @classmethod
    def identities(cls, binding_id: str) -> SingleLaneCurationIdentity:
        return SingleLaneCurationIdentity(
            program_id=cls._id("program", binding_id),
            subscription_id=cls._id("subscription", binding_id),
            lane_id=cls._id("lane", binding_id),
        )

    async def _program(
        self,
        session: AsyncSession,
        ids: SingleLaneCurationIdentity,
        binding: PatrolContextBinding,
        root: DesktopThread,
        agent: PatrolAgent,
    ) -> CurationProgram:
        program = await session.get(CurationProgram, ids.program_id)
        if program is None:
            program = CurationProgram(
                program_id=ids.program_id,
                workspace_id=root.workspace_id,
                patrol_id=binding.agent_id,
                control_state=binding.control_state,
                policy=agent.curation_policy or {},
                revision=0,
            )
            session.add(program)
            await session.flush()
        return program

    async def _subscription(
        self,
        session: AsyncSession,
        ids: SingleLaneCurationIdentity,
        program: CurationProgram,
        binding: PatrolContextBinding,
    ) -> None:
        subscription = await session.get(CurationSourceSubscription, ids.subscription_id)
        if subscription is None:
            session.add(
                CurationSourceSubscription(
                    subscription_id=ids.subscription_id,
                    program_id=program.program_id,
                    source_context_id=binding.root_context_id,
                    source_role="root",
                    selection_policy=program.policy,
                    position=0,
                )
            )

    async def _lane(
        self,
        session: AsyncSession,
        ids: SingleLaneCurationIdentity,
        program: CurationProgram,
        binding: PatrolContextBinding,
    ) -> None:
        lane = await session.get(CurationLane, ids.lane_id)
        if lane is None:
            session.add(
                CurationLane(
                    lane_id=ids.lane_id,
                    program_id=program.program_id,
                    managed_context_id=binding.managed_context_id,
                    purpose="Continuously curated context",
                    normalized_purpose="continuously curated context",
                    lane_policy=program.policy,
                    lifecycle=self._lifecycle(binding.control_state),
                    publisher_epoch=1,
                )
            )

    @staticmethod
    def _lifecycle(state: str) -> str:
        return {
            "following": "active",
            "paused": "paused",
            "stopped": "retired",
        }[state]

    @staticmethod
    def _id(kind: str, source_id: str) -> str:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"focus:legacy-curation:{kind}:{source_id}",
        ).hex
