# Desktop 模块导航

`app.py` 注册桌面接口和静态资源；`composition/service.py` 装配共享资源、适配领域端口，负责启动、关闭和持久派发循环。HTTP 路径及请求合同保持兼容。

| 功能 | 归属 |
| --- | --- |
| HTTP、SSE、请求校验 | `api/`；显式浏览器接口随 `browser/` 维护 |
| Context 生命周期与派生 | `contexts/` |
| 持续策展、模型策展、revision 演化 | `contexts/curation/`、`contexts/curator/`、`contexts/evolution/` |
| 普通 Patrol、Context Patrol、Agent Loop | `patrol/` |
| Run 准入、恢复、装配、结算、执行尝试 | `runs/` |
| 会话查询、输入交付、历史、压缩 | `conversations/` |
| 材料上传、读取、归属、分组、快照与监测 | `materials/` |
| 工作区、执行隔离、文件链接与文档预览 | `workspaces/` |
| 协作、Inbox、Swarm | `agents/` |
| Memory、Skills、Strategy、模型配置 | `memory/`、`capabilities/` |
| ORM 注册、存储约定、状态通知、维护 | `persistence/` |

`api/schemas.py` 是独立的请求合同；`persistence/models.py` 定义原有桌面 ORM；各领域新增实体仍由自己的 `models.py` 定义，通过 `persistence/registry.py` 汇集。请求合同可以单独导入，不加载 ORM。

业务实现接受各自的 `Protocol` 依赖端口，领域模块不导入组合根。同领域内部调用留在自身实现，只有外部依赖进入端口。`DesktopService` 保留既有调用接口，通过明确的转发方法适配这些端口，并按需复用领域实例；它不再实现材料事务、会话投影、运行准入、恢复或协作规则。已有异步事务、锁顺序及回调注入点保持一致。新功能应放在对应领域，跨领域协作通过实际需要的端口表达。

`patrol/agent_loop/` 保留 Kernel、Coordinator、Patrol、Supervisor、Service、请求合同、模型及路由入口。支撑机制分别进入 `facts/`、`observations/`、`completion/`、`missions/`、`execution/`、`recovery/`、`projections/`、`live/`、`journal/`、`resources/` 等目录。原有 `context_expansion/`、`context_recovery/`、`task_progress/` 和 `compression_authority/` 保持完整。

`curation` 与 `curator` 的职责和持久化身份分别保留。材料的文件生命周期属于 `materials/`；逐轮材料准入和运行历史属于 `runs/`。文件历史的版本选择和工作区权限属于 `workspaces/`，HTTP 模块只适配请求与响应。

主运行输入通过 `runs/intake.py` 受理，保持引用授权、Loop 优先分流、持久化幂等查询及派发通知的顺序。HTTP 入口只提供已验证的请求、幂等头及运行/Loop 端口。

架构回归入口：`backend/tests/test_desktop_domain_architecture.py`。重构记录与验收方式见 [desktop-module-refactor.md](../../../docs/desktop-module-refactor.md)。
