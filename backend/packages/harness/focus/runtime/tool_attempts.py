"""本文件对外提供 ToolExecutionMiddleware 与 ToolExecutionUncertain 的恢复执行边界。

输入为受治理 runtime 中注入的 ledger 端口、原调用消息和工具请求；输出为新结果或已保存 canonical 结果。
具体工作流为稳定来源 claim、执行和结果保存；缺少可靠结果时阻止重复执行，不替代 Security 的实时裁决。
delegated execution 使用其独立子执行 checkpoint，普通子 Agent 可继承 ledger 端口保护自己的工具调用。
示例：make_lead_agent 默认装配此 middleware；Desktop 通过 security.extras 提供 tool_execution_ledger。
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from focus.history import content_hash
from focus.security.context import security_context_of
from focus.security.effects import ToolEffectKind, effect_of


class ToolExecutionUncertain(RuntimeError):
    pass


class ToolExecutionMiddleware(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        context = request.runtime.context
        ledger = context.get("tool_execution_ledger") if isinstance(context, dict) else None
        if ledger is None or effect_of(request.tool).kind is ToolEffectKind.DELEGATED_EXECUTION:
            return await handler(request)
        routing = security_context_of(context).routing
        messages = request.runtime.state.get("messages", ())
        parent_id = next((message.id for message in reversed(messages) if getattr(message, "tool_calls", ())), None)
        if not parent_id:
            raise ToolExecutionUncertain("工具缺少稳定调用来源，不能宣称安全恢复")
        identity = content_hash([routing.thread_id, routing.checkpoint_ns, routing.agent_id, parent_id, request.tool_call["id"]])
        existing = await ledger.claim(identity, routing.run_id, request.tool_call)
        if existing is not None:
            return existing
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            raise ToolExecutionUncertain("工具返回非消息状态结果，需独立耐久执行合同")
        if result.tool_call_id != request.tool_call["id"]:
            raise ToolExecutionUncertain("工具结果 call identity 冲突")
        if result.id is None:
            result = result.model_copy(update={"id": f"tool-result:{identity}"})
        await ledger.complete(identity, result)
        return result
