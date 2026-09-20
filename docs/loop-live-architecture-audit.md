# Live Loop 架构审计

## 责任边界

- `LoopLiveSnapshotService` 只协调 sequence 边界、projector、overlay 和脱敏策略。
- `LoopLiveProjectionOverlay` 只把 current 领域实体装配成 snapshot。
- `LoopLiveAccessPolicy` 与 `LoopLiveRedactionPolicy` 分别负责授权解析和内容脱敏。
- `LoopLiveEventFeed` 只负责有界 replay/backpressure 协议。
- 前端 schema、reducer、selectors、store、connection 和视图各自独立；`app.js` 只选择连接器并管理页面生命周期。
- `LoopStore`/`LoopConsoleStore` 是兼容视图状态与分页 UI 缓存，不再归约 canonical 领域事件。

## 类与函数

- 新增类没有共享“万能”基类，也没有把网络、持久化、投影和 DOM 放进同一个对象。
- 核心方法公开，辅助方法使用 `_` 前缀或模块私有函数。
- 长查询组装已从传输模块移入 `LoopLiveProjectionOverlay`；不同实体仍在一个 snapshot 事务中读取，以维持同一 sequence 边界。
- `LoopSupervisor` 只管理组件生命周期；admission、Context Run、Curator、发布和 Fact 投影各自拥有独立端口与持久队列。
- `FactProjector` 只推进游标和协调 materializer；source reader、identity、lifecycle、verification 与 query 分属独立模块。
- 前端 connection、schema、reducer、selectors、store 和视图之间为单向依赖；兼容 Store 只消费 selector 结果，不拥有 canonical reducer。
- 手工职责复核未发现同时承担调度、持久化、投影、传输与展示的 coordinator/store god object。

## 文件头注释

新增 Live Loop Python/JavaScript/CSS/Test 文件均在文件头声明：对外提供内容、输入、输出、具体工作流和示例。修改过的视图、Store、API、preload、index 与网关头部也已更新。

记录的例外：

- `desktop/app.js`、`backend/app/gateway/app.py` 和若干既有领域文件包含本次变更前已有的必要运行时说明；本次没有向文件中部新增解释性注释，也没有为了形式要求删除仍在解释安全/兼容不变量的既有注释。
- ORM registry 的 `# noqa: F401`、类型检查抑制和测试 fixture 标记属于工具指令，不作为业务注释。
- OpenSpec、迁移说明和 Markdown 文档不受“源文件仅文件头注释”限制。

自动化架构测试覆盖全部 40 个新增 Agent Loop 模块、13 个 Loop 测试模块、8 个前端模块及 Live CSS，逐一检查文件头五要素和文件中部说明性注释；同时检查传输层不得重新导入领域装配模型，以及 `app.js` 不得重新拥有 reducer、SSE 重试或全量刷新链。最终审计命令产生 126 个通过断言。
