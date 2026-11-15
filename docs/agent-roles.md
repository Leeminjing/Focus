# Agent Roles

## 普通会话模式

| 角色 | 业务能力 | 配备的工具 |
| --- | --- | --- |
| **Main Agent：`main`** | 承接当前会话的用户目标，理解需求、制定计划、修改代码、执行验证、汇报结果；按需创建和管理协作 Agent。 | `read_file`、`discover_files`、`write_file`、`edit_file`、`shell`、dictincet-browser工具、`web_search`、`web_fetch`、`describe_skill`、`spawn_agent`、`spawn_teammate`、`spawn_worker`、`wake_agent`、`wait_for_swarm`、`send_message`、`approve_plan`、`respond_shutdown`、`publish_task`、`list_board_tasks`、`list_patrol_agents`、`read_patrol_agent_history`、`read_swarm_agent_history` |
| **临时子 Agent：`spawn_agent`** | 独立完成一次明确子任务，例如调查代码、分析问题、实施修改；将结果返回父 Agent，结束后销毁。 | `read_file`、`discover_files`、`write_file`、`edit_file`、`shell` |
| **持久队友：`teammate`** | 持续负责一条委派工作线，独立实施并汇报；向 Main 请求计划审批或结束工作，可被再次唤醒继续。 | `read_file`、`discover_files`、`write_file`、`edit_file`、`shell`、dictincet-browser工具、`web_search`、`web_fetch`、`send_message`、`request_plan_approval`、`request_shutdown` |
| **持久工蜂：`worker`** | 从任务板认领明确任务，完成实施与验证，提交任务结果；可持续领取后续任务。 | `read_file`、`discover_files`、`write_file`、`edit_file`、`shell`、dictincet-browser工具、`web_search`、`web_fetch`、`send_message`、`claim_task`、`complete_task` |
| **装配 Agent** | 配置 Focus 自身，包括 MCP、Skill、插件和配置文件；支持全局范围或指定工作区范围。 | 与 Main Agent 相同 |
| **Session Patrol：`patrol` / `standard`** | 按用户编写的指令和冻结上下文执行独立任务；支持重跑、从 checkpoint 继续和恢复中断。 | `read_file`、`discover_files`、`write_file`、`edit_file`、`shell`、dictincet-browser工具、`web_search`、`web_fetch` |
| **Context Curator：`context_curator`** | 持续跟踪根会话的稳定版本，保留目标、约束、决策、证据和未完成事项，整理为可接手工作的精炼 Context。 | 无 |

## Patrol Loop 模式

| 角色 | 业务能力 | 配备的工具 |
| --- | --- | --- |
| **Portfolio Patrol** | 统筹整个工作区的 Context 集合；处理用户新输入，决定继续、派生、恢复、汇合、暂停或取消工作，分配权限、采用成果、请求验收和申请完成。提案由 Kernel 校验提交。 | `web_search`、`web_fetch`；协议能力：`reads`、`compression_candidate`、`needs_evidence`、`decision` |
| **Context 执行 Agent：Main/Lead** | 在指定 Context 中执行获准任务，包括需求探索、开发、问题调查、修复、审查、测试和集成；产出文件与执行证据。 | `read_file`、`discover_files`、`write_file`、`edit_file`、`shell`、dictincet-browser工具、`web_search`、`web_fetch`、`describe_skill`、`send_message`、`approve_plan`、`respond_shutdown`、`publish_task`、`list_board_tasks`、`list_patrol_agents`、`read_patrol_agent_history`、`read_swarm_agent_history` |
| **Lane Curator：`lane_curator`** | 承接 Patrol 委派的策展与工作规划任务，提出工作线候选，说明职责、目标、问题和完成条件，交给 Patrol 选择。 | 无独立工具；生产路径使用 Retrieval Cognitive Planner |
| **Retrieval Cognitive Planner** | 在授权索引中规划查询、选择并精读证据、读取策略，据此生成有来源支持的 `WorkContextDraft`；是 Lane Curator 当前生产使用的规划实现。 | 无直接执行工具；协议能力：`queries`、`candidate_ids`、`strategy_reads` |
| **Completion Verifier** | 根据当前 Mission 的验收标准，逐项审查已有成果与证据，判断满足、不满足或未知，指出缺失证据；不自行执行测试或宣布完成。 | 无 |
| **任务记忆沉淀模型** | 将本轮新增信息与既有任务记忆合并，更新成果、义务、阻塞、冲突和未决项，为下一轮保留连续的任务状态。 | 无 |

工具按权限和环境条件装配；未核实昵称的动态扩展未列入。`shell` 统称 `powershell`、`sh`、`cmd`、`bash`；dictincet-browser工具统称 focus-dictincet-browser 提供的工具。
