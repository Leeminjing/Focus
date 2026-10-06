"""本文件对外提供 CompletionEligibilityPolicy 与 CompletionEligibilityRejected。

输入为当前事务中的 Loop/Round、持久独立 verification 及完成动作身份；输出为版本化只读资格目录或安全拒绝码。
具体工作流为读取当前授权、后果、publication/adoption，复用 CompletionGuard、检查合同和真实来源校验；
Observation 冻结目录，候选精确选择身份，Stage/Kernel 在各自事务重新读取。政策不提交、不替换动作、不改写历史。
全局消息/控制阻断不依赖 verification 是否存在；动作输入使用已解析的完整 RequestCompletionAction。
完成资格复检当前可信完整产物正文；新读面不向旧验证补写正文或 satisfied。
示例：view = await policy.read(session, loop, round_row)；policy.require_allowed(view, action)。
"""

from datetime import UTC, datetime

from sqlalchemy import func, select

from backend.app.desktop.agent_loop.completion import CompletionGuard
from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.completion_sources import CompletionSourceValidator, CompletionSourceRejection
from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import (CompletionVerification, LoopContextMembership,
    LoopDelegationGrant, LoopDirective, LoopGoalRevision, LoopObservation, LoopPendingDecision, LoopUserIntent)
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract
from backend.app.desktop.context_curation.models import CurationProgram, PortfolioLaneCandidate, PortfolioRevision
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceSlot


class CompletionEligibilityRejected(ValueError):
    def __init__(self, reasons, eligible_ids=()):
        self.reasons = tuple(reasons)
        super().__init__('completion_ineligible: ' + ','.join(self.reasons) + '; eligible_verification_ids=' + ','.join(eligible_ids))


class CompletionEligibilityPolicy:
    VERSION = 'completion-eligibility-v1'

    async def read(self, session, loop, round_row):
        facts = await self._facts(session, loop, round_row)
        checks = await self._checks(session, loop)
        rows = tuple(await session.scalars(select(CompletionVerification).where(
            CompletionVerification.loop_id == loop.loop_id).order_by(
            CompletionVerification.created_at.desc()).execution_options(populate_existing=True)))
        entries = [await self._entry(session, row, checks, facts) for row in rows]
        return {'contract': self.VERSION, 'expected': facts['expected'],
            'final_slot_id': facts['final_slot_id'], 'final_context_ids': facts['final_context_ids'],
            'blockers': facts['blockers'], 'verifications': entries}

    @classmethod
    def require_allowed(cls, view, action):
        if view is None:
            return
        if view.get('contract') != cls.VERSION:
            raise CompletionEligibilityRejected(('eligibility_contract_unknown',))
        entry = next((item for item in view['verifications'] if item['verification_id'] == action.verification_id), None)
        reasons = [*view['blockers'], *(entry['reasons'] if entry is not None else ['verification_missing'])]
        if action.final_slot_id != view['final_slot_id']:
            reasons.append('workspace_not_adopted')
        if not set(action.final_context_ids).issubset(view['final_context_ids']):
            reasons.append('final_context_not_published')
        if reasons:
            raise CompletionEligibilityRejected(dict.fromkeys(reasons),
                tuple(item['verification_id'] for item in view['verifications'] if item['allowed']))

    async def require_current(self, session, loop, round_row, action):
        self.require_allowed(await self.read(session, loop, round_row), action)

    async def _entry(self, session, row, checks, facts):
        reasons = list(facts['blockers'])
        try:
            contract = CompletionVerificationContract(verification_id=row.verification_id, loop_id=row.loop_id,
                round_id=row.round_id, goal_revision=row.goal_revision, frontier_hash=row.frontier_hash,
                workspace_revision=row.workspace_revision, criteria=tuple(row.criteria),
                conclusion=row.conclusion, unresolved=tuple(row.unresolved))
        except ValueError:
            reasons.append('verification_contract_invalid')
        else:
            guard = CompletionGuard().check(contract, **facts['expected'], active_or_queued_runs=facts['runs'],
                pending_human_gates=facts['pending'], portfolio_published=facts['published'],
                workspace_adopted=facts['adopted'], grant_allows_completion=facts['authorized'])
            reasons.extend(guard.reasons)
            required = {str(check['check_id']) for check in checks if check.get('required', True)}
            satisfied = {item.check_id for item in contract.criteria if item.status == 'satisfied'}
            if not required or not required.issubset(satisfied):
                reasons.append('required_checks_missing')
            try:
                CompletionCheckPolicy().validate(checks, contract.criteria)
            except ValueError:
                reasons.append('verification_checks_invalid')
            if not reasons:
                try:
                    await CompletionSourceValidator().validate(session, checks, contract, require_artifact_content=True)
                except CompletionSourceRejection as exc:
                    reasons.append(exc.code)
                except ValueError:
                    reasons.append('required_evidence_missing')
        return {'verification_id': row.verification_id, 'round_id': row.round_id,
            'goal_revision': row.goal_revision, 'frontier_hash': row.frontier_hash,
            'workspace_revision': row.workspace_revision, 'conclusion': row.conclusion,
            'allowed': not reasons, 'reasons': list(dict.fromkeys(reasons))}

    @staticmethod
    async def _checks(session, loop):
        mission = await session.scalar(select(LoopMissionRevision).where(
            LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        if mission is not None:
            return mission.completion_checks
        goal = await session.scalar(select(LoopGoalRevision).where(
            LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
        if goal is None:
            return []
        return [item.model_dump(mode='json') for item in LegacyMissionAdapter.convert(
            goal=goal.goal, task_contract=goal.task_contract, acceptance_criteria=goal.acceptance_criteria).completion_checks]

    async def _facts(self, session, loop, round_row):
        slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id,
            WorkspaceSlot.kind == 'authoritative', WorkspaceSlot.lifecycle != 'deleted').execution_options(populate_existing=True))
        program = await session.get(CurationProgram, loop.program_id, populate_existing=True)
        portfolio = await session.get(PortfolioRevision, loop.current_portfolio_revision_id, populate_existing=True) if loop.current_portfolio_revision_id else None
        published = bool(program and portfolio and program.current_portfolio_revision_id == portfolio.portfolio_revision_id and portfolio.status == 'published')
        memberships = set(await session.scalars(select(LoopContextMembership.context_id).where(
            LoopContextMembership.loop_id == loop.loop_id, LoopContextMembership.status == 'active')))
        contexts = set(await session.scalars(select(PortfolioLaneCandidate.target_context_id).where(
            PortfolioLaneCandidate.portfolio_revision_id == portfolio.portfolio_revision_id,
            PortfolioLaneCandidate.status.in_(['prepared', 'unchanged', 'published'])))) if published else set()
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id,
            LoopDelegationGrant.status == 'active', LoopDelegationGrant.revision == loop.authority_revision))
        authorized = bool(grant and 'request_completion' in grant.capabilities and
            (grant.expires_at is None or grant.expires_at > datetime.now(UTC)))
        blockers = []
        if (loop.status != 'running' or loop.current_round_id != round_row.round_id
            or loop.authority_revision != round_row.authority_revision or loop.goal_revision != round_row.goal_revision):
            blockers.append('completion_control_changed')
        if await self._frontier_changed(session, round_row, memberships):
            blockers.append('verification_stale')
        if await self._count(session, LoopUserIntent, LoopUserIntent.loop_id == loop.loop_id,
            LoopUserIntent.intent_kind == 'direct_message', LoopUserIntent.delivery_state.not_in(
                ('settled', 'failed', 'cancelled', 'rejected', 'delivery_failed'))):
            blockers.append('user_message_pending')
        runs = await self._count(session, DesktopRun, DesktopRun.loop_id == loop.loop_id,
            (DesktopRun.status.in_(['pending', 'running'])) | ((DesktopRun.round_id.is_not(None)) & DesktopRun.settled_at.is_(None)))
        runs += await self._count(session, LoopDirective, LoopDirective.loop_id == loop.loop_id,
            LoopDirective.status.in_(['created', 'launching', 'blocked']))
        unadopted = await session.scalar(select(func.count()).select_from(RunExecutionAnchor).join(
            DesktopRun, DesktopRun.run_id == RunExecutionAnchor.run_id).where(DesktopRun.loop_id == loop.loop_id,
            RunExecutionAnchor.adoption_state.in_(['pending', 'conflict', 'required'])))
        pending = await self._count(session, LoopPendingDecision, LoopPendingDecision.loop_id == loop.loop_id,
            LoopPendingDecision.status == 'pending')
        return {'expected': {'goal_revision': loop.goal_revision, 'frontier_hash': round_row.frontier_hash,
                'workspace_revision': slot.revision if slot else round_row.workspace_revision},
            'final_slot_id': slot.slot_id if slot else None, 'final_context_ids': sorted(contexts & memberships),
            'blockers': blockers, 'runs': runs, 'pending': pending, 'published': published,
            'adopted': bool(slot and slot.revision == round_row.workspace_revision and not unadopted),
            'authorized': authorized}

    @staticmethod
    async def _frontier_changed(session, round_row, memberships):
        frozen = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == round_row.round_id))
        if frozen is None:
            return False
        expected = {item['context_id']: str(item.get('revision_id') or '')
            for item in frozen.envelope.get('portfolio_frontier', ())}
        if set(expected) != memberships:
            return True
        for context_id, revision in expected.items():
            context = await session.get(DesktopThread, context_id, populate_existing=True)
            if context is None or str(context.current_revision_id or '') != revision:
                return True
        return False

    @staticmethod
    async def _count(session, model, *conditions):
        return int(await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0)
