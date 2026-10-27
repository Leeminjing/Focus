# Live Loop 发布与回滚

Live Loop 的权威状态始终保存在原有 Loop 领域表、canonical journal 和物化事实表中。发布开关只改变生产、调度和读取路径，不删除或重写权威数据。

## 独立开关

| 环境变量 | 默认 | 作用 | 关闭后的路径 |
|---|---:|---|---|
| `FOCUS_LOOP_CANONICAL_EVENTS` | `true` | 写入 canonical journal | 权威领域提交继续，Live 投影不再推进 |
| `FOCUS_LOOP_SUPERVISOR` | `true` | 启动分层监督运行期 | 不启动自动 Loop 调度；用于回滚演练或人工维护 |
| `FOCUS_LOOP_MATERIALIZED_FACTS` | `true` | `/facts` 读取物化事实 | 回到隔离的按 Run 查询端口；stable fact detail 不可用 |
| `FOCUS_LOOP_LIVE_API` | `true` | 开放 snapshot 与 resumable SSE | Live API 返回 404，旧查询接口保持可用 |

布尔值接受 `1/0`、`true/false`、`yes/no`、`on/off`；非法值会在启动时失败，避免带着不明确配置运行。

## 启用顺序

1. 开启 canonical event emission，以 shadow 方式比较增量 projection 与 journal rebuild。
2. 开启 supervisor，观察多 Loop 公平性、组件失败和 projector lag。
3. 物化事实 parity 通过后，开启 materialized fact reads。
4. 对测试环境开启 Live API，验证 snapshot 边界、replay、retention 和 backpressure。
5. 验证工作区 Patrol 的统一 Live projection；每个挂载工作区只建立一条 multiplexed stream。旧 Portfolio Map 控制台和 legacy connection adapter 已退役，前端不再提供兼容开关。

后端开关可以独立回退；工作区 Patrol 依赖 Live API，不再支持切回旧前端或旧轮询。

## 长任务读取

部署前应用 `e25c6d7e8f90` 迁移，创建 `loop_live_checkpoints`。首次缓存缺失时重放保留 journal；随后打开或重同步从检查点 sequence 补放尾部事件。快照仍在只读 Repeatable Read 事务内读取单一边界，事务结束后独立保存检查点；旧请求不能覆盖较新的 sequence，缓存写失败不影响已经读出的快照。缓存保存 reducer 的原始版本，响应每次根据当前授权脱敏。

当前快照保留最近 50 个 rounds/patrol sessions、最近 200 个 runs/curators/expansions/directives/facts，并额外保留未完成工作。Observation/Decision 只读取所选实体引用的身份；当前 Context 拓扑保持完整。accounting 用数据库聚合轮次和已结算调用，不携带完整 round_history。Run 权限依赖统一批量读取，200 个 Run 与一个 Run 的依赖查询数量一致。

`GET /desktop/api/agent-loops/{loop_id}/live/history?before_sequence=100&limit=50` 返回一页 `items`（页内正序）、排他的 `next_before_sequence`、`minimum_sequence` 和 `truncated`。省略游标从最新事件开始，limit 上限 200；每页复查权限。前端独立加载历史并提供“更早活动/返回最新”，不把历史事件再次送入 reducer，不推进 SSE 游标。已有 SSE 增量和缺口重同步协议保持原样。

检查点缺失、格式失效或被裁剪边界超越时，从仍保留的 journal 恢复并由权威物化表补齐当前实体。裁剪掉的活动历史不会伪造恢复，历史接口显式返回保留边界。`rebuild` 绕过缓存，供诊断使用。

## 回滚步骤

1. 前端故障时部署已验证的 Patrol 版本；不能通过环境变量恢复已删除的旧控制台。
2. 需要维护 Live 服务时关闭 `FOCUS_LOOP_LIVE_API`，阻止新 Live 连接。Patrol 会显示连接不可用；`/console`、`/conversation`、`/facts` 查询仍可用于诊断，不构成旧前端回退。
3. 如物化事实读取异常，关闭 `FOCUS_LOOP_MATERIALIZED_FACTS`。物化表和 revision history 保留，便于事后对比。
4. 如调度异常，关闭 `FOCUS_LOOP_SUPERVISOR` 并重启。已有 Lease、Run、Directive、Fact 和 Portfolio 数据不删除；恢复前先执行现有 recovery/reconciliation 流程。
5. 只有在 journal 生产本身造成问题时才关闭 `FOCUS_LOOP_CANONICAL_EVENTS`。关闭后 Live projection 会停在最后一致 sequence，权威 Kernel 状态仍保留。

回滚完成后不得清空 canonical journal、fact revisions 或 projector cursors。再次启用时由 replay/rebuild 和 fact backfill 收敛，不从 UI 状态反写权威领域数据。

## 验收信号

- snapshot `last_sequence` 与 replay/rebuild 一致；projector lag 可解释且最终归零。
- 普通 Live event 不触发 snapshot、Portfolio、Evolution、Tree、Audit、Slots、Console、Facts 和 Conversation 的级联请求。
- 一个 Loop 的慢发布或慢 Context 不阻塞同 Loop 的 Run/Fact/Curator，也不阻塞其他 Loop。
- Patrol、directive、Context Run、Tool、Workspace、Fact 和 Portfolio 的可见变化都能追溯到持久事件。

## Run 事件恢复

部署前应用 `f36d7e8f901a` 迁移，为 Run outbox 增加 `lease_expires_at`、`next_attempt_at` 和 `failed` 状态。消费者每轮 drain 都按 Loop scope 回收过期领取；启动恢复只处理过期或旧 schema 遗留的无租期领取，跳过仍持有行锁的处理事务。默认领取租期为 60 秒，处理事务持有事件行锁期间不会被回收。副作用和 delivery receipt 同事务提交，处理异常或取消先回滚，再按 `(event_id, claimed_by, delivery_count)` 归还本次领取；旧领取不能投递或归还新领取。

失败按 1、2、4…秒持久化退避，上限 60 秒；默认 8 次领取仍未成功则进入 `failed`，保留错误和领取次数，停止自动重试。可查询 `run_outbox_events` 的 `status`、`error`、`delivery_count`、`next_attempt_at` 和 `lease_expires_at` 定位问题；恢复处理器后，未耗尽重试的事件无需重启即可继续消费。`delivery_count` 同时作为领取代次，不应重置。数据库暂时不可用导致异常归还失败时，后续正常 tick 通过过期租约恢复。降级迁移保留事件、错误和 receipt，将 `failed` 转为旧版支持的 `error`。
