"""Suspend the next model decision during manual control, then request a new view."""
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from focus.security.context import security_context_of


class BrowserControlMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        run = security_context_of(request.runtime.context).extras["browser_run"]
        while run.browser.state in {"taking_over", "manual", "paused"}:
            generation = run.owner.changes.generation
            await run.owner.changes.wait(generation, timeout=30)
            run.check()
        run.check()
        if run.handoff:
            run.handoff = False
            request = request.override(messages=[*request.messages, HumanMessage(content=(
                "[Focus 浏览器交接] 用户已交还控制。原页面和用户输入已保留；旧观察和旧动作均已失效。"
                "下一次浏览器操作必须先调用 browser_observe 重新取得租约并观察当前页面，再决定动作；"
                "禁止直接重放交接前的动作。"))])
        return await handler(request)
