# Revision 继承式语义索引

Focus 为每个精确、冻结的 Context Revision 发布一份完整且不可变的 Semantic Index。持久缓存命中时不调用模型；新 Revision 沿已提交的同 Context `run_settled` 单来源链寻找最近的兼容基线，证明 execution messages 的完整旧前缀未变后复用稳定 segments。

```text
Frozen Revision
  ├─ 精确缓存命中 → 完整 Index
  └─ 缓存未命中
       ├─ 无兼容基线 → 全量 segment-local 构建
       └─ 有追加证明 → 复用稳定 segments + 重算变化尾部
                            ↓
                    重新绑定目标 Revision
                            ↓
                    完整、不可变的新 Index
```

## 复用证明

- 来源必须有 publication receipt。generation 相邻和消息 ID 相同均不构成追加证明。
- 规范化消息身份、顺序、角色、文本／多模态内容和 Tool 结构必须一致。
- segmenter fingerprint 包含实际切分参数；投影 fingerprint 包含 prompt、schema、grounding／verifier 规则和非凭据模型配置。
- 每次 build 冻结有效模型配置；长期复用的 service 也能识别配置变化。一次 build 中途变化则显式阻断。
- 基线查找最多 64 个来源节点。压缩、恢复、curation、定义更新、多来源、跨 Context、缺失记录或合同不兼容均全量回退；scope/stale/完整性错误直接阻断。

## 局部理解与来源绑定

模型一次只看一个协议闭合 segment，不能引用其他段。`SegmentProjectionRecord` 保存原始 drafts、精确 quotes、独立 verdicts、隔离结果和可用 provider 模型元数据；它按 Context 隔离，独立于具体 Revision。

新 Index 对复用和新 records 使用相同 assembly：重新检查 supports，生成指向目标 Revision 的 evidence refs 和 unit IDs，重建覆盖、fallback/rejections 和 quality。旧 Index 不被修改；degraded 不因继承变成 complete。Curator 和检索始终读取完整 Index，不需要追踪祖先指针。

索引是历史证据库。旧“测试失败”和新“问题已修复”可以同时保留；当前任务状态由 Task Progress 及决策层归纳。索引发布不会创建 Context、更新 Lineage、推进 Task Progress，也不依赖 LoopFact。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| portfolio_index | 冻结读取、共享资源调度、完整 Portfolio 编排 |
| index_inheritance / index_build_contracts | 权威来源链、追加证明与稳定前缀计划 |
| segment_projection / projection_record_repository | 单段投影验证、不可变记录与首个提交赢家 |
| index_assembly / semantic_indexer / semantic_index | 新来源绑定、协议切分、全量覆盖和身份校验 |
| index_model_budget / index_budget_repository / RoleBoundStructuredModel | 逐 attempt 请求准入、短事务共享预算预留与结算、窗口和真实用量 |

读取和发布分别使用短 session；模型调用不持有数据库事务。Portfolio 与 segment 调度共用同一个并发上限，段内顺序处理，不额外放大并发。

## 发布与恢复

所有来源就绪后，在一个短事务中解析 record/index 唯一键赢家并校验完整 catalog；失败或容量超限不会发布部分新批次。不同模型输出竞争时，后提交者采用实际持久化赢家及其 Index ID。完成、失败、重试和取消都记录实际新模型用量，历史 attempt 不重复计费。

stage 记录区分构建合同、授权预算及成功／失败结果；重试成功不会重放旧失败结果。旧 stage 保留不改写。

Index／record schema 和完整切分 fingerprint 都属于 record 合同。即使两种切分政策碰巧产生相同的段，也不会在发布时采用另一合同的旧 record。

每个索引 build 在发送请求前，以短事务锁定 Loop 用量，检查真实 active grant 的版本和限制，并扣除已有用量及全部未结算预留。并发实例和显式重试共享这份额度；结束时幂等结算实际用量，释放保守预留的差额。模型调用期间没有 session 事务。

预算增加后，可显式调用 `service.build(frozen_observation, budget_authority_revision=new_revision)`。新预算及 catalog 容量限制来自持久化授权，预留及 stage 身份记录授权版本；原 Observation、任务记忆和 Lineage 不被修改。未传新授权版本的旧重试不会自动获得更多额度。

提交前进程崩溃会重做未提交模型工作；提交后新进程可以零调用精确命中，并为后继 Revision 继续增量构建。

无法确认真实用量的崩溃预留继续占额度，不按 TTL、重试或新授权自动释放；恢复重做也需要剩余额度。此阶段没有自动推断失联请求结果的功能。

## 成本与限制

第一阶段仍读取、规范化和检查完整 execution history，并保存完整目标 Index。优化重点是模型输入和验证，不能宣称数据库读取或 JSON 存储成本也只随增量增长。

单段合同增加冷构建调用数；较长历史首次使用新合同可能需要更多授权预算。请求使用保守字节上界检查窗口和预算，不拆分超长 Tool Exchange；零预算和窗口不足都显式阻断。

确定性测试以同一目标的 cold/incremental 陈述、覆盖与质量合同作比较；真实 LLM 的两次输出不要求逐字相同。提供商未公开的模型别名变化无法完全检测，显式配置／prompt／verifier 版本变化可以使缓存失效。

## 迁移与回滚

Alembic revision `9d0e1f2a3b4c` 添加 `loop_segment_projection_records`；继承凭据保存在既有 index JSON payload。旧 v3 Index 和已冻结 planning sessions 保持可读，旧 artifact 不伪造验证记录。新版首次访问旧合同执行冷构建。

后继 additive revision `ae1f2a3b4c5d` 添加 `loop_index_budget_reservations`，不改已发布迁移。存在未结算预留时 downgrade 显式失败，避免降级后重新获得已发送工作的额度；需依据真实调用证据先完成结算。

回滚应用可保留新增表。显式 downgrade 前必须停止 writers；downgrade 删除本 change 的带继承凭据 v4 Index 后再删 records，保留旧 Index 和所有 Context／Progress 数据。引用被撤销新合同 artifact 的 planning sessions 不能在旧版本下继续恢复，需重新规划；不要把 schema downgrade 当成无损保留新版执行状态。
