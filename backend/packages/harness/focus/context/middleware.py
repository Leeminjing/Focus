"""本文件对外提供 WorldStateMiddleware 的最终请求准备与 sampling manifest。

输入为受治理运行上下文、真实工具/基础指令、冻结 selected bindings 及精确 graph state；输出为同时提交的 Items、snapshot 和 prepared manifest。
具体工作流为在历史手术后刷新状态、补齐冻结引用、核对实际 continuation 前缀；不兼容时显式重建执行分支，
再将完整请求来源（含合同／协作 source refs）与 typed authority 原子提交。在完整模型返回后标记 sampled；工具授权仍由安全中间件独立刷新。
示例：middleware = WorldStateMiddleware(tools, base_instructions, skill_names, model_name)。
"""

import uuid

from langchain.agents.middleware import AgentMiddleware
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from focus.context.sections import execution_sections
from focus.context.scoped import prepare_scoped_contexts, scoped_messages
from focus.context.replay import continuation_rebuild_reason, rebuilt_prefix
from focus.context.world_state import compile_world_state
from focus.history import content_hash
from focus.history.bridge import ExecutionHistoryRebuild, synchronize_items
from focus.models.provider_contract import ProviderContract
from focus.security.context import has_security_context, security_context_of
from focus.security.live_mode import refreshed_runtime_context


class WorldStateMiddleware(AgentMiddleware):
    def __init__(self, tools, base_instructions: str, skill_names: frozenset[str], model_name: str, projection_version: str,
                 *, skill_catalog: dict | None = None, provider_contract: dict | None = None, frozen_contexts=()):
        self._tool_specs = [convert_to_openai_tool(tool) for tool in tools]
        self._instructions_hash = content_hash(base_instructions)
        self._skill_names = skill_names
        self._skill_catalog = skill_catalog
        self._model_name = model_name
        self._projection_version = projection_version
        self._provider_contract = provider_contract
        self._instructions = base_instructions
        self._frozen_contexts = tuple(frozen_contexts)
        self._contract = ProviderContract(**provider_contract) if provider_contract else None

    def preview_messages(self, state, context):
        messages, _, _, _ = self._prepare(state, context)
        return messages

    def _assemble(self, messages, previous, context):
        security = security_context_of(context)
        sections = execution_sections(security, self._tool_specs, self._skill_names, self._model_name, self._skill_catalog)
        updates, snapshot = compile_world_state(sections, previous,
                                               {message.id for message in messages if message.id}, self._binding(security))
        updates.extend(prepare_scoped_contexts([*messages, *updates], self._frozen_contexts, security.routing.run_id))
        return [*messages, *updates], updates, snapshot

    def _prepare(self, state, context):
        messages, updates, snapshot = self._assemble(state["messages"], state.get("world_state_snapshot"), context)
        reason = continuation_rebuild_reason(messages, context, self._instructions, self._contract,
                                             self._model_name, self._tool_specs)
        if reason is not None:
            messages, _, snapshot = self._assemble(rebuilt_prefix(state["messages"], reason), None, context)
            updates = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]
        return messages, updates, snapshot, reason

    def _binding(self, security):
        return {"thread_id": security.routing.thread_id, "checkpoint_ns": security.routing.checkpoint_ns,
                "workspace": str(security.authorization.workspace), "agent_id": security.routing.agent_id,
                "projection_version": self._projection_version, "model_name": self._model_name,
                "provider_contract": self._provider_contract}

    async def abefore_model(self, state, runtime):
        if not has_security_context(runtime.context):
            return None
        context = await refreshed_runtime_context(runtime.context)
        security = security_context_of(context)
        messages, updates, snapshot, reason = self._prepare(state, context)
        binding = self._binding(security)
        items = synchronize_items(None if reason else state.get("execution_items"), messages)
        visible_ids = {message.id for message in scoped_messages(messages, security.routing.run_id)}
        manifest = {
            "attempt_id": uuid.uuid4().hex, "status": "prepared", "binding": binding,
            "run_id": security.routing.run_id, "model_name": self._model_name,
            "provider_contract": self._provider_contract,
            "instructions_hash": self._instructions_hash, "tools_hash": content_hash(self._tool_specs),
            "item_ids": [item["item_id"] for item in items if item.get("message_id") in visible_ids],
            "source_bindings": [{"item_id": item["item_id"], "kind": item["kind"], "origin": item["origin"],
                                 "source_refs": item["source_refs"]} for item in items
                                if item.get("message_id") in visible_ids and item["source_refs"]],
            "world_state_hash": content_hash(snapshot),
            "context_revision_ref": context.get("context_revision_ref"),
            "selected_bindings": [item["source_refs"] for item in items if item.get("message_id") in visible_ids
                                  and item["kind"] in {"selected_context", "round_decision_context"}],
            "material_bindings": context.get("run_material_inputs"),
            "history_rebuild_reason": reason,
        }
        replacement = ExecutionHistoryRebuild(content_hash(state.get("execution_items")), items) if reason else items
        return {"messages": updates, "execution_items": replacement, "world_state_snapshot": snapshot, "request_manifest": manifest}

    async def awrap_model_call(self, request, handler):
        if not has_security_context(request.runtime.context):
            return await handler(request)
        run_id = security_context_of(request.runtime.context).routing.run_id
        return await handler(request.override(messages=scoped_messages(request.messages, run_id)))

    async def aafter_model(self, state, runtime):
        manifest = state.get("request_manifest")
        if manifest is None:
            return None
        return {"request_manifest": {**manifest, "status": "sampled", "response_message_id": state["messages"][-1].id}}
