"""本文件对外提供 PatrolDeploymentCoordinator 的冻结登记、restart、continue 与 resume。

输入为草稿、预览 token 或精确 Run/checkpoint；输出为现有 durable dispatch 上的唯一 Run。
工作流为锁定草稿、检查 freshness、同事务写定义／Run／dispatch；restart 新身份，continue 钉住 C，resume 恢复原中断。
来源 R/C 只存定义，不拼接父 current pointer。模型／工具 ledger 仍由现有执行脊柱维护。
原定义沿精确同 agent 来源链解析，旧 continue 以 legacy 冻结输入回退；来源状态只读检查不改变文档/hash。
示例：await coordinator.deploy(draft_id, key, preview_token)；retry 只读取原冻结定义。
部署 key 在 PostgreSQL 事务锁内绑定完整文档（含显式变换）、装备与模式；edit_definition 从不可变定义产生新草稿，preparation_guard 在采样前校验真实工厂合同。
"""
from copy import deepcopy
import uuid
from fastapi import HTTPException
from sqlalchemy import select, text
from backend.app.desktop.models import DesktopRun, PatrolDraft, PatrolAgent
from backend.app.desktop.run_orchestration.models import RunDispatch
from .preview import PatrolPreview
from .repository import DraftRepository
from .contracts import legacy_document
from .definitions import DefinitionRepository
from focus.history import content_hash


class PatrolDeploymentCoordinator:
    def __init__(self, host):
        self._host = host
        self._preview = PatrolPreview(host)

    async def preview(self, draft_id):
        async with self._host.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id)
            if draft is None or draft.mode != "standard":
                raise HTTPException(404, "普通 Patrol 草稿不存在")
            task, workspace = await self._host._get_task_entities(session, draft.task_id)
            return await self._preview.build(draft, task, workspace)

    async def audit(self, run_id):
        from .audit import PatrolAuditReader
        return await PatrolAuditReader(self._host).read(run_id)

    async def read_draft(self, draft_id):
        async with self._host.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id)
            if draft is None:
                raise HTTPException(404, "草稿不存在")
            return self._host._draft_payload(draft)

    async def source_status(self, draft_id):
        from .availability import SourceAvailabilityReader
        async with self._host.session_factory() as session:
            draft = await session.get(PatrolDraft, draft_id)
            if draft is None:
                raise HTTPException(404, "草稿不存在")
            return {"sources": await SourceAvailabilityReader(self._host).inspect(session, draft.frozen_sources or {})}

    async def deploy(self, draft_id, key, token=None, *, visible_agent_id=None):
        async with self._host.session_factory() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "patrol-deployment:" + key})
            draft = await session.get(PatrolDraft, draft_id, with_for_update=True)
            if draft is None:
                raise HTTPException(404, "草稿不存在")
            document = DraftRepository.document(draft)
            admission_hash = content_hash([document.model_dump(mode="json"), draft.equipment, "restart"])
            existing = await session.scalar(select(DesktopRun).where(DesktopRun.deployment_id == key))
            if existing:
                definition = await DefinitionRepository.for_run(session, existing.run_id)
                if not definition or definition.compiled_plan.get("admission_hash") != admission_hash or existing.task_id != draft.task_id:
                    raise HTTPException(409, {"code": "deployment_key_conflict"})
                return self._accepted(existing)
            if draft.status != "editing":
                raise HTTPException(409, "草稿已投放；重跑将创建新分支")
            if draft.authoring_document is not None and token is None and visible_agent_id is None:
                raise HTTPException(409, {"code": "refresh_required", "message": "请先预览当前文档"})
            task, workspace = await self._host._get_task_entities(session, draft.task_id)
            plan = await self._preview.build(draft, task, workspace)
            if not plan["executable"]:
                raise HTTPException(422, {"code": "compilation_failed", "diagnostics": plan["diagnostics"], "budget": plan.get("budget")})
            if token is not None and token != plan["preview_token"]:
                raise HTTPException(409, {"code": "refresh_required", "message": "定义或实际装备已变化，请刷新预览"})
            self._host._require_run_admission()
            agent_id = visible_agent_id or plan["agent_id"]
            plan["equipment_hash"] = content_hash(draft.equipment)
            plan["admission_hash"] = admission_hash
            equipment = {**plan["equipment"], "_patrol_material_policy": plan["material_policy"],
                         "_patrol_preparation_fingerprint": plan["preparation_fingerprint"],
                         "_durable_dispatch_execution": {"agent_role": "patrol", "base_prompt": document.instructions, "checkpoint_id": None, "execution_mode": "restart"}}
            run = DesktopRun(run_id=plan["run_id"], task_id=draft.task_id, agent_id=agent_id, deployment_id=key,
                kind="patrol", status="pending", input_messages=plan["messages"], model_name=equipment.get("model_name"), origin="direct_user",
                execution_thread_id=plan["execution_thread_id"], checkpoint_ns=plan["checkpoint_ns"], equipment=equipment,
                workspace_anchor={"workspace_id": workspace.workspace_id, "workspace_path": workspace.path})
            if visible_agent_id is None:
                session.add(PatrolAgent(agent_id=agent_id, task_id=draft.task_id, checkpoint_ns=plan["checkpoint_ns"],
                    system_prompt=document.instructions, frozen_messages=plan["messages"], equipment=equipment, source_checkpoint_id=draft.source_checkpoint_id, mode="standard", curation_policy={}))
            session.add(run)
            await session.flush()
            DefinitionRepository.freeze(session, run.run_id, document, plan, draft.frozen_sources, execution_mode="restart")
            session.add(RunDispatch(dispatch_id=uuid.uuid4().hex, run_id=run.run_id, status="accepted"))
            draft.status = "deployed"
            await session.commit()
        return self._accepted(run)

    async def restart(self, agent_id, key=None):
        draft = await self._copy_definition(agent_id)
        return await self.deploy(draft.draft_id, key or uuid.uuid4().hex, visible_agent_id=agent_id)

    async def edit_definition(self, agent_id):
        return self._host._draft_payload(await self._copy_definition(agent_id))

    async def _copy_definition(self, agent_id):
        async with self._host.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if not agent or agent.mode != "standard":
                raise HTTPException(409, "此小兵不能普通重跑")
            latest = await session.scalar(select(DesktopRun).where(DesktopRun.agent_id == agent_id).order_by(DesktopRun.created_at.desc()))
            definition = await DefinitionRepository.original_for_run(session, latest.run_id, agent_id) if latest else None
            doc = definition.document if definition else legacy_document(agent.system_prompt, agent.frozen_messages).model_dump(mode="json")
            draft = PatrolDraft(draft_id=uuid.uuid4().hex, task_id=agent.task_id, authoring_document=deepcopy(doc), draft_revision=0,
                                frozen_sources=deepcopy(definition.sources if definition else {}), equipment=deepcopy(definition.compiled_plan.get("equipment", agent.equipment) if definition else agent.equipment), status="editing", mode="standard")
            session.add(draft)
            await session.commit()
        return draft

    async def continue_from(self, agent_id, message, *, source_run_id=None, checkpoint_id=None):
        async with self._host.session_factory() as session:
            agent = await session.get(PatrolAgent, agent_id)
            if agent is None or agent.mode != "standard":
                raise HTTPException(404, "普通小兵不存在")
            run = await session.get(DesktopRun, source_run_id) if source_run_id else await session.scalar(select(DesktopRun).where(DesktopRun.agent_id == agent_id).order_by(DesktopRun.created_at.desc()))
            if run is None or run.agent_id != agent_id:
                raise HTTPException(404, "分支不存在")
            definition = await DefinitionRepository.for_run(session, run.run_id)
            task, workspace = await self._host._get_task_entities(session, agent.task_id)
            config = {"configurable": {"thread_id": run.execution_thread_id or task.thread_id, "checkpoint_ns": run.checkpoint_ns or agent.checkpoint_ns}}
            if checkpoint_id:
                config["configurable"]["checkpoint_id"] = checkpoint_id
            checkpoint = await self._host.checkpointer.aget_tuple(config)
            if checkpoint is None:
                raise HTTPException(409, "分支暂无可继续的稳定 checkpoint")
            pinned = checkpoint.config["configurable"]["checkpoint_id"]
            if checkpoint_id and pinned != checkpoint_id:
                raise HTTPException(409, "精确 checkpoint 不匹配")
            if checkpoint.checkpoint.get("channel_values", {}).get("__interrupt__") or checkpoint.pending_writes:
                raise HTTPException(409, {"code": "resume_required", "message": "此分支有未结算状态，请恢复原运行"})
            equipment = deepcopy(run.equipment or agent.equipment)
            base = equipment.get("_durable_dispatch_execution", {}).get("base_prompt", agent.system_prompt)
            await self._require_compatible_target(agent, workspace.path, equipment, base)
            self._host._require_run_admission()
            equipment["_durable_dispatch_execution"] = {"agent_role": "patrol", "base_prompt": base, "checkpoint_id": pinned, "execution_mode": "continue"}
            identity = uuid.uuid4().hex
            records = [{"role": "human", "content": message, "id": "patrol-input:" + identity,
                        "additional_kwargs": {"focus_context": {"origin": "user_authored", "kind": "message", "scope": "execution"}}}]
            new_run = DesktopRun(run_id=identity, task_id=agent.task_id, agent_id=agent_id, kind="patrol", status="pending", input_messages=records,
                model_name=run.model_name, origin="direct_user", execution_thread_id=config["configurable"]["thread_id"], checkpoint_ns=config["configurable"]["checkpoint_ns"],
                equipment=equipment, workspace_anchor=deepcopy(run.workspace_anchor))
            session.add(new_run)
            await session.flush()
            doc = legacy_document(base, records)
            plan = {"source_run_id": run.run_id, "checkpoint_id": pinned, "original_definition_id": definition.compiled_plan.get("original_definition_id", definition.definition_id) if definition else None}
            DefinitionRepository.freeze(session, identity, doc, plan, {"branch": config["configurable"]}, execution_mode="continue")
            session.add(RunDispatch(dispatch_id=uuid.uuid4().hex, run_id=identity, status="accepted"))
            await session.commit()
        return self._accepted(new_run)

    async def resume(self, run_id, payload):
        async with self._host.session_factory() as session:
            run = await session.get(DesktopRun, run_id, with_for_update=True)
            if run is not None and run.status in {"pending", "running"} and (run.equipment or {}).get("_patrol_resume_hash") == content_hash(payload):
                return self._accepted(run)
            if run is None or run.kind != "patrol" or run.status != "interrupted":
                raise HTTPException(409, "请选择已中断的小兵运行")
            task, workspace = await self._host._get_task_entities(session, run.task_id)
            agent = await session.get(PatrolAgent, run.agent_id)
            config = {"configurable": {"thread_id": run.execution_thread_id or task.thread_id, "checkpoint_ns": run.checkpoint_ns or agent.checkpoint_ns}}
            if run.final_checkpoint_id:
                config["configurable"]["checkpoint_id"] = run.final_checkpoint_id
            checkpoint = await self._host.checkpointer.aget_tuple(config)
            if checkpoint is None:
                raise HTTPException(409, "中断 checkpoint 不可用")
            pinned = checkpoint.config["configurable"]["checkpoint_id"]
            execution = (run.equipment or {}).get("_durable_dispatch_execution", {})
            equipment = deepcopy(run.equipment or agent.equipment)
            equipment["_durable_dispatch_execution"] = {**execution, "agent_role": "patrol", "base_prompt": execution.get("base_prompt", agent.system_prompt), "checkpoint_id": pinned, "execution_mode": "resume", "resume_payload": payload}
            equipment["_patrol_resume_hash"] = content_hash(payload)
            run.equipment = equipment
            run.execution_thread_id = config["configurable"]["thread_id"]
            run.checkpoint_ns = config["configurable"]["checkpoint_ns"]
            run.status = "pending"
            run.settled_at = None
            self._host._require_run_admission()
            dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id).with_for_update())
            if dispatch is None:
                session.add(RunDispatch(dispatch_id=uuid.uuid4().hex, run_id=run_id, status="accepted"))
            else:
                dispatch.status = "accepted"
                dispatch.claimed_by = None
                dispatch.lease_expires_at = None
            await session.commit()
        return self._accepted(run)

    def _accepted(self, run):
        from backend.app.desktop.service import PreparedRun
        from backend.app.gateway.routers.thread_runs import RunCreateRequest
        self._host.notify_run_dispatch()
        return PreparedRun(body=RunCreateRequest(input={"messages": []}), thread_id=str(run.execution_thread_id or ""), agent_factory=None, payload=self._host._run_payload(run))

    async def _require_compatible_target(self, agent, workspace_path, equipment, base):
        guard = self.preparation_guard(equipment)
        if guard is None:
            return
        factory = self._host._build_agent_factory(agent.task_id, agent.agent_id, workspace_path, equipment, base,
            equipment.get("_patrol_material_policy", ""), "patrol", preparation_observer=guard)
        try:
            await factory()
        except ValueError as exc:
            raise HTTPException(409, {"code": "restart_required", "message": "原分支合同已变化，请编辑原定义、重新预览并新建分支"}) from exc

    @staticmethod
    def preparation_guard(equipment):
        expected = equipment.get("_patrol_preparation_fingerprint")
        if expected is None:
            return None

        def verify(preparation):
            if preparation.fingerprint != expected:
                raise ValueError("refresh_required: 工具或模型执行合同已变化，请重新预览并重跑新分支")

        return verify
