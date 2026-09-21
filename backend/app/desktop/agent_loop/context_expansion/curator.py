r"""本文件对外提供 ExpansionCuratorPort、NoopExpansionCurator 与 WorkerResultExpansionCurator。

输入为冻结 Loop observation 与确定性 detector 候选；输出为无权威 CuratorExpansionProposal 集合。具体工作流为
coordinator 依赖该 port 获取模型辅助语义拆分，测试或禁用模式使用空 adapter，Worker adapter 只解析已冻结结果；任何实现都不接收数据库提交端口。
示例：`proposals = await curator.propose(observation, opportunities)`。
"""

from __future__ import annotations

from typing import Protocol

from backend.app.desktop.agent_loop.context_expansion.contracts import CuratorExpansionProposal, ExpansionOpportunity
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope


class ExpansionCuratorPort(Protocol):
    async def propose(
        self,
        observation: LoopObservationEnvelope,
        opportunities: tuple[ExpansionOpportunity, ...],
    ) -> tuple[CuratorExpansionProposal, ...]: ...


class NoopExpansionCurator:
    async def propose(
        self,
        observation: LoopObservationEnvelope,
        opportunities: tuple[ExpansionOpportunity, ...],
    ) -> tuple[CuratorExpansionProposal, ...]:
        return ()


class WorkerResultExpansionCurator:
    async def propose(
        self,
        observation: LoopObservationEnvelope,
        opportunities: tuple[ExpansionOpportunity, ...],
    ) -> tuple[CuratorExpansionProposal, ...]:
        proposals: list[CuratorExpansionProposal] = []
        for worker in observation.worker_results:
            if worker.get("kind") != "lane_curator" or worker.get("status") not in {"proposed", "consumed", "success"}:
                continue
            result = worker.get("result") or {}
            for payload in result.get("proposals") or ():
                proposals.append(CuratorExpansionProposal.model_validate(payload))
        return tuple(proposals)
