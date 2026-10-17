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

`RevisionInterpretationBuilder` 获取全部有序 segment 身份、所有已接受的局部 authority／线索、rejection 和 fallback。冷构建和增量均优先在首轮提供全部冻结原文，前提是完整请求窗口、读取上限和本轮剩余模型预算均容纳；否则增量先给变化尾部，冷构建先给目录，再按需读取。预算预检不预留或消费，实际发送仍重新检查持久授权和共享额度。综合模型可 `read_segments` 展开任意冻结旧段，在同一次调用中理解多段原文；没有固定相邻窗口、关键词或 top-k 门槛。摘要只引导发现，支持引文必须来自实际提供的原文。

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

读取和发布分别使用短 session；模型调用不持有数据库事务。同一 Revision 的独立 segment 并发处理并按原序组装，每段仍依次执行抽取和独立验证。服务内跨 Revision、跨 build 的抽取、验证与整体解释共享模型调用额度，默认 4；内部重试也占用同一名额，不按 Context 数量放大并发。

同一服务内，完全相同的冻结 Observation、授权版本、fencing token、切段／模型合同与 catalog 限制共享一个进行中的完整构建。等待者独立取消；最后一个等待者离开时才取消并回收工作。不同 Round、Observation 或授权不合并；跨进程／服务实例仍依靠持久缓存和提交赢家保证一致性。

## 发布与恢复

每个局部段完成抽取、验证和完整性检查后，独立短事务保存不可变缓存并采用首个提交赢家。失败、取消或进程重启后的构建可以复用这些记录，只补缺失段及整体解释。

所有来源完成整体解释后，在一个短事务中重验已提交局部依赖、解析完整 Index 赢家并校验 catalog；失败或容量超限不发布部分完整 Index。局部缓存仅供构建使用，Curator 始终消费完整 catalog。不同 Revision 竞争导致理解依赖不一致时显式阻断，不拼装未经验证的 proof。

完成、失败、重试和取消都记录实际新模型用量，历史 attempt 不重复计费。每个阶段通过 `index_phase` 区分 local、interpretation 和 interpretation_verifier；所有阶段共用 durable admission，不额外获得预算。

预算化模型包装器为每次 invocation 保存独立 attempt 快照。配置检查或外层准入在发送前拒绝时，本次记录为空；真正进入底层模型的失败、结果验证错误和取消仍采集本次真实用量。两次实际调用即使元数据相同也分别计费，不按内容去重。

stage 记录区分构建合同、授权预算及成功／失败结果；重试成功不会重放旧失败结果。旧 stage 保留不改写。

Index／record schema 和完整切分 fingerprint 都属于 record 合同。即使两种切分政策碰巧产生相同的段，也不会在发布时采用另一合同的旧 record。

每个索引 build 在发送请求前，以短事务锁定 Loop 用量，检查真实 active grant 的版本和限制，并扣除已有用量及全部未结算预留。并发实例和显式重试共享这份额度；结束时幂等结算实际用量，释放保守预留的差额。模型调用期间没有 session 事务。

预算增加后，可显式调用 `service.build(frozen_observation, budget_authority_revision=new_revision)`。新预算及 catalog 容量限制来自持久化授权，预留及 stage 身份记录授权版本；原 Observation、任务记忆和 Lineage 不被修改。未传新授权版本的旧重试不会自动获得更多额度。

完整 Index 提交前进程崩溃，只重做未保存的段及整体解释，已提交局部缓存可以复用；完整 Index 提交后新进程可以零调用精确命中，并为后继 Revision 继续增量构建。

无法确认真实用量的崩溃预留继续占额度，不按 TTL、重试或新授权自动释放；恢复重做也需要剩余额度。此阶段没有自动推断失联请求结果的功能。

## 成本与限制

第一阶段仍读取、规范化和检查完整 execution history，并保存完整目标 Index。优化重点是模型输入和验证，不能宣称数据库读取或 JSON 存储成本也只随增量增长。

局部合同增加冷构建调用数，整体解释仍需要完整目录输入和必要的旧原文，因此总成本并非只随新尾部增长。完整目录或必要原文超窗、读取／循环上限不足、共享授权预算不足均显式阻断；不裁剪目录后发布 local-only complete。个别联合 claim unsupported／unknown／missing verdict 可 quarantine 并发布保留 proof 的 degraded Index。

100 个稳定段、1202 条消息的确定性 fixture 中：冷构建局部 202 次 + 综合 1 次 = 203 次；追加局部 2 次 + 综合 1 次 = 3 次；精确命中 0 次。该 fixture 综合合法空结果，无联合 verifier；有跨段关系的实际成本还包含回读与联合验证。测试分别记录两阶段及合计的请求 payload、模拟 tokens，不能将局部节省当成总管线成本或真实 provider token 预测。

确定性测试以同一目标的 cold/incremental 陈述、覆盖与质量合同作比较；真实 LLM 的两次输出不要求逐字相同。提供商未公开的模型别名变化无法完全检测，显式配置／prompt／verifier 版本变化可以使缓存失效。

默认分段仍为 12 条，工具交换保持闭合；本轮调度和缓存优化不改变分段 fingerprint，也不要求已有完整缓存冷构建。12／30 的选择还需比较真实模型冷构建与追加场景，不能从请求数直接推算端到端加速。

### token 体积分段与冻结上下文对照

`PortfolioSemanticIndexService(..., segment_max_messages=12, segment_max_estimated_tokens=4096)` 可同时约束消息条数和估算 token 体积。体积统计覆盖序列化内容、结构化正文、工具参数及元数据，使用共享文本启发式（CJK 字符 1、其他非空白字符约 1/4，加消息开销），不是 provider tokenizer，也不替代完整模型请求的窗口／授权预检。单条消息和完整工具调用／结果交换始终不可拆分，超过体积目标时独占一段，实际请求仍可能因窗口不足被拒绝。

token 上限默认关闭，继续使用原来的 12 条策略与 v2 fingerprint；开启时使用含消息上限、token 上限和估算算法版本的 v3 fingerprint。因此已有缓存保持可读，开启新合同需重新索引，不会复用另一切分合同的局部 records。Curator 提案仍依据本次冻结输入生成。

从仓库根目录运行以下 PowerShell 命令。发现和导出只连接本机 Focus 数据库的只读会话，通过 `ContextRevisionReader` 读取精确 Revision 的 semantic view；不执行迁移、checkpoint setup 或数据清理。`--config` 可选择其他配置文件。导出包含会话原文，示例存放在仓库外。

```powershell
$env:PYTHONPATH = "$PWD;$PWD\backend\packages\harness"
python -m backend.scripts.semantic_index_corpus list
python -m backend.scripts.semantic_index_corpus export --auto --output "$env:USERPROFILE\focus-index-corpus.json"
# 或指定 Context，读取其已发布 Revision；也可重复 --revision-id 精确固定多个版本：
python -m backend.scripts.semantic_index_corpus export --context-id CONTEXT_ID --output "$env:USERPROFILE\focus-index-selected.json"
python -m backend.scripts.semantic_index_corpus export --revision-id REVISION_ID_1 --revision-id REVISION_ID_2 --output "$env:USERPROFILE\focus-index-revisions.json"

$env:FOCUS_INDEX_REAL_MODEL = '1'
$env:FOCUS_INDEX_BENCHMARK_CORPUS = "$env:USERPROFILE\focus-index-corpus.json"
$env:FOCUS_INDEX_BENCHMARK_OUTPUT = "$env:USERPROFILE\focus-index-results"
$env:FOCUS_INDEX_BENCHMARK_CONCURRENCY = '4'
$env:FOCUS_INDEX_BENCHMARK_TOKEN_LIMITS = 'none,4096'
$env:FOCUS_INDEX_BENCHMARK_REPEATS = '1'
python -m pytest backend/tests/test_index_real_model_performance.py -k latency_matrix -q
python -m backend.scripts.semantic_index_corpus summarize --reports "$env:FOCUS_INDEX_BENCHMARK_OUTPUT" --output "$env:USERPROFILE\focus-index-summary.json"
```

自动选择包含短对话（≤12 条、≤2048 估算 token、没有长工具输出）、至少 4096 字符工具输出，以及三个严格追加版本的样本；优先选择超过 30 条消息的后两类样本，以便消息上限确实可能改变分段。样本不足则明确报错，可以指定 ID 做覆盖范围较小的测试。短对话／长工具样本若没有固定后继，只测 cold 和 exact；报告的 `incremental_scenario_available` 明确标出该范围，多次追加 case 测增量阶段，不伪造后继原文。`list` 仅输出 ID、消息数及体积，不打印正文。导出文件以排他创建方式写入，包含来源身份、来源内容 hash 和 semantic 内容 hash；不允许静默覆盖。内容 hash、协议闭合和追加前缀在加载时重验；来源历史发生改写时分为独立测试 case，不冒充追加链。含 NUL／孤立 surrogate 的文本无法存入索引 JSONB，发现时单独报告并跳过，不清洗后冒充原样本。`--limit` 默认最多发现最近 200 个已发布版本。

矩阵为消息上限 12／20／30 × token 上限关闭／4096。比较消息上限时固定 token 上限；比较 token 上限时固定消息上限。每个 case、配置和重复次数均创建独立测试 Context，全部索引写入自动创建／销毁的隔离 PostgreSQL 测试库，不使用或删除生产缓存。模型、共享并发和预算限制一致并写入报告。数据文件重放保留原 semantic policy；新的 Revision 身份只属于测试命名空间，报告保留原始来源及输入 hash。未设置 corpus 时仍使用明确标注的合成消息。

每份 JSON 分别记录冷构建、每次追加、每个成功版本的完整缓存命中：实际总耗时、模型调用数与落账数、输入／输出 token、重试次数和比例、分阶段耗时、fallback 段比例、引文逐字有效性和接受引文的消息覆盖率。后者只是语义覆盖的代理指标，不能证明语义完整性；报告保留接受陈述和消息来源，需人工核对遗漏、否定及跨段关系。模型阻断／预算或窗口不足保留耗时及真实消费；未发布完整 Index 时将缓存阶段标记为未执行，不把失败称为缓存命中。各阶段预算按真实累计消费扣除，不重置追加版本的预算。若设置外部 `FOCUS_TEST_DATABASE_URL`，它必须指向专用测试库。

性能统计使用每次调用的运行日志：`semantic_index_build` 记录实际等待耗时、是否合并和结果；`semantic_index_model_invocation` 记录 schema、排队、执行与预算准入耗时；`semantic_index_revision` 记录局部抽取、整体解释及局部缓存命中／新投影段数。执行耗时包含预算准入和内部重试，不能当作纯 provider 延迟；日志不含原文或凭据。不可变 stage artifact 在精确重放时保留首次耗时，不能直接作为缓存请求的实时延迟样本。

### 2026-10-17 真实模型摸底（原文预取生产化之前）

使用用户当前配置的 `deepseek-flash`，每组独立 Context、55 条合成验证事实，随后追加 2 条。以下均为索引服务单次实际耗时，尚未包含 Curator、Patrol 和材料合成，也不是 P50/P95。

| 分段上限 | 模型并发 | 冷构建 | 追加 2 条 | 追加索引质量 |
| --- | --- | --- | --- | --- |
| 12 | 1 | 99.98 s | 49.48 s | complete |
| 12 | 4 | 69.34 s | 47.18 s | complete |
| 30 | 1 | 77.01 s | 55.00 s | complete |
| 30 | 4 | 73.72 s | 65.40 s | degraded：隔离 1 条 unsupported claim |

四组精确命中均为零模型调用，约 46–74 ms。30/4 追加样本的 57 条消息仍有已接受的引用；严格要求 `complete` 的基准断言失败，该结果保留在原始报告中。单轮结果不能用于推断分段大小与质量失败率的因果关系。

12/4 的追加阶段中，局部处理约 14.78 s，整体解释约 32.21 s，并产生一轮旧原文回读。隔离实验在容量及读取上限允许时，首次整体解释直接提供全部冻结原文：冷构建 70.75 s，追加 26.85 s，缓存命中约 39 ms；追加不再回读，索引为 complete。模型调用由 5 次变为 3 次，其中也包含联合命题输出及验证需求的差异，不能将全部加速都归因于少了一轮回读。跨段“怀疑—排除—最终诊断”的真实模型检查也通过。当时该策略仅存在于测试补丁，随后已按上述预算和窗口约束落实到生产 builder；有效的历史窄窗口 proof 与完整缓存继续复用，不强制冷构建。

此前 8 项真实模型测试为 7 项通过、1 项严格质量断言失败。原始分阶段耗时、调用、tokens、引用及质量数据见 [汇总报告](benchmarks/semantic-index-2026-10-17/summary.json)。基准入口为 `backend/tests/test_index_real_model_performance.py`，需显式设置 `FOCUS_INDEX_REAL_MODEL=1`；输出目录、消息数和重复次数分别由 `FOCUS_INDEX_BENCHMARK_OUTPUT`、`FOCUS_INDEX_BENCHMARK_MESSAGES`、`FOCUS_INDEX_BENCHMARK_REPEATS` 控制。当前基准直接使用生产窗口选择，已移除原文预取测试补丁。样本为短文本事实，尚不能代表长工具返回和生产流量分布。

### 原文预取生产化后的复测

同一提供商、12 条分段、共享并发 4，基准直接运行生产代码：55 条合成消息冷构建 61.05 s，追加 2 条 43.90 s，精确命中约 60 ms、零调用，均为 complete。追加调用从此前样本的 5 次降到 4 次，旧原文回读从 1 轮降到 0 轮，但整体解释及联合验证仍约 29.82 s；没有复现隔离实验的 26.85 s。独立跨段纠错检查通过，保留“初步怀疑、随后排除、最终确认”的联合陈述，不过该 fixture 的追加仍耗时 64.20 s，零回读不等于低延迟。

本轮 202 项确定性回归和 2 项生产真实模型测试通过，覆盖容量／读取／预算不足时回退、旧窄窗口缓存精确命中、引用验证、取消及崩溃恢复。各配置仍只有一次样本，输出和提供商延迟均有波动，不能声明稳定加速比例或 P50/P95。生产原始数据及与此前样本的比较见 [生产复测汇总](benchmarks/semantic-index-2026-10-17/production-prefill/summary.json)；历史失败和隔离实验报告保留。

## 迁移与回滚

Alembic revision `9d0e1f2a3b4c` 添加局部 records；`ae1f2a3b4c5d` 添加预算预留。新 `bf2a3b4c5d6e` 只添加 JSONB CHECK，要求 `rev:` 合同的 ready payload 包含完成的 interpretation；proof 留在 Index JSON，无新表，不改已发布迁移。旧 v3/v4 Index 及其 planning sessions 按原身份可读，无综合 proof 不冒充新合同。

后继 additive revision `ae1f2a3b4c5d` 添加 `loop_index_budget_reservations`，不改已发布迁移。存在未结算预留时 downgrade 显式失败，避免降级后重新获得已发送工作的额度；需依据真实调用证据先完成结算。

回滚应用可保留数据库结构。显式 downgrade 前停止 writers；新迁移先撤销 `rev:` 综合 artifact，再移除约束；若继续降级则旧迁移撤销带继承凭据 v4 后删 records。旧历史 artifact 与 Context／Progress 数据保留。引用被撤销新合同 artifact 的 planning sessions 需重新规划，schema downgrade 不保留新版执行状态。

## 2026-10-17 冻结真实上下文：完整 12／20／30 对照

本批结果显示耗时、调用数与覆盖之间存在取舍：关闭 token 上限时，30 条把长工具冷构建从 12 条的 238.13 s／10 次降到 165.23 s／6 次，但接受引文消息覆盖从 100% 降到 50%，fallback 从 0/4 段变为 1/2 段。最后一次追加中，30 条为 157.00 s／6 次，12 条为 284.71 s／12 次；三种消息上限均只有 1 个 fallback 段、消息覆盖均为 33.33%，不能单凭 fallback 百分比判断覆盖损失。

开启 4096 上限后，长工具及最后追加的模型调用和耗时都增加，消息覆盖改善并不一致。整批合计 270 次有记录模型调用、6 次结构化重试，30 次完整缓存检查全部零调用。最后目标版本均为 degraded；接受引文逐字有效不代表语义完整。上述是单轮观测，默认参数保持不变。

18 个配置全部完成：保留此前 4 份报告的原始字节，补跑其余 14 项。源文件 SHA-256 为 `8328bb9c8c4bf3a98a9e7e4cd50c7495d846b83987b476ce68a622e0a3f8fd38`，三类样本的全部原始 Revision identity、内容 hash、报告 hash、配置及分阶段数据见 [完整汇总](benchmarks/semantic-index-token-sizing-2026-10-17/summary.json)。输入仍是短对话 2 条、长工具上下文 42 条，以及 1→5→57 条消息的同一追加链；没有重新发现或替换样本。

模型为 `deepseek-flash`，输出上限 65,536；共享并发 4。每组授权均为 10,000 次模型调用、输入和输出各 10,000,000 token。每个配置使用独立测试 Context 和隔离测试数据库，未清理生产缓存。默认仍为 12 条、token 上限关闭。

下表每格为「实际秒数／模型调用数」。耗时仅覆盖索引服务 build，不含后续 Curator、Patrol 或材料合成。冷构建和两次追加分别统计，缓存命中单独统计。

| token 上限 | 消息上限 | 短对话冷构建 | 长工具冷构建 | 追加链冷构建 | 追加 1（5 条） | 追加 2（57 条） | 模型重试合计 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 关闭 | 12 | 38.86／3 | 238.13／10 | 8.84／2 | 62.59／3 | 284.71／12 | 0 |
| 关闭 | 20 | 38.70／4 | 209.96／8 | 8.71／2 | 88.69／5 | 361.53／9 | 3 |
| 关闭 | 30 | 42.67／4 | 165.23／6 | 9.37／2 | 85.32／4 | 157.00／6 | 0 |
| 4096 | 12 | 32.15／3 | 302.22／19 | 12.17／2 | 44.14／3 | 555.38／37 | 2 |
| 4096 | 20 | 40.22／3 | 282.11／18 | 9.27／2 | 33.62／3 | 545.49／36 | 0 |
| 4096 | 30 | 32.06／3 | 384.67／19 | 8.99／2 | 60.97／4 | 466.07／36 | 1 |

| token 上限 | 消息上限 | 模型调用合计 | 输入 token 合计 | 输出 token 合计 | 5 次完整缓存命中范围（ms） |
| --- | --- | --- | --- | --- | --- |
| 关闭 | 12 | 30 | 634,310 | 263,731 | 26.24–240.27 |
| 关闭 | 20 | 28 | 704,439 | 242,869 | 25.59–248.25 |
| 关闭 | 30 | 22 | 593,904 | 129,870 | 31.96–212.30 |
| 4096 | 12 | 64 | 730,178 | 553,139 | 24.23–268.63 |
| 4096 | 20 | 62 | 727,731 | 547,699 | 23.55–310.29 |
| 4096 | 30 | 64 | 729,727 | 564,119 | 26.51–297.52 |

质量表使用每个 case 的最后一个冻结版本（短对话／长工具为 cold，追加链为 append-2）。fallback 同时列出段数和分母；消息覆盖是接受引文对 `semantic_policy=index` 消息的覆盖代理，不能证明语义完整。逐字有效性仅针对已经接受的 supports，unsupported／unknown 等隔离结果另计。

| token 上限 | 消息上限 | 样本 | fallback 段 | 接受引文逐字有效率 | 接受引文消息覆盖 | 隔离命题数 | 质量状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 关闭 | 12 | short | 1/1 (100.00%) | 100% (10/10) | 50.00% | 0 | degraded |
| 关闭 | 12 | long-tool | 0/4 (0.00%) | 100% (127/127) | 100.00% | 15 | degraded |
| 关闭 | 12 | multiple-append | 1/5 (20.00%) | 100% (253/253) | 33.33% | 11 | degraded |
| 关闭 | 20 | short | 1/1 (100.00%) | 100% (10/10) | 50.00% | 0 | degraded |
| 关闭 | 20 | long-tool | 0/3 (0.00%) | 100% (160/160) | 100.00% | 5 | degraded |
| 关闭 | 20 | multiple-append | 1/3 (33.33%) | 100% (111/111) | 33.33% | 23 | degraded |
| 关闭 | 30 | short | 1/1 (100.00%) | 100% (15/15) | 50.00% | 0 | degraded |
| 关闭 | 30 | long-tool | 1/2 (50.00%) | 100% (81/81) | 50.00% | 4 | degraded |
| 关闭 | 30 | multiple-append | 1/2 (50.00%) | 100% (62/62) | 33.33% | 9 | degraded |
| 4096 | 12 | short | 1/1 (100.00%) | 100% (10/10) | 50.00% | 0 | degraded |
| 4096 | 12 | long-tool | 0/8 (0.00%) | 100% (254/254) | 100.00% | 21 | degraded |
| 4096 | 12 | multiple-append | 1/17 (5.88%) | 100% (388/388) | 50.00% | 66 | degraded |
| 4096 | 20 | short | 1/1 (100.00%) | 100% (9/9) | 50.00% | 0 | degraded |
| 4096 | 20 | long-tool | 1/8 (12.50%) | 100% (409/409) | 87.50% | 17 | degraded |
| 4096 | 20 | multiple-append | 1/17 (5.88%) | 100% (353/353) | 33.33% | 57 | degraded |
| 4096 | 30 | short | 1/1 (100.00%) | 100% (9/9) | 50.00% | 0 | degraded |
| 4096 | 30 | long-tool | 0/8 (0.00%) | 100% (293/293) | 100.00% | 21 | degraded |
| 4096 | 30 | multiple-append | 1/17 (5.88%) | 100% (432/432) | 33.33% | 91 | degraded |

全部已接受引文均通过逐字及目标 Revision 绑定校验；完整缓存命中均为零模型调用。调用数按有记录的结构化模型 attempt／落账统计，不等于底层 HTTP 发送次数。这里的重试是结构化模型 invocation 内 `attempt > 1` 的次数，不包含 SDK HTTP 内部重试；本轮日志确实出现 SDK 重试及代理回退，实际墙钟耗时包含这些等待。历史中断请求未冒充成功样本，也不等于账户账单零消费。

关闭 token 上限时，长工具样本为 4／3／2 段，最后一次追加为 5／3／2 段。开启 4096 后三种消息上限分别都为 8 段、17 段，体积约束主导了切分，不能把这些配置间的延迟差都归因于消息上限。完整工具交换超过目标时保留原子，未截断原文。

每配置只有一次完成样本，执行顺序未随机化，没有人工语义完整性 gold set。这些是本批输入上的观测值，不是 P50/P95 或稳定加速比例；保持默认参数不变。

## 普通任务预算结算复测

索引实例的本地余额现按「已可靠结算的实际消费 + 仍在途或消费未知的预留」计算。生产模型的逐请求 owner receipt 在持久结算成功后同步释放本地输入／输出预留差额，内部重试前即可再次使用；持久结算失败或用量未知时保留整笔预留。兼容模型也按请求独立预留和结算，批次汇总跳过已由回执管理的 attempts，避免重复落账。并发结算和迟到回填均按各自回执幂等处理。

另用普通任务真实预设（100 次调用、500,000 输入 token、50,000 输出 token）及产品默认单次输出上限 8,192 跑通两项真实 provider 完整索引流程。冷构建→追加→缓存场景合计 8 次调用、11,245 输入／5,336 输出 token；跨段诊断及增量发现场景合计 12 次调用、8,117 输入／4,162 输出 token。两项质量均为 complete，完整缓存命中均为零调用；隔离 PostgreSQL 测试通过。宿主的 65,536 单次输出覆盖仅在测试配置副本中恢复为产品默认值，未修改宿主配置。

这些结果验证了上述两个合成历史的冷构建、局部投影、独立验证、整体解释、增量追加及完整缓存流程，不包含后续 planner、Curator、材料合成或完整 Loop，也不证明任意用户历史都能落在默认预算内。普通额度下另有确定性用例覆盖单次构建超过六次调用、内部重试、并发在途／未知占用及账本一致性。真实模型运行口径和原始结果见 [普通任务预算复测](benchmarks/index-budget-normal-2026-10-17/summary.json)。
