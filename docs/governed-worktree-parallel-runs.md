# Loop 有界并行与工作树治理

Loop 根据实际依赖安排工作：基础和共享接口先验证，独立模块可在同一授权波次并行，隔离成果逐个采用后再集成。Patrol 使用现有 actions，required expansion 可只选择当前阶段安全的子集。

ContextRunPool 按 Run 数量预留全局/每 Loop 剩余容量，并允许当前 running Round 补位。达到自身上限的 Loop 不再挡住后面的可运行 Loop。Dispatcher 继续执行当前授权、版本、认领和幂等检查；正常资源等待不消耗启动重试。

每个并行 Writer 使用独立 Git worktree。规划前后核对 Round 工作区版本、slot fingerprint、当前 HEAD/dirty 和活动 Writer 的共同 commit；基线漂移时保持有原因的排队，重新观察确认后才使用新基线。非 Git、脏目录或隔离未授权时沿用串行路径。配额不足而选择权威 Writer 后，本次其余 Writer 全部排队，配额释放不能把同一波次变成权威/隔离 Writer 混跑。规划器不初始化、提交、stash 或 reset 用户仓库。

已有或未采用成果保留；采用沿用 Kernel、目标 revision/fingerprint 和 Git patch check/apply。未新增调度表、计划 schema、action、依赖图引擎或外部依赖。

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
