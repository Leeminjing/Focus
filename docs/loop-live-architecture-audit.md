# Live Loop 架构审计

## 责任边界

- `LoopLiveSnapshotService` 只协调 sequence 边界、projector、overlay 和脱敏策略。
- `LoopLiveProjectionOverlay` 只把 current 领域实体装配成 snapshot。
- `LoopLiveAccessPolicy` 与 `LoopLiveRedactionPolicy` 分别负责授权解析和内容脱敏。
- `LoopLiveEventFeed` 只负责有界 replay/backpressure 协议。
- 前端 schema、reducer、selectors、store、connection 和视图各自独立；`app.js` 在 Patrol 工作台和全图挂载时统一通过 `FocusLoopLiveConnection.create` 创建连接，并管理对应页面生命周期，不再选择旧连接器或启动旧 Loop 轮询。桌面机器人面板也使用这套 Live 模块。
- `FocusLoopLiveStore` 保存当前 Live projection，并分别维护连接状态、活动历史分页与局部 UI 状态；snapshot 经 schema 校验后原子替换，canonical event 由 reducer 归约，selectors 为 Patrol 与全图派生只读视图。旧 `LoopStore`/`LoopConsoleStore` 已不存在。

## 类与函数

- 新增类没有共享“万能”基类，也没有把网络、持久化、投影和 DOM 放进同一个对象。
- 核心方法公开，辅助方法使用 `_` 前缀或模块私有函数。
- 长查询组装已从传输模块移入 `LoopLiveProjectionOverlay`；不同实体仍在一个 snapshot 事务中读取，以维持同一 sequence 边界。
- `LoopSupervisor` 只管理组件生命周期；admission、Context Run、Curator、发布和 Fact 投影各自拥有独立端口与持久队列。
- `FactProjector` 只推进游标和协调 materializer；source reader、identity、lifecycle、verification 与 query 分属独立模块。
- 前端 connection 负责 snapshot、SSE 续传、缺口重同步及有界退避，通过 store 提交领域更新；store 调用 schema 和 reducer，视图与控制器通过 selectors 读取投影。活动历史独立分页，不再次归约为当前领域状态，也不推进 SSE 游标。
- 手工职责复核未发现同时承担调度、持久化、投影、传输与展示的 coordinator/store god object。

## 文件头注释

新增 Live Loop Python/JavaScript/CSS/Test 文件均在文件头声明：对外提供内容、输入、输出、具体工作流和示例。修改过的视图、Store、API、preload、index 与网关头部也已更新。

记录的例外：

- `desktop/src/renderer/app/app.js`、`backend/app/gateway/app.py` 和若干既有领域文件包含本次变更前已有的必要运行时说明；本次没有向文件中部新增解释性注释，也没有为了形式要求删除仍在解释安全/兼容不变量的既有注释。
- ORM registry 的 `# noqa: F401`、类型检查抑制和测试 fixture 标记属于工具指令，不作为业务注释。
- OpenSpec、迁移说明和 Markdown 文档不受“源文件仅文件头注释”限制。

`backend/tests/test_loop_architecture_acceptance.py` 按当前模块清单检查 Agent Loop、Loop 测试、前端 Live 模块及图样式的文件头五要素和文件中部说明性注释；同时检查传输层不得重新导入领域装配模型，以及 `app.js` 不得重新拥有 canonical reducer 或旧 stream/poll 刷新入口。`desktop/src/renderer/features/loop/loop-live-projection.test.js` 覆盖单连接、续传、重试、取消、缺口重同步与独立历史分页；测试结果以当前回归记录为准。
