"""本文件对外提供 PatrolPreview 的编译与实际请求准入端口。

输入为冻结候选草稿、装备和宿主组合根；输出为逐条诊断、实际请求、分项窗口与 freshness token。
工作流为纯编译后调用统一 Agent 工厂观察端口，装配当前 WorldState、工具及冻结技能／材料，不采样。
示例：plan = await PatrolPreview(host).build(draft, task, workspace)；token 绑定完整请求。
"""
from dataclasses import asdict
from types import SimpleNamespace
from focus.history import content_hash, deserialize_history_messages
from focus.models.provider_contract import resolve_provider_contract
from focus.messages.request_budget import estimate_responses_budget
from .compiler import compile_document
from .repository import DraftRepository


class PatrolPreview:
    def __init__(self, host):
        self._host = host

    async def build(self, draft, task, workspace):
        document = DraftRepository.document(draft)
        model = self._host.app_config.get_model(draft.equipment.get("model_name") or self._host.app_config.resolve_default_model_name())
        contract = resolve_provider_contract(model)
        compiled = compile_document(document, provider=contract.provider, sources=draft.frozen_sources)
        plan = {"document_hash": document.content_hash, "draft_revision": draft.draft_revision,
                "compiler_fingerprint": compiled.fingerprint, "target": asdict(contract),
                "diagnostics": compiled.diagnostics, "role_mappings": compiled.role_mappings,
                "messages": compiled.messages, "items": compiled.items,
                "transformations": compiled.transformations, "executable": False}
        snapshots = draft.equipment.get("skill_snapshots")
        if snapshots is None:
            snapshots = self._host._freeze_skills(workspace.path, draft.equipment.get("skills", []))
        equipment = {**draft.equipment, "skill_snapshots": snapshots}
        identity = content_hash([draft.draft_id, document.content_hash])[:32]
        plan.update(agent_id=identity, execution_thread_id="session-patrol:" + identity, checkpoint_ns="patrol:" + identity, run_id=identity)
        if not compiled.executable:
            return plan
        if contract.protocol != "responses":
            plan["diagnostics"].append({"code": "target_protocol", "entry_ids": [], "message": "请选择 Responses 模型以预览执行合同"})
            return plan
        material_policy, _ = await self._host._material_context(draft.task_id)
        observed = []
        factory = self._host._build_agent_factory(draft.task_id, identity, workspace.path, equipment, document.instructions, material_policy, "patrol", preparation_observer=observed.append)
        await factory()
        run = SimpleNamespace(run_id=identity, task_id=draft.task_id, agent_id=identity, model_name=equipment.get("model_name"), context_revision_id=None, context_checkpoint_id=None, origin_message_id=None)
        context = self._host._governed_context(thread_id=plan["execution_thread_id"], run=run, workspace_id=workspace.workspace_id,
            workspace_path=workspace.path, permissions=equipment.get("permissions") or ["read"], access_mode=equipment.get("access_mode"),
            checkpoint_ns=plan["checkpoint_ns"], agent_role="patrol", model_name=equipment.get("model_name"), allow_global_config=False, extras={"skills": equipment.get("skills", [])})
        try:
            request = observed[0].preview(deserialize_history_messages(compiled.messages), context)
        except (ValueError, TypeError, KeyError) as exc:
            plan["diagnostics"].append({"code": "provider_projection", "entry_ids": [e.entry_id for e in document.entries], "message": str(exc)})
            return plan
        reserve = int(request.get("max_output_tokens") or 4096)
        budget = {"instructions": estimate_responses_budget({"instructions": request.get("instructions", "")}),
                  "tools": estimate_responses_budget({"tools": request.get("tools", [])}),
                  "input": estimate_responses_budget({"input": request["input"]}),
                  "output_reserve": reserve, "total": estimate_responses_budget(request) + reserve,
                  "context_window": contract.context_window}
        if contract.context_window and budget["total"] > contract.context_window:
            plan["diagnostics"].append({"code": "window_exceeded", "entry_ids": [], "message": "完整请求超过模型窗口；可换模型、编辑或显式转换"})
        plan.update(request=request, budget=budget, equipment=equipment, material_policy=material_policy,
                    preparation_fingerprint=observed[0].fingerprint,
                    executable=not plan["diagnostics"])
        plan["preview_token"] = content_hash([document.content_hash, draft.draft_revision, compiled.fingerprint, observed[0].fingerprint, request, equipment, workspace.path])
        return plan
