# Round 长期记忆与实时事实

Progress 包含两种记忆。Task Progress 累计任务成果、义务、blocker 和未决项，由成功决策与执行后果汇合登记的工作驱动；Derivation Lineage 描述真实 Context 派生与合并历史，由权威 publication 提交驱动。LoopFact 独立展示实时事实，其当时已有 revision 进入因果快照；任务记忆仍从领域结果和 receipts 读取来源。

```text
用户输入 Ui → 冻结输入前 Oi → 当前未决 Round 追加不可变 Vi
                               └→ 完整 Curator → Patrol → Kernel 成功提交
                                                             └→ 登记决策 work
已提交 R5 / R6 / R7 的执行后果 → 全部稳定 → 同一队列的 join work
                                                └→ 来源全部吸收 → 唯一自动后继

决策 work / join work → 首次领取绑定当前 Progress head 与未吸收来源
                     → 校验 → 原子发布新 Progress、贡献、receipts、head
```

新 Loop 在激活事务创建 P0。旧 Loop 没有进度 head 时，在下一份正式 Observation 创建一次 migration baseline，明确 `history_complete=false`，登记已纳入来源的 receipts，不伪造旧 Round 的逐轮进度。旧 Observation 内容和 hash 保持原样。

正式冻结由 `observation_capture.LoopObservationService` 完成。事务外准备精确 revision 内容，REPEATABLE READ 短事务内绑定已发布 Progress、Observation、完整 task manifest、Lineage snapshot；事务冲突重试整份冻结。冻结只保存决策输入，尚未提交的 V 不登记沉淀工作。领域来源以独立结果与吸收 receipts 为准，按稳定 key 分页，超过 4096 条明确阻断，不取最近 Run 切片作为任务增量。

每 Loop 同时至多一个未提交 Round。用户受理与 Kernel 成功权威提交按 Loop→Round 锁序确定先后：输入先受理则同轮追加新 V、旧 fence/Curator/Patrol 失权；提交先成功则输入新建例外 Round。先前已授权工作由所属 Round 管理，较新 Round 或 Mission 不直接取消它。所有活动轮、认知清理、发布/采用及输入处置收敛后，唯一自动推进事务再核对 join 清算。Kernel 负责机械合法性，Patrol 决定保留与精确取消。

每条用户输入保留不可改写的 Oi/Ui 对应关系。新的 Vi 同时包含当前工作局面和完整原因果清单，模型/检视按 identity、hash 与游标读取精确历史。服务器提交边界与客户端已见游标独立，Fact 快照保留实际 revision、projector cursor 与失败覆盖；后来投影的事实不回填旧快照。

`TaskProgressQueue` 在成功权威提交时登记 observation work，在全部执行后果稳定时登记 join work；二者共用原 runtime、head、receipts 和会计。work_id 独立于可空的 origin observation，join 不制造 Observation。首次领取按 Loop→head→最前序 work 加锁，以当前 head 排除已吸收来源，并固定有效 Mission、必要用户修订与 Lineage。pending/claimed/blocked 前序不能被跳过，重试保留首次绑定的沉淀输入。观察输入不会因为沉淀前序变化而被改写。

每次模型归纳必须解释全部来源。明确的任务变化引用冻结证据；没有变化的来源也必须说明已知、不属于任务进展或 unknown。unknown 进入未决记忆。Run 结束和 Agent 自述不能单独证明任务完成，失败/未知测试也不能证明完成。RunContribution 保留真实执行 Round 与 consolidation work，RoundContribution 区分原观察轮和汇合轮集合。旧成果与没有新消息的义务不会因缺席而丢失，Mission 修订要求重新判断义务。

`task_progress` 分为合同、纯归纳、来源读取、串行队列、持久 repository、独立 runtime 与只读诊断。work 使用 lease/fence 和最多三次自动尝试；模型事务外运行，调用前预留 Loop 调用和 token 预算，实际报告后替换估计。失联工作保留保守 unknown 预留，晚到实际用量幂等回填。版本、contribution、吸收 receipts、head、journal 与 published 状态原子提交。停止 Loop 后仍可收口已登记 work，不发出 Directive。

用户介入可使用最新已发布 Progress，不等待旧轮记忆工作。正常自动后继必须等前序和 join work 发布。诊断为 `GET /desktop/api/agent-loops/{loop_id}/task-progress`；明确 blocked 的工作通过 `POST /desktop/api/agent-loops/{loop_id}/task-progress/{work_id}/retry` 恢复。显式重试记录新 grant identity/revision、预算限制和确认时间，调用前重新验证，仍不改变冻结的沉淀输入。旧版本迁移保留 work_id 等于原 observation_id 的身份，因此旧诊断链接仍定位原工作。

Patrol 的 `context_permissions` 完整列出本 V 的可治理 Context 和 `action:<position>` 派生目标，`null` 表示本轮不执行。Kernel 校验三档模式的用户上限与角色上限，工具能力仍独立检查。Directive/Run 保存所属 Round、Decision、Context、grant 与实际文件模式；运行时逐调用复核，较新轮的只读不会覆盖旧轮可写。新轮必须重新授予。Workspace intent 使用同一实效模式，reader lease 不能凭单次审批直接写入。

## 候选引用准入和修正反馈

`candidate_contract` 为每份冻结增量构建请求专属来源枚举，并统一验证候选结构、来源覆盖、按 source_key 的解释唯一、任务修正关系和完成证据。`consolidation` 先经同一准入再转换记忆，repository 在原子发布事务中再次重算。Provider schema 通过不能代替服务器准入；重复解释不会通过转为 set 或字典覆盖被静默清理。

`candidate_interpretation` 只负责请求、反馈与安全诊断投影。校验失败后，下一次既有尝试使用同一冻结输入并携带 `validation_feedback`；反馈不是任务事实。一次领取最多调用模型一次，完整 schema 和反馈计入窗口与共享预算，仍遵守原三次尝试上限、lease/fence 与独立用量记账。

容量预览由 `StructuredWorkerModel.estimate_input_tokens` 读取真实 Runnable 的绑定参数并复用 adapter/SDK 的请求序列化，计入消息之外的 `text.format`、`response_format` 和工具 schema。Runtime 使用同一估算核对模型窗口和共享输入消费，先扣除输出预留再调用；预览不发送请求。离线测试将预览与 MockTransport 捕获的实际 SDK 请求比较，并覆盖超窗、共享预算不足和完整输入预留，防止只统计消息正文而漏掉结构化输出合同。

只读诊断的 `work[].attempt_events` 中新增版本为 1 的 `candidate_validation` 事件，含 work/origin observation/input/manifest 身份、fence、usage fence、valid、errors、error_count/error_counts 和候选引用投影。重复解释、解释越界、变化证据越界及来源遗漏分别有稳定 code 与字段 path；语法/schema 失败使用安全结构诊断。未知引用仅保留 hash 和长度，description、explanation、原始响应和私有推理不入记录或反馈。诊断和反馈各最多 64 KiB，超限明确标记 truncated、总数量与保留数量，并保留完整安全投影的 hash；完整准入和冻结来源不受该上限裁剪。

重启从同一输入的持久诊断恢复剩余尝试；失败或事务回滚不推进 head、不吸收来源。已有 blocked 工作不会因部署自动恢复，仍需用户显式重试并验证有效授权。旧记录只有合并来源错误时，反馈标注 `evidence_missing`，不能补造历史非法键；重新尝试才产生新版诊断。回滚实现时保留 JSONB 事件与消费记录，旧读者可以忽略新增字段。

Lineage 权威仍是 Context current pointer、Revision/source 图与同事务 publication receipt。读取器完整回溯 Revision DAG，保留跨 Context 多父来源；同 Context 来源用于追溯，不作为派生边。Architecture R4→Implementation R7→Architecture R5 可终止回溯，即使 Context 层面存在回流。assignment 使用相关 Context 与全部祖先的子图，只提供拓扑身份，不借路径泄漏祖先消息。当前成员展示图继续独立使用原 resolver。

只有已通过 projection 审核的根可以进入真实路径。若当前 pointer 指向 approval_required 等候选，读面使用该 Context 最近的真实 publication receipt；候选不生成 receipt，不能提前改变路径。归档成员不删除已提交祖先。

assessment 和 Curator 结果保存在独立认知 supplement；`PatrolDecisionContext` 只能追加这两类产物。它不修改基础 Observation/hash、授权、预算、frontier 或用户意图。Kernel 的 freshness、fencing、Mission 和 CompletionGuard 继续决定行动是否合法，Task Progress 的 completed 不直接触发任务终止。

新冻结在事务内重新检查 Loop/Round 和授权状态。准备内容后用户停止、撤销授权，或授权过期、轮次 superseded，不生成新的 Observation 和记忆工作；编排将该尝试收口。已冻结输入仍可恢复读取，终态后台继续收口既有记忆。

Run 交付与实际执行启动是不同边界。若 durable worker 在 launcher 返回之前启动，Directive 已进入 `run_started`；Dispatcher 仍按相同 Run identity 幂等确认 Expansion `dispatched`，不依赖 Directive 恰好仍处于 `delivering`。

Test 解析在 activity 摘要截断前产生类型化领域结果，Fact 和 Progress 分别消费。Test/Artifact 来源先在独立事务保存，展示 journal 全部重试失败或 quarantine 仍不撤销来源。普通 Tool 完成保留活动/审计，不生成公开 LoopFact；旧 Tool facts 在查询、关系端点、快照、replay、前端 schema/selectors/view 中被排除。独立消费者没有互相就绪依赖。

# 部署与回退

1. 停止旧 Gateway 写入，备份数据库，运行 Alembic upgrade 到 `a70d1e2f3b4c`，再启动新 Gateway。不要同时运行新旧 producer。
2. 首次正式 Observation 自动建立旧 Loop 的 baseline；检查诊断中的前序/hash、manifest completeness、work state 和预算报告。
3. 应用回退先停止/暂停 Loop、关闭新 Gateway，保留新表和冻结记录。旧二进制不应继续新版已冻结 Round；恢复新版后原 work 可按 fence/lease 继续。
4. 新版多 V 与独立 join work 不能无损还原成单轮单观察。本迁移主动拒绝 downgrade；需要回退应用时先停止受理、排空执行并保留数据库备份和全部新历史，不让旧 producer 写入新版活动轮。旧 Observation 和事实表不被迁移改写。

本次自动验证使用隔离 PostgreSQL、确定性模型端口、前端测试和 Windows 原生进程。它验证生产提交、执行结算与汇合路径；没有调用收费模型或对用户数据库执行升级/回退演练。具体命令与结果记录在本 change 的 implementation-notes.md。

# 历史版本的操作演练（2026-09-30）

以下记录验证的是当时单 Observation 记忆版本。它不代替本次多 V / join work 的验收；本次定向证据见 OpenSpec change 的 implementation-notes.md。

当时自动验证覆盖冻结/晚到、40 条领域来源、发布回滚/幂等、失败计费/双 worker/终态收口、多父及回流路径、shadow/提交边界、实时测试与 Tool 排除。2026-09-30 已追加实际服务进程的旧/新版本回退、备份恢复及组合故障验收；当时真实桌面模型的 Backend/Test/UI→E2E 全流程由用户免验，没有执行。

`backend/tests/test_round_memory_operational_drills.py` 使用一次性 PostgreSQL，与用户数据库和运行中的服务隔离。`round_memory_drill_worker.py` 每次独立启动 Python 服务进程并建立自己的 engine/session；`round_memory_drill_support.py` 管理进程、旧版本提取、数据快照与恢复库。模型端口返回确定性未知证据，故障注入只位于模型/投影端口及发布事务边界，生产状态机、账本、冻结、发布与控制代码实际运行。

回退覆盖三种时点：模型已预留预算但尚未返回；发布已 flush 但事务尚未 commit；发布 commit 已返回。子进程被操作系统强杀后，旧 HEAD 服务进程读取暂停的 Loop，并逐行比较 Observation、DecisionInputs、Progress、heads、receipts、work、领域来源、Context revision/source DAG、指针与 publication receipts。恢复新版后，未提交工作重新领取新 fence，只发布一个版本；已提交工作不再归纳。晚到 Test 只进入后继轮。租约通过隔离库显式设置过期模拟时钟推进，未等待实际 90 秒。

提交后场景执行真实 `pg_dump -Fc`，将备份恢复到随机 `focus_drill_restore_*` 库，并核对上述全部数据。只删除自己创建的恢复库；应用回退始终保留 additive schema，演练不对用户库运行 downgrade。

组合演练同一个 Loop 的记忆 worker 与 Fact projector 同时阻塞，仍通过生产服务结算 Run、提交 Test、修订 Mission 和暂停。陈旧 CREATE Portfolio 经过真实发布检查成为 superseded，没有当前指针或 lineage proof。记忆进程强杀后的未报告用量保留预留，失败重试记录实际用量；Fact 连续三次超时后隔离，后续 Test 继续展示。记忆可在 Fact 隔离期间发布，修复重放 Fact 不修改记忆。下一轮使用新 Mission 与晚到来源；再次同时阻塞两个进程时，用户停止仍可完成，重启消费者后仅收口既有冻结工作，不产生新 Directive。

复现命令（PowerShell，独占容器名和端口）：

```powershell
docker run --rm -d --name focus-memory-drill-20260930 --label codex.round-memory-drill=20260930 -p 127.0.0.1:17221:5432 -e POSTGRES_USER=focus -e POSTGRES_DB=postgres -e POSTGRES_HOST_AUTH_METHOD=trust pgvector/pgvector:pg17
$env:PYTHONPATH="$PWD/backend/packages/harness;$PWD"
$env:FOCUS_DATABASE_URL='postgresql+asyncpg://focus@127.0.0.1:17221/postgres'
$env:FOCUS_DRILL_CONTAINER='focus-memory-drill-20260930'
python -m pytest backend/tests/test_round_memory_operational_drills.py -q --tb=short -p no:cacheprovider --basetemp .tmp/round-memory-drill
docker stop focus-memory-drill-20260930
```

pytest 创建并销毁随机 `focus_test_*` 库；恢复检查还验证容器归属标签及 host/port。未明确配置演练容器时，这组操作测试跳过。每个场景保存 `evidence.json`、阶段 JSON 与进程日志；最终汇总位于 change 的 `operational-drill-evidence.json`。
