"""本文件对外提供 UserAccessGateRecovery 人工访问审批投影的正式恢复核对。

输入为已锁定Loop、精确recovery_action、实际主执行与可信检查点；输出为同事务收口或明确拒绝。
具体工作流为绑定审批原主体及已结算人工resume，严格读取同一执行身份的实际检查点，确认无访问中断才标记投影resolved并写审计；
旧投影只接受唯一被阻断Directive确定的主体。读取失败、缺失、仍有审批、主体或执行变化均保持阻断，不批准工具、不扩权、不改原payload。
示例：`await recovery.resolve(session, loop, wait, response_id, actor_id)`；外层等待响应失败时投影与审计一并回滚。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import func,select

from backend.app.desktop.agent_loop.models import LoopContextMembership,LoopDirective,LoopEventOutbox,LoopPendingDecision
from backend.app.desktop.main_execution_identity import latest_main_run
from backend.app.desktop.models import DesktopRun,DesktopThread
from backend.app.desktop.pending_interrupts import pending_interrupt_payload
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor,WorkspaceLease,WorkspaceSlot
from focus.security import canonical_text
from focus.security.approval import APPROVAL_TYPE


class UserAccessGateRecovery:
    def __init__(self,checkpointer):
        self._checkpointer=checkpointer

    async def resolve(self,session,loop,wait,response_id,actor_id):
        gate=await session.get(LoopPendingDecision,wait.scope.get('pending_decision_id'),with_for_update=True,populate_existing=True)
        if gate is None or gate.loop_id!=loop.loop_id or gate.kind!='access_approval' or gate.delegable:
            raise ValueError('人工访问审批归属或类型不匹配')
        if gate.status=='resolved':
            return
        if gate.status!='pending' or wait.round_id!=loop.current_round_id:
            raise ValueError('人工访问审批或当前Round已变化')
        original=await self._source(session,loop,wait,gate)
        task=await session.get(DesktopThread,original.task_id,with_for_update=True,populate_existing=True)
        if task is None or task.workspace_id!=loop.workspace_id:
            raise ValueError('人工访问审批主体不属于当前工作区')
        resumed=await self._resumed(session,task,original,gate)
        checkpoint=await self._checkpoint(resumed)
        current=await latest_main_run(session,task)
        if current is None or current.run_id!=resumed.run_id:
            raise ValueError('人工访问审批主执行在核对期间变化')
        gate.status='resolved'
        await self._record(session,loop,gate,original,resumed,checkpoint,response_id,actor_id)

    async def _source(self,session,loop,wait,gate):
        recovery=gate.payload.get('recovery') or {}
        request=recovery.get('request') or {}
        if recovery.get('status')!='resumable' or request.get('type')!=APPROVAL_TYPE or request.get('agent_role')!='main':
            raise ValueError('人工访问审批缺少可信可恢复来源')
        source=gate.payload.get('source')
        if source is None:
            targets=tuple(await session.scalars(select(LoopDirective.target_context_id).where(
                LoopDirective.loop_id==loop.loop_id,LoopDirective.round_id==wait.round_id,
                LoopDirective.status=='created',LoopDirective.lifecycle_state=='authorized',
                LoopDirective.queued_reason.contains('access_review_pending')).distinct()))
            if len(targets)!=1:
                raise ValueError('旧人工访问审批主体缺失或存在歧义')
            original=await session.scalar(select(DesktopRun).where(DesktopRun.loop_id==loop.loop_id,
                DesktopRun.task_id==targets[0],DesktopRun.kind=='main',DesktopRun.status=='interrupted',
                DesktopRun.created_at<=gate.created_at).order_by(DesktopRun.created_at.desc()))
        else:
            if not isinstance(source,dict) or not source.get('run_id') or not source.get('task_id'):
                raise ValueError('人工访问审批来源身份缺失')
            original=await session.get(DesktopRun,source['run_id'])
            if original is None or original.task_id!=source['task_id']:
                raise ValueError('人工访问审批来源身份不匹配')
        if original is None or original.kind!='main' or original.status!='interrupted' or original.settled_at is None:
            raise ValueError('人工访问审批原执行未稳定中断')
        if original.created_at>gate.created_at or original.loop_id not in (None,loop.loop_id):
            raise ValueError('人工访问审批原执行的时间或Loop归属不匹配')
        member=await session.scalar(select(LoopContextMembership).where(LoopContextMembership.loop_id==loop.loop_id,
            LoopContextMembership.context_id==original.task_id,LoopContextMembership.status.in_(('active','paused'))))
        if member is None or original.agent_id!=f'main:{original.task_id}':
            raise ValueError('人工访问审批原主体不属于Loop')
        path=await self._execution_path(session,loop,original)
        if (original.equipment.get('access_mode')!=request.get('access_mode') or not request.get('cwd') or not path
            or canonical_text(Path(path))!=canonical_text(Path(request['cwd']))):
            raise ValueError('人工访问审批模式或执行目录不匹配')
        return original

    @staticmethod
    async def _execution_path(session,loop,original):
        if original.workspace_anchor.get('workspace_path'):
            return original.workspace_anchor['workspace_path']
        anchor=await session.get(RunExecutionAnchor,original.run_id)
        if (anchor is None or anchor.slot_id!=original.workspace_anchor.get('slot_id')
            or anchor.lease_id!=original.workspace_anchor.get('lease_id') or anchor.context_revision_id!=original.context_revision_id):
            raise ValueError('人工访问审批原执行锚点不匹配')
        slot=await session.get(WorkspaceSlot,anchor.slot_id)
        if slot is None or slot.workspace_id!=loop.workspace_id:
            raise ValueError('人工访问审批Workspace Slot不属于当前工作区')
        if anchor.lease_id:
            lease=await session.get(WorkspaceLease,anchor.lease_id)
            if (lease is None or lease.run_id!=original.run_id or lease.slot_id!=anchor.slot_id
                or lease.fencing_token!=original.workspace_anchor.get('fencing_token')):
                raise ValueError('人工访问审批原执行租约身份不匹配')
        return slot.root_path

    @staticmethod
    async def _resumed(session,task,original,gate):
        resumed=await latest_main_run(session,task)
        if (resumed is None or resumed.origin!='resume' or resumed.status!='success' or resumed.settled_at is None
            or resumed.created_at<=gate.created_at or resumed.run_id==original.run_id):
            raise ValueError('人工访问审批尚无已结算的人工恢复')
        if (not original.execution_thread_id or not original.context_revision_id
            or any(getattr(resumed,name)!=getattr(original,name) for name in (
                'execution_thread_id','checkpoint_ns','context_revision_id','context_checkpoint_id'))):
            raise ValueError('人工访问审批恢复不是同一执行身份')
        return resumed

    async def _checkpoint(self,resumed):
        config={'configurable':{'thread_id':resumed.execution_thread_id,'checkpoint_ns':resumed.checkpoint_ns or ''}}
        try:
            checkpoint=await self._checkpointer.aget_tuple(config)
        except Exception as exc:
            raise ValueError('人工访问审批检查点不可读取') from exc
        if checkpoint is None or not isinstance(getattr(checkpoint,'checkpoint',None),dict) or not checkpoint.checkpoint.get('id'):
            raise ValueError('人工访问审批检查点缺失')
        actual=(getattr(checkpoint,'config',None) or {}).get('configurable',{})
        if actual.get('thread_id')!=resumed.execution_thread_id or (actual.get('checkpoint_ns') or '')!=(resumed.checkpoint_ns or ''):
            raise ValueError('人工访问审批检查点身份不匹配')
        if pending_interrupt_payload(checkpoint,APPROVAL_TYPE) is not None:
            raise ValueError('人工访问审批仍存在，必须先由用户处理')
        return checkpoint

    @staticmethod
    async def _record(session,loop,gate,original,resumed,checkpoint,response_id,actor_id):
        sequence=int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence),0)).where(LoopEventOutbox.loop_id==loop.loop_id)) or 0)+1
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex,loop_id=loop.loop_id,sequence=sequence,
            event_type='LoopPendingDecisionSourceResolved',idempotency_key=f'user-gate:{gate.pending_decision_id}:{response_id}',
            payload={'pending_decision_id':gate.pending_decision_id,'source_run_id':original.run_id,'resume_run_id':resumed.run_id,
                'task_id':original.task_id,'checkpoint_id':checkpoint.checkpoint['id'],'actor_id':actor_id,
                'response_id':response_id,'resolution':'source_interrupt_consumed','access_approved_by_recovery':False}))
