"""本文件对外提供 RoundProgressReader 的稳定事务语义进展判定。

输入为已稳定 Round、冻结 Observation 和当前同源完成事实；输出为 fingerprint、是否进展及判定基线合同。
具体工作流为比较真实 workspace/frontier/证据资格内容及必要检查的可信满足状态；Run/Worker success 和消费身份不参与。
旧观察无新版事实时读取其实际冻结 workspace/frontier 内容；缺少可比较内容才回退原版本身份，不猜测回填；本模块只读，计数由 rounds 唯一推进事务更新。
旧语义合同与当前版本不同时同样使用保守基线，合同版本更新本身不算任务进展。
示例：state = await RoundProgressReader().read(session, loop, round_row, current_frontier)。
"""

from sqlalchemy import select

from backend.app.desktop.agent_loop.completion_admission import CompletionRequestAdmission
from backend.app.desktop.agent_loop.models import CompletionVerification, LoopObservation
from backend.app.desktop.domain_evidence.identity import canonical_hash


class RoundProgressReader:
    async def read(self, session, loop, round_row, frontier_hash):
        current = await CompletionRequestAdmission().current_input(session, loop, round_row)
        semantic = current['semantic']
        observation = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
        frozen = observation.envelope if observation else {}
        baseline = (frozen.get('completion_admission') or {}).get('input')
        baseline_semantic = baseline.get('semantic') if baseline and baseline.get('contract') == current['contract'] else None
        if baseline_semantic is None:
            workspace = frozen.get('workspace')
            frontier = frozen.get('portfolio_frontier', ())
            workspace_changed = (semantic['workspace'] != {k: workspace.get(k) for k in ('revision', 'fingerprint')}
                if workspace is not None else semantic['workspace']['revision'] != round_row.workspace_revision)
            frontier_changed = (semantic['frontier_hash'] != CompletionRequestAdmission.frozen_frontier(frozen)
                if frontier and all('content_hash' in item for item in frontier)
                else frontier_hash != round_row.frontier_hash)
            changed = workspace_changed or frontier_changed
        else:
            changed = canonical_hash(semantic) != canonical_hash(baseline_semantic)
        latest = await session.scalar(select(CompletionVerification).where(
            CompletionVerification.loop_id == loop.loop_id,
            CompletionVerification.round_id == round_row.round_id).order_by(CompletionVerification.created_at.desc()).limit(1))
        previous_satisfied = set((frozen.get('completion_admission') or {}).get('satisfied_check_ids', ()))
        satisfied = {c['check_id'] for c in latest.criteria if c.get('status') == 'satisfied' and c.get('evidence')} if latest else set()
        return {'contract': 'stable-round-progress-v1', 'fingerprint': canonical_hash(semantic),
                'progressed': changed or bool(satisfied - previous_satisfied),
                'baseline': 'frozen_semantic_input' if baseline_semantic is not None else 'legacy_workspace_frontier'}
