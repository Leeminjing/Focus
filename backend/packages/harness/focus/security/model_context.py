"""本文件对外提供 FileModeContextMiddleware，为每次模型请求注入实际文件模式与工作区。

输入为模型请求、受治理安全上下文和可选的服务端会话模式读取器。
输出为追加当次沙箱说明的模型请求；模型消息本身不改变工具授权。
具体工作流为异步模型调用先刷新会话模式，再从安全身份生成系统说明；
同步调用使用已有身份快照，缺少受治理身份时交由执行边界拒绝。
示例：FileModeContextMiddleware().awrap_model_call(request, handler) 交付当前模式说明。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from focus.security.context import has_security_context, security_context_of
from focus.security.live_mode import refreshed_runtime_context
from focus.security.model_guidance import shell_policy_guidance


class FileModeContextMiddleware(AgentMiddleware):
    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._with_guidance(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        if has_security_context(context):
            runtime = replace(runtime, context=await refreshed_runtime_context(context))
            request = request.override(runtime=runtime)
        return await handler(self._with_guidance(request))

    @staticmethod
    def _with_guidance(request: Any) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None)
        if not has_security_context(context):
            return request
        security = security_context_of(context)
        authorization = security.authorization
        guidance = shell_policy_guidance(
            authorization.workspace, authorization.access_mode, authorization.permissions,
        )
        previous = getattr(request, "system_message", None)
        base = str(previous.content) if previous is not None else ""
        return request.override(system_message=SystemMessage(content=f"{base}\n\n{guidance}".strip()))
