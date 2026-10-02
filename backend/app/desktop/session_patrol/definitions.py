"""本文件对外提供 DefinitionRepository 的不可变投放定义读取、原定义解析与插入。

输入为调用方事务、Run identity、文档与编译/来源快照；输出为冻结定义 ORM 记录。
工作流只查询或 INSERT，不提交事务；创建由协调器与 Run/dispatch 一同提交，数据库触发器拒绝修改/删除。
原定义沿同 agent 的精确 definition/source Run 链读取，无 V2 定义返回 legacy 回退信号；断链/循环返回冲突。
示例：repository.freeze(session, run_id, document, plan, sources, execution_mode="restart")。
"""
from copy import deepcopy
import uuid
from sqlalchemy import select
from fastapi import HTTPException
from backend.app.desktop.models import PatrolDeploymentDefinition, DesktopRun


class DefinitionRepository:
    @staticmethod
    async def for_run(session, run_id):
        return await session.scalar(select(PatrolDeploymentDefinition).where(PatrolDeploymentDefinition.run_id == run_id))

    @classmethod
    async def original_for_run(cls, session, run_id, agent_id):
        visited = set()
        while run_id:
            if run_id in visited:
                raise HTTPException(409, {"code": "original_definition_unavailable", "message": "原定义引用存在循环"})
            visited.add(run_id)
            run = await session.get(DesktopRun, run_id)
            if run is None or run.agent_id != agent_id:
                raise HTTPException(409, {"code": "original_definition_unavailable", "message": "原定义分支不可读取"})
            definition = await cls.for_run(session, run_id)
            if definition is None or definition.execution_mode == "restart":
                return definition
            original_id = definition.compiled_plan.get("original_definition_id")
            if original_id:
                original = await session.get(PatrolDeploymentDefinition, original_id)
                if original is None:
                    raise HTTPException(409, {"code": "original_definition_unavailable", "message": "原冻结定义不可读取"})
                run_id = original.run_id
            else:
                run_id = definition.compiled_plan.get("source_run_id")
                if not run_id:
                    raise HTTPException(409, {"code": "original_definition_unavailable", "message": "继续定义缺少原来源分支"})
        return None

    @staticmethod
    def freeze(session, run_id, document, plan, sources, *, execution_mode):
        definition = PatrolDeploymentDefinition(definition_id=uuid.uuid4().hex, run_id=run_id,
            document_hash=document.content_hash, document=document.model_dump(mode="json"),
            compiled_plan=deepcopy(plan), sources=deepcopy(sources), execution_mode=execution_mode)
        session.add(definition)
        return definition
