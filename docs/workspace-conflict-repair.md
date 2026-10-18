# 成果冲突修复

Patrol 可以在当前授权内安排常规代码冲突的隔离修复。Kernel 继续控制执行与采用，workspace 服务继续控制真实文件、版本、指纹和恢复；修复提示不产生权限。

## 冲突边界

采用记录保留 `reason` 并增加 `kind`：

| kind | 处理 |
| --- | --- |
| `code_overlap` | 仅发生在持久写入意图之前的明确 Git patch check 重叠；继续检查修复资格 |
| `target_version_changed` | 目标逻辑版本变化，需要重新观察 |
| `user_changes` | 来源或目标与已登记状态不符，保留现场 |
| `execution_uncertain` | 未分类错误、意图失效、写入/恢复失败，或目标既非前态也非后态；保持原恢复等待及预留 |

历史记录缺少分类时不推断为代码重叠。具有非空 `application_intent` 的冲突绝不转入自动修复。

修复资格要求有效的 continue、isolate、adopt 和 write 授权、完整共同基线、当前物理指纹，以及成功来源 Run 的任务指令。目标必须能从共同基线沿已经 adopted 的补丁前后指纹连续追溯到当前状态。登记过的新指纹本身不能证明用户修改的意图。二进制、符号链接、子模块及超过 128 KiB 的上下文保守地交回恢复流程。已修复成果再次冲突不递归自动修复。

## 执行与采用

1. 采用失败仍保存 conflict 和来源锚点；有完整修复资格时，该次处理决策收口并开启后继观察，Patrol 获得带内容 hash 的 `repair_context` 引用。补丁正文只进入持久上下文和执行 Directive。
2. Patrol 判断双方意图可兼容后，以 `continue_context.workspace_repair = {adoption_id, phase: "repair"}` 独立提交修复。涉及产品取舍时请求用户决定。Kernel 校验冻结引用、当前版本、权限、预算、Context 和活动 Run，同一冲突只授权一次修复尝试。
3. 规划器从当前目标生成私有 index 的快照和不移动用户分支的 Git commit 对象，创建新的受管 worktree。用户的 HEAD、index、当前文件和原冲突成果均保留。该 Directive 不能回退到权威目录执行；配额不足沿用排队。
4. 修复 Run 在隔离目录保留双方行为并运行验证。成功结算后，Patrol 以独立 Context 提交 `phase: "review"`，精确引用修复 `slot_id/revision`，并授予 read-only 模式。独立 Run 检查集成差异、测试和回归风险。
5. Patrol 检查实际验证和审查结论后，对新 slot 提交现有 `adopt_workspace_result`。Kernel 和采用服务再次核验当前授权、原冲突上下文、目标版本、成功修复 Run 和绑定当前指纹的独立只读审查 Run。Run 成功结算只证明执行完成，语义上是否通过仍由 Patrol 依据实际报告决定。
6. 新成果仍走原 patch check、持久采用意图和崩溃恢复。登记成功后，原冲突记录增加 `repaired_by`，旧来源保留，原锚点标记 `repaired`，避免继续阻塞完成资格。新成果和审查锚点按原采用流程登记 adopted。

暂停、撤权、过期授权、来源/目标漂移或陈旧审查均不能产生新的采用效果。已有持久采用意图的恢复沿用原身份和补丁证据，不重新生成修复补丁。

## 验证入口

`backend/tests/test_workspace_conflict_repair.py` 使用真实临时 Git worktree、独立 PostgreSQL 和确定性文件 Worker，覆盖可追溯冲突、未知用户修改、隔离修复、独立只读审查、新成果采用、旧成果保留和重放。

该回归验证物理执行与治理链，不作为真实模型已经正确判断产品语义或测试通过的证据。

两路代码审查分别发现并修正了快照重试身份不稳定、目标任务意图使用采用理由的问题；复核没有新增阻断项。

新增修复链 14 项回归通过；原采用、恢复、租约、治理和 Worker 恢复 48 项，加上未知用户修改的最终复测一项，合跑 49 项通过。按用例去重为 62 项相关回归通过。另完成 12 次真实 Git 采用探测、相关 Python 编译和 `git diff --check`。

全量测试未通过：已有 `workspace_coordination/reservations.py` 缺少声明式文件头；独立运行其余架构检查还发现已有任务记忆消费者依赖展示 journal 的断言失败。扩展合同测试的旧 `SimpleNamespace` 夹具缺少 `evolution_strategies`，也无法通过当前合同校验。这些文件和行为未在本次扩展修改。综合回归还遇到过 PostgreSQL `WinError 121`，发生在测试数据创建前。
