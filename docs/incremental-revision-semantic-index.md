# Revision 继承式语义索引

Focus 为每个精确、冻结的 Context Revision 发布完整且不可变的 Semantic Index。稳定局部证据按权威追加证明继承；内容变化的 Revision 重新做跨 segment 整体解释。完整目标和两类合同精确命中时零模型调用。

```text
Frozen Revision
  ├─ 精确缓存命中 → 完整 Index
  └─ 缓存未命中
       ├─ 无兼容基线 → 全量局部证据抽取
       └─ 有追加证明 → 复用稳定局部 records + 重抽变化尾部
                            ↓
                   完整有序证据目录
                   + 新原文 + 任意需要的冻结旧原文
                            ↓
                   Revision 整体解释 → 联合引文独立验证
                            ↓
                   统一 grounding，全部引用绑定目标 Revision
                            ↓ 原子发布
                   完整 v5 Index + interpretation proof
```

## 复用证明

- 来源必须有 publication receipt。generation 相邻和消息 ID 相同均不构成追加证明。
- 规范化消息身份、顺序、角色、文本／多模态内容和 Tool 结构必须一致。
- segmenter fingerprint 包含实际切分参数；投影 fingerprint 包含 prompt、schema、grounding／verifier 规则和非凭据模型配置。
- 每次 build 冻结有效模型配置；长期复用的 service 也能识别配置变化。一次 build 中途变化则显式阻断。
- 基线查找最多 64 个来源节点。压缩、恢复、curation、定义更新、多来源、跨 Context、缺失记录或合同不兼容均全量回退；scope/stale/完整性错误直接阻断。

## 局部理解与来源绑定

仅局部抽取一次看一个协议闭合 segment，不能引用其他段。`SegmentProjectionRecord` 保存 drafts、精确 quotes、独立 verdicts、隔离结果和可用 provider 元数据；它按 Context 隔离，独立于具体 Revision。这是缓存子步骤，不能代替整个 Index 的理解合同。

`RevisionInterpretationBuilder` 获取全部有序 segment 身份、所有已接受的局部 authority／线索、rejection 和 fallback。冷构建原文全部能容纳时共同提供；增量先给变化尾部原文。综合模型可 `read_segments` 展开任意冻结旧段，在同一次调用中理解多段原文；没有固定相邻窗口、关键词或 top-k 门槛。摘要只引导发现，支持引文必须来自实际提供的原文。

例如 S1 报告 500、S2 怀疑连接池、S3 排除连接池、S4 确认线程池死锁，可在投影阶段生成联合时间线，并用 S2/S3/S4 的原文共同独立验证。Curator 收到已生成的诊断。

每个内容变化版本重跑整体解释，不能因旧正向引文未变而直接继承“最终原因”。不可变 `RevisionInterpretationRecord` 保存完整目录、全部实际提供原文、读取请求、局部／综合 fingerprints、drafts、quotes、独立 assessments 和 dispositions。整个阅读依赖及未引用上下文都参与校验，proof 纳入完整 Index identity。

新 Index 对复用和新 records 使用相同 assembly：重新检查 supports，生成指向目标 Revision 的 evidence refs 和 unit IDs，重建覆盖、fallback/rejections 和 quality。旧 Index 不被修改；degraded 不因继承变成 complete。Curator 和检索始终读取完整 Index，不需要追踪祖先指针。

不同逐字引文可能证明相同 statement 和消息来源。每条 draft 先按自己的 claim key 和独立 verdict 验证，通过后才按最终 unit ID 合并，同时保持 projected 库存唯一。局部与综合 proof 保留各自原始引文和 verdict；不以一个 supported verdict 接受另一条 unsupported／unknown claim。

索引是历史证据库。旧“测试失败”和新“问题已修复”可以同时保留；当前任务状态由 Task Progress 及决策层归纳。索引发布不会创建 Context、更新 Lineage、推进 Task Progress，也不依赖 LoopFact。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| portfolio_index | 冻结读取、共享资源调度、完整 Portfolio 编排 |
| index_inheritance / index_build_contracts | 权威来源链、追加证明与稳定前缀计划 |
| segment_projection / projection_record_repository | 单段投影验证、不可变记录与首个提交赢家 |
| interpretation_inputs | 完整发现目录、任意冻结原文展开、协议闭合与读取上限 |
| revision_interpretation / interpretation_record | 整体解释、联合独立验证、不可变阅读依赖 proof |
| index_assembly / semantic_indexer / semantic_index | 新来源绑定、协议切分、全量覆盖和身份校验 |
| index_model_budget / index_budget_repository / RoleBoundStructuredModel | 逐 attempt 请求准入、短事务共享预算预留与结算、窗口和真实用量 |

读取和发布分别使用短 session；模型调用不持有数据库事务。Portfolio 与 segment 调度共用同一个并发上限，段内顺序处理，不额外放大并发。

## 发布与恢复

所有来源完成局部抽取及整体解释后，在一个短事务中解析 record/index 赢家并校验完整 catalog；失败或容量超限不发布部分批次。竞争可能等待别的事务提交，因此在局部 put 后重新查完整目标赢家，采用其 proof、Index ID 和 catalog。不同 Revision 竞争导致理解依赖与局部赢家不一致时显式阻断，不拼装未经重算的 proof。

完成、失败、重试和取消都记录实际新模型用量，历史 attempt 不重复计费。每个阶段通过 `index_phase` 区分 local、interpretation 和 interpretation_verifier；所有阶段共用 durable admission，不额外获得预算。

预算化模型包装器为每次 invocation 保存独立 attempt 快照。配置检查或外层准入在发送前拒绝时，本次记录为空；真正进入底层模型的失败、结果验证错误和取消仍采集本次真实用量。两次实际调用即使元数据相同也分别计费，不按内容去重。

stage 记录区分构建合同、授权预算及成功／失败结果；重试成功不会重放旧失败结果。旧 stage 保留不改写。

Index／record schema 和完整切分 fingerprint 都属于 record 合同。即使两种切分政策碰巧产生相同的段，也不会在发布时采用另一合同的旧 record。

每个索引 build 在发送请求前，以短事务锁定 Loop 用量，检查真实 active grant 的版本和限制，并扣除已有用量及全部未结算预留。并发实例和显式重试共享这份额度；结束时幂等结算实际用量，释放保守预留的差额。模型调用期间没有 session 事务。

预算增加后，可显式调用 `service.build(frozen_observation, budget_authority_revision=new_revision)`。新预算及 catalog 容量限制来自持久化授权，预留及 stage 身份记录授权版本；原 Observation、任务记忆和 Lineage 不被修改。未传新授权版本的旧重试不会自动获得更多额度。

提交前进程崩溃会重做未提交模型工作；提交后新进程可以零调用精确命中，并为后继 Revision 继续增量构建。

无法确认真实用量的崩溃预留继续占额度，不按 TTL、重试或新授权自动释放；恢复重做也需要剩余额度。此阶段没有自动推断失联请求结果的功能。

## 成本与限制

第一阶段仍读取、规范化和检查完整 execution history，并保存完整目标 Index。优化重点是模型输入和验证，不能宣称数据库读取或 JSON 存储成本也只随增量增长。

局部合同增加冷构建调用数，整体解释仍需要完整目录输入和必要的旧原文，因此总成本并非只随新尾部增长。完整目录或必要原文超窗、读取／循环上限不足、共享授权预算不足均显式阻断；不裁剪目录后发布 local-only complete。个别联合 claim unsupported／unknown／missing verdict 可 quarantine 并发布保留 proof 的 degraded Index。

100 个稳定段、1202 条消息的确定性 fixture 中：冷构建局部 202 次 + 综合 1 次 = 203 次；追加局部 2 次 + 综合 1 次 = 3 次；精确命中 0 次。该 fixture 综合合法空结果，无联合 verifier；有跨段关系的实际成本还包含回读与联合验证。测试分别记录两阶段及合计的请求 payload、模拟 tokens，不能将局部节省当成总管线成本或真实 provider token 预测。

确定性测试以同一目标的 cold/incremental 陈述、覆盖与质量合同作比较；真实 LLM 的两次输出不要求逐字相同。提供商未公开的模型别名变化无法完全检测，显式配置／prompt／verifier 版本变化可以使缓存失效。

## 迁移与回滚

Alembic revision `9d0e1f2a3b4c` 添加局部 records；`ae1f2a3b4c5d` 添加预算预留。新 `bf2a3b4c5d6e` 只添加 JSONB CHECK，要求 `rev:` 合同的 ready payload 包含完成的 interpretation；proof 留在 Index JSON，无新表，不改已发布迁移。旧 v3/v4 Index 及其 planning sessions 按原身份可读，无综合 proof 不冒充新合同。

后继 additive revision `ae1f2a3b4c5d` 添加 `loop_index_budget_reservations`，不改已发布迁移。存在未结算预留时 downgrade 显式失败，避免降级后重新获得已发送工作的额度；需依据真实调用证据先完成结算。

回滚应用可保留数据库结构。显式 downgrade 前停止 writers；新迁移先撤销 `rev:` 综合 artifact，再移除约束；若继续降级则旧迁移撤销带继承凭据 v4 后删 records。旧历史 artifact 与 Context／Progress 数据保留。引用被撤销新合同 artifact 的 planning sessions 需重新规划，schema downgrade 不保留新版执行状态。
