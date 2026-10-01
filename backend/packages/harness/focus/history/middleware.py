"""本文件对外提供 TypedHistoryMiddleware 的持久 checkpoint 历史边界。

输入为已更新的 Agent messages 与可选 typed authority；输出为同一 LangGraph superstep 的 Items 状态。
具体工作流为 sampling 前验证兼容投影及合同镜像并收录工具结果、模型后收录完整模型输出；任何既有历史偏差阻止继续执行。
合同来源明确区分 typed 与 legacy；typed 合同移除后镜像清空，消息、Items 与镜像随同一更新提交。
示例：create_agent(model, tools, middleware=[TypedHistoryMiddleware()], state_schema=LeadAgentState)。
"""

from langchain.agents.middleware import AgentMiddleware

from focus.history.bridge import synchronize_items
from focus.history.task_contract import task_contract_state_update


class TypedHistoryMiddleware(AgentMiddleware):
    async def abefore_model(self, state, runtime):
        return self._update(state)

    async def aafter_model(self, state, runtime):
        return self._update(state)

    @staticmethod
    def _update(state):
        return {"execution_items": synchronize_items(state.get("execution_items"), state["messages"]),
                **task_contract_state_update(state)}
