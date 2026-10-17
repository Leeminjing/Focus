"""本文件对外提供 ToolExecutionMiddleware 与 ToolExecutionUncertain 的恢复执行边界。

输入为受治理 runtime 中注入的 ledger 端口、原调用消息和工具请求；输出为新结果或已保存 canonical 结果。
具体工作流为稳定来源 claim、执行和结果保存；所有平台 Shell 在 claim 同事务登记待清理证据，结果仅凭明确进程清理回执解除结算屏障；缺少可靠结果时阻止重复执行，不替代 Security 的实时裁决。
delegated execution 使用其独立子执行 checkpoint，普通子 Agent 可继承 ledger 端口保护自己的工具调用。
示例：make_lead_agent 默认装配此 middleware；Desktop 通过 security.extras 提供 tool_execution_ledger。
文件采集端口由 ledger 注入本次 Security extras，缓存结果直接返回，采集结果只随本次 complete 提交。
"""

from dataclasses import replace

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
        options = {"requires_process_cleanup": True} if effect_of(request.tool).kind is ToolEffectKind.SANDBOXED_SHELL else {}
        existing = await ledger.claim(identity, routing.run_id, request.tool_call, **options)
        if existing is not None:
            return existing
        receipt = {"process_cleanup": "not_started"} if options else None
        if receipt is not None and hasattr(ledger, "shell_receipt"):
            receipt = await ledger.shell_receipt(identity)
        capture = await ledger.prepare_file_capture(routing.run_id) if hasattr(ledger, "prepare_file_capture") else None
        if receipt is not None or capture is not None:
            security = security_context_of(context)
            security = replace(security, extras={**security.extras, "shell_cleanup_receipt": receipt, "file_version_capture": capture})
            request = request.override(runtime=replace(request.runtime,
                context={**context, **security.to_runtime_context()}))
        try:
            result = await handler(request)
        except BaseException:
            if receipt is not None:
                await ledger.complete(identity, None, process_cleanup=receipt["process_cleanup"])
            raise
        if not isinstance(result, ToolMessage):
            raise ToolExecutionUncertain("工具返回非消息状态结果，需独立耐久执行合同")
        if result.tool_call_id != request.tool_call["id"]:
            raise ToolExecutionUncertain("工具结果 call identity 冲突")
        if result.id is None:
            result = result.model_copy(update={"id": f"tool-result:{identity}"})
        completion = {"process_cleanup": receipt["process_cleanup"]} if receipt is not None else {}
        if capture is not None:
            completion["file_capture"] = capture
        await ledger.complete(identity, result, **completion)
        return result
