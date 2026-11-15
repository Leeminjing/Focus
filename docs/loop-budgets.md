# Loop 预算

Workspace Patrol 的预算固定无上限，不提供预设、调整或追加入口。首次输入与终态后继均使用同一策略。

轮次、任务时长、模型调用次数、输入与输出 token、重试次数、Lane 和 Context 数量、模型来源数、每轮新增 Lane、Loop 并发配额及 Context Expansion 资源配额均不设额外上限。持久合同以 `null` 表达无上限。

累计用量、模型请求预留、实际消费结算和未知消费记录继续保存。预算无上限不改变模型服务的计费。

运行池容量、模型上下文窗口、授权范围、工作区隔离和停滞保护仍然生效。运行池满时任务排队；预算无上限不代表无限并发。权限收窄、撤权、暂停、继续和停止保持可用。

## API 与界面

- 首输入 `POST /desktop/api/agent-loops/workspace/{workspace_id}/inputs` 不接受 `budget_preset`。
- 移除 `GET /desktop/api/agent-loops/budget-presets`。
- 授权接口 `POST /desktop/api/agent-loops/{loop_id}/grant` 仅接受授权收窄和撤权，不接受 `adjust_budgets` 或 `increase_budget`。
- 等待响应不接受 `revise_budget` 或预算修改载荷。
- 工作控制保留授权操作与用量展示；预算显示为固定无上限。

## 现有 Loop 升级

正常启动在恢复调度前，将尚未终结的 Workspace Patrol 有限授权换为无上限的新授权版本。旧授权和观察记录保留，旧执行资格失效，累计用量与请求预留不清零；Run 的实际结算仍由原终结流程负责。

升级保持暂停和等待状态。旧预算等待提供继续执行或停止，用户明确选择后才恢复；权限、审批及其他等待保持原处理流程。终态 Loop 和已撤销授权的历史内容不重写。验收检查模式不执行这项数据收敛。
