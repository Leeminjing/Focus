# 旧 Context Loop 启动线退役

2026-11-15，用户明确决定废弃“将已有 Context 和初始 Run 接入自动 Loop”的旧启动方式，并清理专属接口、执行代码及测试。此前迁移文档中的“保留业务；入口待承接”由本决定取代。

## 现行启动流程

自动 Loop 统一使用 Workspace Patrol：

1. 绑定有效的本地工作区。
2. 通过 `POST /desktop/api/agent-loops/workspace/{workspace_id}/inputs` 提交输入，首次发送确定访问模式；Loop 预算固定无上限，不接受预算预设或调整。
3. 服务端持久受理首输入，建立工作区 Loop、Context、授权、预算账本和首轮观察；Patrol 根据输入与执行事实继续组织工作。
4. 后续输入仍走同一工作区入口。目标、工作边界和完成检查由统一输入处理。
5. Loop 终态后，需要继续工作时显式调用 `POST /desktop/api/agent-loops/workspace/{workspace_id}/restart` 创建工作区后继。

“重新启用”是工作区 Patrol 的后继流程，不再将普通任务 Context 或初始 Run 转换为另一种自动 Loop。

## 移除范围

以下接口不再公开，也不提供旧启动请求的兼容转换：

| 已退役接口 | 原用途 |
| --- | --- |
| `POST /desktop/api/agent-loops` | 从已有 Context 和初始 Run 创建旧 Context Loop |
| `GET /desktop/api/agent-loops/by-context/{context_id}` | 查询旧启动流程所需的 Context 归属 |
| `GET /desktop/api/agent-loops/activation-eligibility/by-context/{context_id}` | 检查旧启动及后继激活资格 |
| `POST /desktop/api/agent-loops/{loop_id}/override` | 从旧表单覆盖 Mission |
| `POST /desktop/api/agent-loops/{loop_id}/missions` | 从旧表单激活新的 Mission |
| `POST /desktop/api/agent-loops/{loop_id}/interventions` | 提交旧 Context/Portfolio 意见 |

对应的创建请求合同、激活资格判断、初始 Run 接管及专属 Mission bootstrap 移除。专门验证这些旧行为的测试随之删除；原先仅借用旧启动方式准备数据的共享测试，改为验证 Workspace Patrol 下的相同行为。

## 保留的共享能力

- 普通 Context 的创建、编辑、派生、查看和 Main Run，以及 Session Patrol。
- Workspace Patrol 的统一输入、首线程准备、工作线组织和终态后继。
- Mission 的工作区输入处理、任务进度、观察、执行事实和完成验证。
- 权限、固定无上限预算与用量统计、授权收窄及撤销、暂停/继续/停止、人工 gate 和等待恢复。
- Run 调度、工作区结果采用、上下文压缩、Live 状态、诊断和历史查询。

共享模块是否保留，以 Workspace Patrol 及上述现行调用方为依据。移除 `POST missions` 不代表删除 Mission 领域或只读 Mission 历史；移除旧 intervention 接口也不代表删除工作区输入使用的受理与生命周期记录。

## 历史数据与升级边界

旧 `context_loop` 不再创建或继续自动调度。已有历史记录保留供读取，不自动转换为 `workspace_patrol`，不为退役而重写已提交的目标、证据或账务。历史迁移与旧激活账务记录的必要读模型继续保留；保留读模型不代表仍支持创建旧 Loop。

升级后的正常应用启动会在恢复执行前，将尚未终结的旧 Loop 停止并撤销授权，取消待执行工作与消息，释放策展占用。Run 的实际结算和租约释放仍由共享终结流程负责；更新验收模式不执行这项数据收敛。新建 Loop 的应用及数据库默认值均为 `workspace_patrol`。

历史验收报告、截图和基准数据记录当时版本的行为，不作为当前旧接口仍可用的证据。本次改动的验证应同时检查旧接口和专属符号已移除，以及工作区首次输入、连续输入、后继、权限、预算、执行、完成和恢复仍然可用。

## 本次验证

2026-11-15 本地隔离测试结果：核心 Loop 回归 101 项通过，工作区输入、后继、附件、历史、退役迁移及普通 Main 输入相关回归 54 项通过，准入锁顺序与控制竞争回归 10 项通过。另行验证了 Worker 恢复、预算账务、完成资格、Live 投影和旧决策重放拒绝。生产改动通过 Python 语法、未定义名称及未使用导入检查。

PR #14 的 B1 补充修复将普通 Main 准入与结算统一为 Run→Context 锁顺序：先锁幂等赢家或活跃 Main，再锁 Context 并在同一事务复查 Patrol 所有权，随后返回幂等结果或持久化新 Run/dispatch。Loop Run 继续先取得 Loop 控制锁。`test_main_admission_lock_order.py` 的隔离 PostgreSQL 双事务回归共 7 项通过，覆盖带/不带幂等键的新输入、幂等重放与最终 checkpoint 发布并发，以及等待 Context 锁后对 active/paused Patrol 成员的拒绝；同时核对用量、current revision、唯一 outbox 和 dispatch 状态。将同一组三项结算并发测试切回修复前的准入代码时，全部复现真实 `DeadlockDetectedError`，未用异常重试掩盖死锁。

B1 修复后的相关回归另有 52 项通过，覆盖工作区统一输入、durable Run 派发、共享结算、执行所有权、暂停/停止/撤权竞争、Loop 派发锁顺序、Main intake 与普通会话输入。新增测试通过 Ruff 全规则检查，改动生产模块通过 F401/F821，`git diff --check` 通过。

桌面全量测试为 955 项通过、2 项跳过、5 项失败，其中 1 个缩放时序用例单独重跑通过；其余 4 个静态 UI 断言在未修改的 HEAD 快照中同样失败。后端检查还发现既有的 provider 请求窗口、历史事实迁移拒绝降级、文件头规范及任务记忆依赖检查失败，已与 HEAD 对照确认。本次不修改这些无关功能或放宽检查。
