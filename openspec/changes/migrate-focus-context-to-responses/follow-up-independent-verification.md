# Independent verification: migrate-focus-context-to-responses

> 后续处置：用户要求“全部修复”，V1／V2／V3 均已解决，见 follow-up-repair-verification.md。本文保留修复前的审计与反例事实；原 strict xfail 已转为 backend/tests/test_semantic_eligibility_repairs.py 和 test_contract_history_repairs.py 的正式回归。

2026-10-01。当前用户调用 openspec-verify-change；复用用户先前明确选择的 change。此次只验证实现、增加独立复现并更新验证记录，没有修改应用实现，没有提交、推送或归档。

## Scorecard

| Dimension | Result |
|---|---|
| Completeness | 71/71 tasks checked；40 requirements、83 scenarios 均有映射；59 个不同测试引用可解析 |
| Correctness | 2 WARNING、1 SUGGESTION；通过既有专项不能证明下述边界正确 |
| Coherence | 合同编译、workflow、delivery、history、Provider 职责分开；压缩后合同镜像尚未遵守单一权威 |

前 37 个 requirements／69 个场景的旧映射和 F1–F6 修复保留为历史依据。此次重新读取六份规格及 proposal／design／tasks，核对当前实现落点和具名测试，重点独立审查新增合同生产及相邻语义、压缩、展示路径。未将旧 621 项测试结果冒充本次重新执行。

## Findings

### V1 — WARNING / P1: reference_only 的任务命题资格没有在宿主校验层执行

位置：`backend/app/desktop/agent_loop/context_expansion/semantic_indexer.py:295`，相同问题还存在于 `segment_projection.py:158`、`interpretation_record.py:226`。

v7 fallback 已排除知识正文，但模型生成的 drafts 仍直接进入只检查引文与 claim-support verdict 的 grounding；并不验证支持消息的 semantic_policy。独立复现使用真实 compile_handoff 生成的 reference_only 知识，将只引用该知识的 draft 标为 decision／confirmed，并提供 supported verdict，索引接纳该 confirmed decision，零 rejection。独立 claim verifier 能证明“引文支持正文”，不能证明“该来源有资格定义任务决定”。因此 prompt 中的资格约束并非宿主边界。

对应：Selection eligibility applies across the entire indexing pipeline；design decision 4／14；tasks 4.3、12.5。

建议：在共享宿主校验边界传入 semantic_policy／来源，明确任务命题、关联证据结果和参考用途的允许组合，拒绝仅由 reference_only 支持的新任务决定；局部 projection、整体 interpretation 与最终 index 采用相同规则，并纳入相关版本 fingerprint。补有意违反 prompt 的模型输出用例；不要仅要求模型遵守 prompt，也不要误删用于验证的正常工具证据。

### V2 — WARNING / P2: 压缩删除 typed 合同后保留独立 task_contract 镜像

位置：`backend/packages/harness/focus/agents/commitment/middleware.py:223`、`:226`，`handoff.py:95`；历史手术入口 `focus/agents/compression/gate.py:168`。

apply_compression_ranges 可以删除合同，重建 messages／execution_items，但不更新 task_contract 通道。删除后 validate_contract_mirror 返回 None，非空旧 scalar 仍触发 middleware 的跳过分支。新 V2 checkpoint 因而能够出现“没有 canonical typed 合同，但镜像仍表示已有合同”的状态；它被当作 legacy string-only 状态放行，无法判断是否由本次历史手术造成。独立复现使用实际压缩函数、add_messages 和 middleware._run，确认未清除镜像、未拒绝分歧且返回 None。

对应：Confirmed task contracts are produced as typed domain items；compatibility bridge 的单一历史权威；design decision 9／14；tasks 12.4。

建议：使合同镜像随历史重建与恢复按保留 canonical Items 一起更新；对真正 V1 string-only 兼容状态建立明确区别，而非仅凭“找不到 typed 消息”推断 legacy。覆盖删除、摘要替代、恢复以及之后的新 Run；没有 typed 合同时清空镜像或依明确可解析的合同来源规则恢复，不能留下独立权威。

复现是函数／middleware 边界证据，未宣称已驱动完整桌面压缩批准、持久化、下轮 admission。当前 checkpoint 更新合同表明该组合可达；需要正式 graph 回归补齐。

### V3 — SUGGESTION / P2: 替换式合同展示未保留触发输入的附件信息

位置：`backend/packages/harness/focus/history/display.py:26`、`:35`。

display 隐藏原触发消息，展示合同消息副本并复用其 ID。合同没有 files 元数据，原输入的附件因此不出现在该 display 行。独立复现使用含 files 的 trigger，经 compile_handoff 和实际 events.serialize_value 后，得到一个合同行且 files 缺失；canonical 输入的附件仍在。

这是展示缺口，不是 Provider 或持久化数据丢失；旧混合交付路径本来也没有保留 files，不能描述为新引入的历史回归。建议明确合同替换行如何呈现原选材料：保留只读附件元数据或保留独立原输入行，并补 Reader／live snapshot 一致性与材料显示测试。该决定无需改变 canonical 身份。

## Reproduction evidence

`verify-reproductions/test_follow_up_boundaries.py` 保存三项预期边界断言，均为 strict xfail。实际结果 **3 xfailed**，不是 3 个通过的行为测试；修复后会 XPASS，必须移除对应标记并纳入正式回归。日志 `.tmp/independent-boundary-reproductions.txt`。

另一个展示 ID／压缩 ID 疑点已排除：`DesktopService.get_checkpoint_messages` 向压缩面板提供 canonical IDs，因此不能仅凭 conversation display 的替换 ID 宣称实际压缩选错消息。未列为 finding。

## Fresh validation

| Batch | Actual result | Log |
|---|---|---|
| 合同、实际 Lead 批准恢复、隔离 PostgreSQL publication／checkpoint、旧承诺流程、事件、Reader、Inbox、Responses、typed history 与安全专项，15 个模块 | 181 passed，9.74s | .tmp/independent-handoff-regression.txt |
| interpretation、evidence corpus、缓存兼容、增量索引、派生、Reader、Curator，7 个模块 | 133 passed，77.66s | .tmp/independent-semantic-regression.txt |
| desktop conversation events／reconciler／live consistency／render budgets／DOM／run-ui／compression／Curator，9 个文件 | 72 passed，352ms | .tmp/independent-display-regression.txt |
| 上述独立边界复现 | 3 xfailed，2.17s | .tmp/independent-boundary-reproductions.txt |
| OpenSpec strict、git diff --check | passed | 本次工具输出；换行警告不是 diff error |

各批次有重叠，不相加为全量测试数量。Python 使用 `C:/Develop/python/python312/python.exe`，PYTHONPATH 为 repo 与 backend/packages/harness。pytest 唯一警告为本机 .pytest_cache 无写入权限，不影响测试断言。

具名引用核对仅证明测试函数存在，不能证明 83 个生产场景全部正确。原映射中已删除的 ScopedContextMiddleware 是历史落点；其现行职责已转入 WorldStateMiddleware._prepare／prepare_scoped_contexts，详见 repair-verification.md；不将它误报为能力未实现。keyword 类型的实现线索不按 AST 函数名校验。

## Limits and assessment

本次没有执行全量 backend、真实 OpenAI／DeepSeek smoke 或交互 Electron E2E。双 Provider 离线 wire／SSE 和隔离真实 PostgreSQL 组件测试通过。既有测试主要覆盖没有错误 drafts 的 semantic fallback、完整保留合同的恢复和无附件 display，因此未发现上述边界。

没有缺失实现或未完成 checkbox 导致的 CRITICAL；有 2 个应修复的 WARNING 和 1 个展示建议。本次不能写成“全部验证通过”；建议先处理 V1／V2 再归档。原 F1–F6 的历史 resolved 状态不被此次 findings 覆盖。
