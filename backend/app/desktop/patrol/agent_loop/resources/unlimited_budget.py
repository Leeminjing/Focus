"""本文件对外提供 unlimited_loop_budgets，构造固定的无上限预算快照。

输入为无参数工厂调用；输出为新建及后继工作区 Loop 的独立预算字典。
具体工作流为序列化默认 LoopBudgetContract，保持配额和停滞保护各自语义。
产品配额保持 None；用量账本继续记录实际消耗，max_no_progress 是独立的停滞保护。
每次构造新字典，避免授权之间共享可变的扩展资源策略。
示例：budgets = unlimited_loop_budgets()。
"""

from backend.app.desktop.patrol.agent_loop.schemas import LoopBudgetContract


def unlimited_loop_budgets() -> dict:
    return LoopBudgetContract().as_grant_budgets()
