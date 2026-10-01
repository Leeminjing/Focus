# Verification Report: migrate-focus-context-to-responses

> 当前修复复核：用户“全部修复”授权的 V1／V2／V3 均已解决，**76/76 tasks，0 个未解决发现**。正式回归及最终 222／151／153／72 项专项结果见 [follow-up-repair-verification.md](follow-up-repair-verification.md)。下方独立审计和前期报告保留为历史证据。

> 独立审计初始结果（修复前，2026-10-01）：71/71 checked，40 Requirements／83 场景映射存在；发现 **2 WARNING + 1 SUGGESTION**。当时复现为 3 strict xfail；专项 181／133／72 不覆盖这些边界。完整原始发现见 [follow-up-independent-verification.md](follow-up-independent-verification.md)。

> 2026-10-01 补充实施：用户批准的 tasks 第 12 节已完成，当前 71/71，40 Requirements／83 场景。新增 14 场景以新测试映射验收，结果见 follow-up-verification.md。本报告正文中的 63/63、37 Requirements、69 场景及旧测试结果仅适用于原迁移／F1–F6 修复，不能代替新增验收。

2026-10-01 修复复核：原审计 **1 CRITICAL + 5 WARNING 全部解决**，修复范围 **0 个未解决问题**。

| Dimension | Status |
|---|---|
| Completeness | 63/63 tasks；37 个原 Requirements 的实现映射保留；69 个场景引用可解析，9 个新增修复回归映射 |
| Correctness | 大范围专项 621 通过，最终专项 70 通过；六项反例均有正式回归 |
| Coherence | 唯一最终准备端口、宿主来源、canonical V1 读取、Provider 前缀证明和共享 error repair |

初始报告见 verify-report-initial.md/json，原反例见 verify-reproductions.json。本报告更新原发现的处置状态；不是重新宣称全量 backend、所有生产场景或真实 Provider 均已通过。未执行 archive。

## Repair verification

2026-10-01。用户“全部修复”授权后修复原审计 F1–F6；改应用代码前成功调用 Context7，依据记录见 repair-preflight.md。没有修改已发布 Revision，没有归档 change。

## 结果

六项原审计问题均已修复并通过具名回归；当前修复范围没有未解决的 CRITICAL/WARNING。原审计的失败输出保留在 verify-reproductions.json，原报告与 JSON 原样保存为 verify-report-initial.md/json。

| Finding | 根本修复 | 回归证据 |
|---|---|---|
| F1 | 删除独立 ScopedContextMiddleware；压缩之后由唯一最终准备端口补齐冻结引用，预算也使用同一端口预览 | test_request_context_repairs.py::test_compression_resume_restores_frozen_context_before_first_wire，真实 Lead 工厂、已有冻结引用 checkpoint、interrupt/apply、首个实际 SDK 请求 |
| F2 | Curator copy/compose 均保留宿主 origin/authority/source refs；Provider 依据来源投影为 user | test_history_source_repairs.py::test_curator_system_role_cannot_promote_source_authority，OpenAI/DeepSeek × copy/compose/mixed |
| F3 | RunAdmissionService、兼容 Registrar 和服务执行装配共用 bind_run_inputs；只绑定新输入 | test_history_source_repairs.py::test_admitted_main_input_has_host_provenance_in_execution，隔离数据库双来源；test_desktop_poc.py::test_unified_pipeline_main_run_end_to_end，真实 HTTP 到 checkpoint |
| F4 | 实际 wire 保存前缀证明；校验失败时核对源 typed hash，原子重建 messages/Items/snapshot，再准备当前运行引用 | test_request_context_repairs.py::test_actual_prefix_change_rebuilds_authority_and_preserves_old_checkpoint，image/run_scope/unchanged × memory/PostgreSQL；拒绝缺失证明与基础指令变化；数据库提交后采样前故障、重新构图恢复；旧 checkpoint 不变 |
| F5 | V1 semantic 使用 canonical checkpoint；同身份同协议内容才补回已知来源，原有显式 repair 元数据优先 | test_history_source_repairs.py::test_v1_semantic_uses_canonical_checkpoint_without_mutation，checkpoint-only/存储 UI 投影；runtime 排除、reference 保留、旧记录/hash 不变 |
| F6 | compression/interruption 共用 error repair 构造；空删除标记不再拆开尚存并行结果 | test_history_source_repairs.py::test_compression_parallel_exchange_has_error_repair_and_real_evidence；test_unified_run_orchestration.py 中断因果与 publication 回归 |

模块边界：history 负责无损 authority/repair/reducer；context 负责最终准备与显式分支重建；models 负责 wire 前缀证明与 Provider 投影；Desktop admission 负责可信来源。Harness 不反向依赖 Desktop ORM。修改文件的声明式头部已核对；没有恢复多处 prompt 拼接或新增第二套可写历史。

## 实际验证

- 大范围专项：**621 passed in 279.82s**，覆盖 typed history、WorldState/inbox、Responses、语义派生/索引、Curator、压缩、Run admission/dispatch/publication、Desktop HTTP、材料/must-view、权限/sandbox、配置与架构。日志 .tmp/responses-repair-regression.txt。
- 最终修改后专项：**70 passed in 7.19s**，包含全部新增反例、attempt audit、canonical Reader、冻结/inbox、压缩与真实 wire。日志 .tmp/responses-repair-final-focused.txt。
- 实际 HTTP Main 来源核对与新 history 用例：**12 passed in 9.04s**。日志 .tmp/responses-repair-admission.txt。
- 场景映射：69 个原场景引用均可解析；9 个场景补上具名 repair_regression_tests。新增引用证明这些组合路径，不能替代生产环境全部场景验收。
- OpenSpec strict、git diff --check 与修复模块语法/声明式头部/依赖方向检查：通过。

这些测试组有重叠，不能相加宣称全量通过。大范围批次期间完成的最后 V1 兼容增强和回归补充由最终 70 项专项覆盖；不宣称所有生产路径均已覆盖。

## 限制

本轮没有重跑完整 backend 套件；此前全量的 13 项独立 HEAD 基线失败仍是仓库背景问题，本次六项审计修复没有宣称解决这些无关失败。此前 desktop 180 项通过是原验证证据，本轮未修改前端也未重复该批次。OpenAI/DeepSeek live smoke 和交互 Electron E2E 本轮未运行；数据库 checkpoint 与故障注入使用隔离测试环境，不代表用户生产进程实验。临时原复现脚本依赖已删除中间件，保留作历史证据；自动回归入口使用以上正式 tests。

仓库 .gitignore 的 openspec/ 规则保持不变；本次远程发布明确纳入本 change 的 artifacts。任务总计 63/63，未执行 archive。
