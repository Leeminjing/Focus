# Loop 有界并行与工作树治理

Loop 根据实际依赖安排工作：基础和共享接口先验证，独立模块可在同一授权波次并行，隔离成果逐个采用后再集成。Patrol 使用现有 actions，required expansion 可只选择当前阶段安全的子集。

ContextRunPool 按 Run 数量预留全局/每 Loop 剩余容量，并允许当前 running Round 补位。达到自身上限的 Loop 不再挡住后面的可运行 Loop。Dispatcher 继续执行当前授权、版本、认领和幂等检查；正常资源等待不消耗启动重试。

每个并行 Writer 使用独立 Git worktree。规划前后核对 Round 工作区版本、slot fingerprint、当前 HEAD/dirty 和活动 Writer 的共同 commit；基线漂移时保持有原因的排队，重新观察确认后才使用新基线。非 Git、脏目录或隔离未授权时沿用串行路径。配额不足而选择权威 Writer 后，本次其余 Writer 全部排队，配额释放不能把同一波次变成权威/隔离 Writer 混跑。规划器不初始化、提交、stash 或 reset 用户仓库。

已有或未采用成果保留；采用沿用 Kernel、目标 revision/fingerprint 和 Git patch check/apply。未新增调度表、计划 schema、action、依赖图引擎或外部依赖。

## 采用崩溃恢复（2026-10-17）

采用分为两个提交阶段。第一阶段锁定 source/target slot，核对 revision、fingerprint 和活动租约，生成 binary patch，并在临时文件树上用目标仓库的 HEAD、index 和配置预演。随后独立提交 `workspace_adoptions.application_intent`：协议版本、补丁字节（base64）、SHA-256、两个根路径、来源 fingerprint、目标前态和预期后态 fingerprint。迁移 `f36d7e8f9012` 为旧记录填入空对象，不猜测历史文件效果。预演临时复制完整工作树（包括 ignored/untracked 文件），因此需要相应临时磁盘空间；不复制 Git 对象库。

第二阶段重新按稳定顺序锁定两个 slot，核对持久身份和补丁 SHA-256，并按目标物理 fingerprint 判断：

| 目标状态 | 恢复行为 |
| --- | --- |
| 精确匹配后态 | 文件效果已完成，只登记 adopted、推进 revision、保留来源，绝不重复 apply |
| 精确匹配前态 | 在 owner 仍有权执行时应用已保存的补丁；不从可能变化的来源重新生成 |
| 两者都不匹配，或意图/版本失效 | 保存 conflict，保留文件、补丁证据和 slot 预留，等待核验 |

意图提交后，来源和目标均拒绝新租约、fingerprint 推进和物理清理；迟到 Run 结算返回 `adoption_recovery_pending`，不能抢先推进版本。完成登记后解除预留。重复请求必须保持相同 slot/revision 身份，同来源 revision 不能绑定第二个采用请求。

`AgentLoopRecovery.reconcile()` 在普通调度恢复前扫描意图，先登记已完成的文件效果，再由 `LoopWorkspaceAdoptionService` 收口 action、decision、anchor 和领域结果。用户暂停或 owner 已失效时不执行尚未发生的文件写入；再次证明目标仍为前态后将该意图标为 rejected，释放预留。已经发生的文件效果照实登记，暂停和 superseded 决策不复活。旧记录缺少意图时只能保留冲突，不能自动推断成功。

强制退出演练使用独立 PostgreSQL 测试库和真实 Git worktree，子进程在真实 `git apply` 返回后立即 `os._exit(73)`。修复前，文件已含文本/二进制结果而 slot revision 仍为 1，重试返回 fingerprint conflict；修复后，启动恢复登记为 adopted，重复和并发恢复后 revision 均为 2。另覆盖应用前退出、数据库提交后退出、暂停、部分落盘、外部编辑、损坏补丁、陈旧版本、迟到 Run 结算、迁移往返、staged/ignored 文件保留和 Git 配置对预期 fingerprint 的影响。

```powershell
$env:PYTHONPATH = "$PWD;$PWD\backend\packages\harness"
python -m pytest backend/tests/test_workspace_adoption_recovery.py backend/tests/test_git_workspace_adoption.py backend/tests/test_workspace_coordination.py backend/tests/test_loop_workspace_adoption_governance.py backend/tests/test_loop_worker_recovery.py -q
```

扩展验证的限制：`test_loop_worktree_parallel_runs.py` 的 12 项测试在采用之前因缺少冻结 Observation 失败，使用 HEAD 原实现复跑其中一项得到相同失败；`test_agent_loop_dispatch.py` 的一项派发测试返回两个空领取结果，使用 HEAD 原实现复跑仍相同。这些失败不作为本次采用恢复验收通过的证据。

上述五个核心文件的 48 项回归通过；暂停释放未应用意图的最终行为又单独通过全部 12 项采纳治理测试。`git diff --check` 和相关 Python 文件编译检查通过。未执行全量测试或重启已安装应用。

合入本地 main 前已将采用意图迁移接在新增的 Run outbox 迁移 `f36d7e8f901a` 之后，确认迁移 head 唯一；基于合并后代码重跑上述五个文件及 `test_run_outbox_migration.py`，49 项全部通过。

## 验证（2026-10-09）

- 两项已复现失败转为正式回归：基线变化后的同轮补位、配额动态释放后的串行回退；修复后通过。另验证准备期间用户编辑权威文件时不交付 Run、认领不耗 attempt，已准备目录仍有持久身份。
- 真实临时 Git/PostgreSQL 完成基础 Run 准备 baseline、模块 Run 重叠与补位、池重建、逐个采用、只读集成 Run。集成经历 admission、lease、执行锚点、实际文件读取与结算；重放结算仍保持唯一后继 Round。
- 16 个相关测试文件覆盖容量、派发、工作区、adoption、控制/锁顺序、Round 后果、实时投影和认知合同，共 129 项。综合批次 128 通过，1 项在创建测试数据前发生 PostgreSQL 连接 `WinError 121`；该项针对性重跑通过，按用例取最新结果为 129 通过、0 失败。
- 既有四类真实 Patrol 模型演练通过；本次未改模型引导。OpenSpec 严格校验与 `git diff --check` 通过。没有运行全量测试或重启已安装应用。

核心回归入口：

```powershell
$env:PYTHONPATH = "$PWD;$PWD\backend\packages\harness"
python -m pytest backend/tests/test_loop_worktree_baseline_governance.py backend/tests/test_loop_worktree_parallel_runs.py backend/tests/test_loop_runtime_pools.py -q -p no:cacheprovider --basetemp=tmp/worktree-check
```

OpenSpec artifacts 依照仓库 `.gitignore` 保留在本地 `openspec/changes/enable-governed-worktree-parallel-runs`，本说明与产品代码、正式回归测试一同入库。
