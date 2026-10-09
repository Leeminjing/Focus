"""本文件对外提供 CompletionRequestAdmission 的完成请求事实读取和语义准入政策。

输入为当前 Loop/Round 或已投影的冻结证据目录；输出为不含执行随机身份的语义输入、独立来源追踪及重复验证 blocker。
具体工作流为复用 D8 目录资格判定，按证据内容与可信覆盖去重，再比较已成功保存的验证输入；失败 Worker 不参与。
输入合同保存于现有 Worker scope/Observation JSON，不新增账本或迁移；旧输入仅按实际冻结材料保守比较，不改写历史。
v2 将报告用例与 suite 作为按内容排序的多重集合，保留重复数量；v1 从实际冻结 Worker 材料投影到新合同，排序变化不提供进展。
frontier_identity 以正式 Context 内容哈希、角色和资格表达语义，相同内容的新 revision 随机身份不提供进展。
v3 以完整正文合同、编码与原字节摘要表达真实正文信息，脱敏配置变化不当作任务进展；旧 v2 从已保存语义升级合同标签，
旧 v1/缺失身份的冻结材料比较禁止补读新正文，原输入与 hash 不写回。
示例：view = await policy.read(session, loop, round_row)；policy.require_allowed(view)。
空检查返回 completion_checks_not_defined，不允许空 verifier 请求或空集合验收通过。
"""

from copy import deepcopy

from sqlalchemy import select

from backend.app.desktop.agent_loop.completion_evidence_catalog import CompletionEvidenceCatalog
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.models import CompletionVerification, LoopContextMembership, LoopGoalRevision, LoopObservation, LoopWorkerRequest
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopThread
from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


class CompletionRequestAdmission:
    contract = 'completion-semantic-input-v3'

    async def current_input(self, session, loop, round_row):
        mission = await session.scalar(select(LoopMissionRevision).where(
            LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        goal = None if mission is not None else await session.scalar(select(LoopGoalRevision).where(
            LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
        slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id,
            WorkspaceSlot.kind == 'authoritative', WorkspaceSlot.lifecycle != 'deleted'))
        sources = tuple(await session.scalars(select(DesktopDomainResult).where(
            DesktopDomainResult.loop_id == loop.loop_id).order_by(DesktopDomainResult.created_at, DesktopDomainResult.result_key)))
        frozen = {'mission': EffectiveMissionProjector.from_rows(structured=mission, legacy=goal).model_payload(),
            'frontier_hash': await self.frontier_identity(session, loop.loop_id),
            'workspace': {'revision': slot.revision if slot else round_row.workspace_revision,
                          'fingerprint': slot.current_fingerprint if slot else None},
            'completion_sources': [{'source_id': s.result_key, 'kind': s.kind, 'run_id': s.run_id, 'payload': s.payload} for s in sources]}
        projected = await CompletionEvidenceCatalog().project(session, frozen, loop.loop_id, frozen['workspace']['revision'])
        return self.input_identity(projected)

    async def read(self, session, loop, round_row):
        current = await self.current_input(session, loop, round_row)
        if not current['semantic']['mission'].get('completion_checks'):
            return {'input': current, 'allowed': False, 'code': 'completion_checks_not_defined',
                    'verification_id': None, 'unresolved': [], 'satisfied_check_ids': [],
                    'previous_input_fingerprint': None}
        latest = await session.scalar(select(CompletionVerification).where(
            CompletionVerification.loop_id == loop.loop_id).order_by(CompletionVerification.created_at.desc()).limit(1))
        previous = None
        if latest is not None:
            worker = await session.get(LoopWorkerRequest, latest.worker_request_id)
            if worker is not None and worker.status == 'success':
                previous = worker.scope.get('completion_admission_input')
                if previous and previous.get('contract') == 'completion-semantic-input-v2':
                    previous = deepcopy(previous)
                    previous['contract'] = previous['semantic']['contract'] = self.contract
                    previous['fingerprint'] = canonical_hash(previous['semantic'])
                if (previous is None or previous.get('contract') != self.contract) and worker.scope.get('frozen_worker_input') is not None:
                    frozen = worker.scope['frozen_worker_input']
                    projected = await CompletionEvidenceCatalog().project(session, frozen, loop.loop_id, latest.workspace_revision,
                        include_artifact_content=False)
                    observation = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == worker.round_id))
                    frontier = self.frozen_frontier(observation.envelope) if observation else frozen.get('frontier_hash')
                    previous = self.input_identity(projected, frontier_hash=frontier)
        repeated = bool(previous and previous['fingerprint'] == current['fingerprint'])
        return {'input': current, 'allowed': not repeated,
                'code': 'completion_evidence_unchanged' if repeated else None,
                'verification_id': latest.verification_id if latest else None,
                'unresolved': list(latest.unresolved) if latest else [],
                'satisfied_check_ids': sorted(c['check_id'] for c in latest.criteria if c.get('status') == 'satisfied' and c.get('evidence')) if latest and repeated else [],
                'previous_input_fingerprint': previous['fingerprint'] if previous else None}

    @staticmethod
    def require_allowed(view):
        if not view['allowed']:
            raise ValueError(str(view.get('code') or 'completion_evidence_unchanged') + ': verification=' + str(view['verification_id'])
                             + '; unresolved=' + ','.join(view['unresolved']))

    @classmethod
    def input_identity(cls, projected, *, frontier_hash=None):
        mission = projected['mission']
        semantic = {'contract': cls.contract, 'mission': {k: mission.get(k) for k in
            ('revision', 'outcome', 'boundaries', 'completion_checks')},
            'frontier_hash': frontier_hash or projected.get('frontier_hash'),
            'workspace': {k: projected.get('workspace', {}).get(k) for k in ('revision', 'fingerprint')},
            'evidence': sorted({canonical_hash(cls._source_content(s)) for s in projected.get('completion_sources', ())
                               if s.get('eligibility', {}).get('status') == 'available'})}
        return {'contract': cls.contract, 'fingerprint': canonical_hash(semantic), 'semantic': semantic,
                'source_ids': sorted(projected.get('available_source_ids', ()))}

    @staticmethod
    def frozen_frontier(envelope):
        frontier = [{k: item.get(k) for k in ('context_id', 'role', 'required_barrier', 'content_hash', 'projection_status')}
                    for item in sorted(envelope.get('portfolio_frontier', ()),
                        key=lambda item: item['context_id'])]
        return canonical_hash(frontier)

    @staticmethod
    async def frontier_identity(session, loop_id):
        rows = (await session.execute(select(LoopContextMembership.context_id, LoopContextMembership.role,
            LoopContextMembership.required_barrier, ContextRevision.content_hash, ContextRevision.projection_status)
            .outerjoin(DesktopThread, DesktopThread.task_id == LoopContextMembership.context_id)
            .outerjoin(ContextRevision, ContextRevision.revision_id == DesktopThread.current_revision_id)
            .where(LoopContextMembership.loop_id == loop_id, LoopContextMembership.status == 'active')
            .order_by(LoopContextMembership.context_id))).mappings()
        return canonical_hash([dict(row) for row in rows])

    @staticmethod
    def _source_content(source):
        payload = source['payload']
        kind = source['kind']
        if kind == 'test':
            proof, report = payload.get('execution_proof') or {}, payload.get('named_results') or {}
            content = {'status': payload.get('status'), 'metrics': payload.get('metrics'),
                'proof': {k: proof.get(k) for k in ('command', 'status', 'exit_code', 'bound', 'workspace')},
                'report': {'status': report.get('status'), 'protocol': report.get('protocol'),
                    **{group: sorted([{k: item.get(k) for k in ('name', 'suite', 'status', 'coverage')}
                               for item in report.get(group, ())], key=canonical_hash) for group in ('cases', 'suites')}}}
        elif kind == 'artifact':
            content = {k: payload.get(k) for k in ('path', 'sha256', 'size_bytes', 'workspace_revision')}
            if source.get('artifact_content'):
                content['artifact_content'] = {k: source['artifact_content'].get(k)
                    for k in ('contract', 'encoding', 'complete', 'sha256')}
        elif kind == 'workspace':
            content = {k: payload.get(k) for k in ('revision', 'fingerprint', 'workspace_status')}
        else:
            content = {k: v for k, v in payload.items() if k not in
                {'run_id', 'call_id', 'result_key', 'source_id', 'created_at', 'timestamp', 'version', 'execution_proof'}}
        return {'kind': kind, 'eligibility': source['eligibility'], 'content': content}
