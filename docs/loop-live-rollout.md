# Live Loop 发布与回滚

Live Loop 的权威状态始终保存在原有 Loop 领域表、canonical journal 和物化事实表中。发布开关只改变生产、调度和读取路径，不删除或重写权威数据。

## 独立开关

| 环境变量 | 默认 | 作用 | 关闭后的路径 |
|---|---:|---|---|
| `FOCUS_LOOP_CANONICAL_EVENTS` | `true` | 写入 canonical journal | 权威领域提交继续，Live 投影不再推进 |
| `FOCUS_LOOP_SUPERVISOR` | `true` | 启动分层监督运行期 | 不启动自动 Loop 调度；用于回滚演练或人工维护 |
| `FOCUS_LOOP_MATERIALIZED_FACTS` | `true` | `/facts` 读取物化事实 | 回到隔离的按 Run 查询端口；stable fact detail 不可用 |
| `FOCUS_LOOP_LIVE_API` | `true` | 开放 snapshot 与 resumable SSE | Live API 返回 404，旧查询接口保持可用 |
| `FOCUS_LOOP_FRONTEND_PROJECTION` | `true` | 使用统一前端 projection | Electron renderer 使用隔离的 legacy connection adapter |

布尔值接受 `1/0`、`true/false`、`yes/no`、`on/off`；非法值会在启动时失败，避免带着不明确配置运行。

## 启用顺序

1. 开启 canonical event emission，以 shadow 方式比较增量 projection 与 journal rebuild。
2. 开启 supervisor，观察多 Loop 公平性、组件失败和 projector lag。
3. 物化事实 parity 通过后，开启 materialized fact reads。
4. 对测试环境开启 Live API，验证 snapshot 边界、replay、retention 和 backpressure。
5. 最后开启 frontend projection；浏览器只建立一条 multiplexed stream。

每一步都能独立回退。默认路径不会同时运行旧轮询和 Live stream。

## 回滚步骤

1. 先关闭 `FOCUS_LOOP_FRONTEND_PROJECTION`，renderer 切回 legacy adapter；已有 projection 保留但不再作为 UI 权威。
2. 关闭 `FOCUS_LOOP_LIVE_API`，阻止新 Live 连接；旧 `/console`、`/conversation`、`/facts` 查询仍可使用。
3. 如物化事实读取异常，关闭 `FOCUS_LOOP_MATERIALIZED_FACTS`。物化表和 revision history 保留，便于事后对比。
4. 如调度异常，关闭 `FOCUS_LOOP_SUPERVISOR` 并重启。已有 Lease、Run、Directive、Fact 和 Portfolio 数据不删除；恢复前先执行现有 recovery/reconciliation 流程。
5. 只有在 journal 生产本身造成问题时才关闭 `FOCUS_LOOP_CANONICAL_EVENTS`。关闭后 Live projection 会停在最后一致 sequence，权威 Kernel 状态仍保留。

回滚完成后不得清空 canonical journal、fact revisions 或 projector cursors。再次启用时由 replay/rebuild 和 fact backfill 收敛，不从 UI 状态反写权威领域数据。

## 验收信号

- snapshot `last_sequence` 与 replay/rebuild 一致；projector lag 可解释且最终归零。
- 普通 Live event 不触发 snapshot、Portfolio、Evolution、Tree、Audit、Slots、Console、Facts 和 Conversation 的级联请求。
- 一个 Loop 的慢发布或慢 Context 不阻塞同 Loop 的 Run/Fact/Curator，也不阻塞其他 Loop。
- Patrol、directive、Context Run、Tool、Workspace、Fact 和 Portfolio 的可见变化都能追溯到持久事件。
