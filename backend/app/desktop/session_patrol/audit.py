"""本文件对外提供 PatrolAuditReader 的只读定义与执行关联查询。

输入为精确 Run identity；输出为冻结文档、编译输入、来源 refs、当前原来源可用性、实际 checkpoint 与既有模型尝试状态。
工作流读取不可变定义和原 Run/attempt 表，仅公开可定位身份，不返回来源内部原生证明或模型私有正文。
示例：await PatrolAuditReader(host).read(run_id)，prepared/attempted 不会被改写为 completed。
"""
from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import select
from backend.app.desktop.models import DesktopRun, ModelAttemptAudit
from .definitions import DefinitionRepository
from .availability import SourceAvailabilityReader


class PatrolAuditReader:
    def __init__(self, host):
        self._host = host

    async def read(self, run_id):
        async with self._host.session_factory() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None or run.kind != "patrol":
                raise HTTPException(404, "小兵运行不存在")
            definition = await DefinitionRepository.for_run(session, run_id)
            attempts = list((await session.scalars(select(ModelAttemptAudit).where(ModelAttemptAudit.run_id == run_id).order_by(ModelAttemptAudit.created_at))).all())
            source_status = await SourceAvailabilityReader(self._host).inspect(session, definition.sources if definition else {})
            return {"definition_id": definition.definition_id if definition else None,
                    "document": deepcopy(definition.document) if definition else None,
                    "compiled_plan": deepcopy(definition.compiled_plan) if definition else None,
                    "sources": {ref: deepcopy(value.get("source", value)) for ref, value in (definition.sources if definition else {}).items()},
                    "source_status": source_status,
                    "execution": {"run_id": run.run_id, "thread_id": run.execution_thread_id, "checkpoint_ns": run.checkpoint_ns, "checkpoint_id": run.final_checkpoint_id, "status": run.status},
                    "attempts": [{"attempt_id": a.attempt_id, "status": a.status, "thread_id": a.execution_thread_id, "checkpoint_ns": a.checkpoint_ns, "checkpoint_id": a.checkpoint_id, "usage": a.usage} for a in attempts]}
