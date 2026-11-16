# Observation

Observation 是 Patrol 在一轮决策时使用的输入快照，保存当时的用户输入、任务状态、执行证据、Context 版本、权限和预算等依据。冻结后的基础快照保持不变，后续变化通过新的观察版本或独立认知补充表达。

**Observation 的组装和冻结不调用 AI，但会收录此前由 AI 生成的内容。** 判断来源时，需要区分“程序怎样把内容放进快照”和“内容最初由谁生成”。程序读取一段 AI 自述，不会让这段自述变成独立验证的事实。

## 组成部分与来源

| 组成部分 | 内容及来源 | 是否 AI 总结 |
| --- | --- | --- |
| **用户输入** `user_intents` | 已受理的用户原文、输入类型、附件及文件版本引用。 | **否**，保留原文与引用。 |
| **当前任务约定** `mission` | 数据库中已提交的目标、边界和完成检查。 | 冻结时**不总结**，直接读取有效版本；内容最初由谁提出，需要追溯该版本的来源。 |
| **前序任务进度** `previous_task_progress` | 已发布的任务记忆，包括成果、状态、阻塞和未决项。 | **通常包含 AI 归纳**：后台任务记忆模型解释证据、提出变更，程序校验并发布；首次基线由程序按任务约定初始化。 |
| **本次新增证据** `task_delta` | 尚未被任务记忆吸收的执行结果、测试、产物、工作区变化和用户修订。 | 清单由程序枚举；**证据正文有 AI 与非 AI 两类**，见下表。 |
| **Context 集合现状** `portfolio_frontier` | 活动 Context、角色、精确版本、权限和消息预览。 | 版本和状态由程序读取；预览是**消息截取，不是新做的 AI 总结**，其中可能含已有 AI 消息。 |
| **已发布来源关系** `committed_lineage` | 哪个 Context 版本派生自哪些版本。 | **否**，从已提交的关系记录构建。 |
| **执行结果概况** `stable_results` | Run 状态、错误、最终 checkpoint、工作区结果及执行锚点。 | **非 AI 汇总**，读取执行记录；执行结束不等于任务完成。 |
| **工作区、权限与预算** `workspace` / `grant` / `budget` | Git 状态、隔离与采纳记录、授权范围、预算和实际用量。 | **否**，来自数据库、工作区读取和程序计数。 |
| **待决定／待补充事项** `pending_decisions` / `information_requests` | 已登记的待办决策、补充请求和状态。 | 直接读取已有记录；问题或理由正文可能来自此前 AI 提案。 |
| **Worker 结果** `worker_results` | 已完成 Worker 的状态及结果正文。 | **混合**：AI 分析类 Worker 的结论属于 AI 产物，状态字段由系统记录。 |
| **演化策略** `evolution_strategies` | 启用的全局／工作区策略目录、适用信息及正文版本引用；策略正文按精确引用保存、按需读取。 | **否**，冻结已配置材料，不现场总结；正文最初可能由人工编写或 AI 协助编写。 |
| **输入时点与因果历史** `intervention` / `pre_input_state` / `causal_chain` | 输入前的局面、用户当时可见线索、历次输入与原快照的关联。 | 结构由程序采集和关联；引用内容保留其原始来源，可能包含此前 AI 产物。 |
| **资格、恢复及版本信息** | 完成准入 `completion_admission`、完成资格 `completion_eligibility`、恢复机会与等待原因、派生读取句柄和待履行义务、观察编号、版本、哈希及各实体版本等。 | 由规则检查、已有记录和程序计算生成；资格检查可能引用此前 AI 验证结论，冻结时不重新调用 AI 验证。 |

## 新增证据的来源

`task_delta` 是带精确来源身份和版本的证据清单，不是将全部新增信息交给 AI 后生成的一段摘要。

| 证据类型 | 正文来源 | AI／非 AI |
| --- | --- | --- |
| **用户修订** `user_revision` | 用户输入或已提交任务约定。 | 原文／已有记录，**非现场 AI 总结**。 |
| **执行结果** `run_outcome` | 系统记录执行状态，并摘取 Agent 最后的自述。 | **混合**：状态是系统事实，自述是 AI 内容。 |
| **测试** `test` | 程序解析工具返回的测试报告。 | **非 AI 总结**；测试结果来自工具输出。 |
| **工作区变化** `workspace` | 版本、指纹、文件变化和采纳状态。 | **非 AI 总结**。 |
| **产物** `artifact` | 文件记录、实际文件调用审计或显式产物列表。 | 记录由程序生成；**文件内容本身可能由 AI 写成**。 |

Agent 说“完成了”只算自述。系统将这种自述标记为 `asserted`；任务进度中的 `completed` 必须有相关独立领域证据支持，不能仅凭 Run 的 `success` 状态或 AI 自述判定。

## 内容在什么时候生成

1. **冻结前**：用户输入、任务约定、执行与工具结果、已发布的 Context 及来源关系持续形成；后台任务记忆模型根据此前证据归纳进度，程序校验后发布。新观察读取的是当时已经发布的进度版本。
2. **冻结时**：程序准备精确 Context 内容及工作区状态，在一致事务中采集业务记录、枚举新增证据、关联输入和因果历史，生成版本及哈希并保存 Observation。此过程不调用 AI。
3. **冻结后**：Patrol 基于快照分析和提出安排；需要时派发 Curator 等认知工作。新的认知结果独立持久化，并与基础快照组合为模型读面，不回写基础 Observation。模型读面可以追加 `worker_results` 和 `expansion_assessment`。

页面上的“结论”从冻结证据的摘要或标题截取，“下一步”读取已提交决策的理由。展示时不额外调用 AI，但被截取的正文可能来自上游 AI。

## 实现依据

- [Observation 字段与输入合同](../backend/app/desktop/patrol/agent_loop/schemas.py)
- [Observation 采集、组装与冻结](../backend/app/desktop/patrol/agent_loop/observations/observation_capture.py)
- [新增领域证据清单](../backend/app/desktop/patrol/agent_loop/task_progress/source_reader.py)
- [执行自述、测试和工作区结果提取](../backend/app/desktop/runs/evidence/run_outcomes.py)
- [后台任务记忆模型与发布流程](../backend/app/desktop/patrol/agent_loop/task_progress/runtime.py)
- [独立认知补充与模型读面](../backend/app/desktop/patrol/agent_loop/decisions/decision_context.py)
- [Observation 页面摘要](../backend/app/desktop/patrol/agent_loop/observations/observation_query.py)

[返回 README](../README.md) · [返回中文 README](../README.zh-CN.md)
