# Focus 前端业务迁移清单

状态：实施前清单，等待用户审阅；所有验收项均未在本次改造中执行。

阅读基线：2026-10-09，源码 HEAD `250875a`。已有 `docs/synthesis-support-repair.md` 的工作区修改与本改造无关，保留原样。

需求来源：用户指定的 ChatGPT 对话「Focus前端设计重构」，conversation `6ac8e5c5-fc28-83ec-aec9-04e5185408f1`。已通过 read_thread 读取该对话的用户要求和交付说明。原对话 ZIP、HTML、总体图和细节图目前只有 `sandbox:/mnt/data/...` 链接，当前会话没有其文件内容；不能宣称已看过原图或已验证视觉一致。实施前须取得原包并完成图文对应确认，优先上传 `Focus-frontend.zip`。

## 已确认的新交互

- 主导航为 Patrol、任务、全图、插件、无工作区模式、记忆库；设置保留独立全局入口。
- Patrol 是工作区统一输入入口。默认主内容区只有输入框，工作区选择、输入类型、发送反馈、待处理提醒和查看详情入口收在输入区中。发送后不自动展开详情或变成聊天记录。
- 展开详情后：左侧上下文集合关系图，右侧 Task Progress 与 Observation，下方 LoopFact，输入框持续保留。
- Context 检查抽屉包含概要、会话、来源与版本；检查不改变 Patrol 发送目标。只有明确点击「在任务中打开」才进入传统任务页。
- 任务页保留用户选择工作线的单线程工作方式，任务列表、会话、材料检查器三栏。其草稿、装备、材料和运行状态与 Patrol 输入分离。
- Task Progress 表达已提交任务记忆；Observation 表达本轮冻结决策依据；LoopFact 表达执行事实。三者不能相互冒充。
- 雾白画布、银灰侧栏、系统字体、柔和浮层、克制蓝色、留白和按需展开构成视觉方向。具体尺寸、排布和细节以取得的原图为核对依据。

## 迁移判定规则

「保留」维持业务合同；「调整」改变布局或交互但保留能力；「合并」把入口或重复呈现并入新位置，不能删除业务；「拟废弃」必须得到用户确认才能实施。原型没画出的能力仍列入迁移范围。

接口均以 `/desktop/api` 为前缀。表中「接入方式」是源码中已经存在的调用或明确标注的新增范围，不表示当前新界面已经实现。

## 已有能力 → 新界面承接 → 接入 → 验收

| ID | 已有能力与现有入口 | 处理 | 新界面承接位置及真实接入方式 | 验收方法 |
| --- | --- | --- | --- | --- |
| M01 | index.html 九项主导航及设置、app.js render/handleDocumentClick | 合并 | 六项主导航；上下文、代理、Agent Loop 的业务入口迁入任务检查器及工作详情；设置不消失 | 六页逐一进入；原三入口全部有等价可达路径；空库也能访问插件、记忆库、无工作区模式 |
| M02 | bootstrap、连接失败重试、动态 loopback/session/CSP | 保留 | 原 bootstrap API、HttpResponse decoder、preload 白名单；启动默认 Patrol，不隐式选择任务作为发送对象 | 后端不可用→重试恢复；错误保留因果；Electron 桥及 CSP 不放宽 |
| M03 | workspace-patrol-picker 原生文件夹选择、确认、取消 | 调整 | 输入框内工作区按钮打开选择/确认浮层；沿 selectWorkspace→POST /workspaces；无原生桥的浏览器入口明确提示宿主能力缺失 | 选择不启动执行；确认仅登记绑定；取消不丢原绑定；迟到响应不能覆盖新页面 |
| M04 | 工作区 Patrol 当前绑定、首次输入创建、终态重新启用 | 保留 | GET /agent-loops/workspace/{workspace_id}、POST /inputs、POST /restart；入口仍在 Patrol 输入区/详情控制 | 首输入前无虚构 Run；终态显式重启；旧输入/成果可追溯 |
| M05 | 四类输入、指定补充回答、稳定 submission_id、失败重试 | 调整 | workspace-patrol-inputs 保留；POST /agent-loops/workspace/{id}/inputs；类型/回答对象在输入框内显式显示 | 同 ID 重试幂等；失败正文可找回；回执到达不覆盖后来草稿；回答请求身份匹配 |
| M06 | 用户输入历史、最近记录、加载更早记录 | 调整 | 输入框内「输入记录」浮层，GET /inputs?before=；默认页不铺历史 | 发言后仍折叠；完整分页；sending/accepted/failed 与任务生效分离 |
| M07 | 工作区控制器、Live Store/Connection、断线与重同步 | 保留 | 复用 loop-live schema/reducer/selectors/store/connection 和 /live、/live/stream；折叠详情不伪造状态 | 断线保留最后一致视图；缺口重同步；切换工作区无迟到污染、无重复订阅 |
| M08 | 当前 Task Progress、长期记忆版本和 blocked retry | 调整 | Patrol 详情卡/独立检查浮层；Live task_progress 与 GET /agent-loops/{id}/task-progress；原 retry API | pending 不替换已提交 head；版本/证据与服务器一致；blocked 重试走真实入口 |
| M09 | 冻结 Observation/LoopDecisionInputs 已持久化，当前 UI 无完整读面 | 调整 | 增量只读 Observation query，见下文 G01；按当前 round.observation_id 精确读取，独立显示前序冻结 Progress 与当前 head | 无 Observation 真空态；跨轮不混读；历史前序不替换成最新进度；私有模型正文不暴露 |
| M10 | 已提交 Revision DAG 与 PortfolioMapView | 调整 | Patrol 关系图复用 /agent-loops/{id}/lineage；简化节点为名称、状态、必要版本；目的/结果放抽屉 | 多来源和 Context 回流不丢 revision 身份；未发布候选不作为已生效边；状态变化不抖动布局 |
| M11 | Context 完整会话、版本快照、消息 provenance | 调整 | 概要/会话/来源与版本抽屉；/contexts/{id}/conversation、/contexts/{id}/revisions、/context-revisions/{id}、/message-provenance | 完整分页与版本固定；检查任意 Context 后发送仍走工作区 intake；显式打开任务才改变模式 |
| M12 | LoopFact 类型/状态/Context 筛选、分页及详情接口 | 调整 | Patrol 详情下方紧凑列表与证据抽屉；复用 facts view/selectors 和 /facts、/facts/{fact_id} | 排除 tool 主列表；unknown 不计成功；超过缓存上限可分页；来源详情可追溯且遵守权限 |
| M13 | Loop 暂停、继续、停止、撤权、budget/grant 修订、等待恢复 | 合并 | Patrol 工作详情「控制与授权」；复用 control/grant/wait-requests/responses/resume-with-current-mission | 请求中状态与生效状态分开；预算不足、冲突、过期、失败可处理；收口不能伪报完成 |
| M14 | Task-based Agent Loop 激活、Mission 编辑、验收、终态退出及后继 | 合并 | 任务工具栏「持续推进」及其工作详情；保留 task loop 身份，不自动改为 workspace Patrol | 原激活条件、授权、沿用 Mission、退出/后继全部可达；无自动扩权 |
| M15 | Loop 三种 intervention：Context 直接消息、Context 意见、Portfolio 意见 | 合并 | 统一输入面只用于 workspace intake；旧 intervention 放任务 Loop 的明确高级操作，保留 scope/目标标签 | 抽屉选择不成为目标；每条高级操作用户明确确认目标；意见不偷入 Agent 历史 |
| M16 | Portfolio、Expansion/Curator、adoption、压缩与用量审计 | 合并 | 工作详情/Context 来源版本/控制与授权的次级检查页；复用 console/audit/program/slots | 保留 ready/committed/applied 差别、未采用结果、失败与用量；主页面不堆审计正文 |
| M17 | 任务新建、列表、切换、Context family 与 ui-state | 调整 | 任务页左栏；POST /tasks、GET /tasks、GET /tasks/{id}、PUT /ui-state；派生任务仍可显式进入 | 空任务/加载/失败；多任务草稿与滚动隔离；切换不误停后台 Run |
| M18 | Main 发送、材料/技能/权限装备、幂等与 SSE | 调整 | 任务会话及 composer；复用 /tasks/{id}/main/runs 与 /runs/{id}/stream、TaskRunOperations | 普通 Run 与 Loop user_message 回执分开；accepted 不当 running；发送失败保留草稿；实际 Run 与 UI 一致 |
| M19 | 会话 Human/Assistant/Tool、reasoning、流式 Markdown、早期历史 | 保留 | 复用 conversation-render/view/events/reconciler；工具摘要与详情按需展开 | 真 SSE 增量、重复终态去重、早期分页、滚动锚点、正文/tool 输出分离 |
| M20 | 当前 Run 中断、resume、Must-view retry/cancel | 保留 | 任务 composer 及必要状态面板；cancel/resume 原接口，access/Must-view 真 gate | 加载/取消/失败状态不阻断合法恢复；UI 不伪造工具已经看图 |
| M21 | /commit 九阶段、确认、修订、放弃与 durable handoff | 保留 | 任务会话内任务合同面板；原 resume/commitment/abandon 链路 | 审批点、取消、恢复、合同交付幂等；不删除未在设计图出现的流程 |
| M22 | 手工 Context 派生、多来源编排、定义编辑、projection 接受/拒绝 | 合并 | 任务检查器「上下文」与全图节点操作；复用 context-editor 和 derive/definition/projection-decision | 来源冻结、Tool Exchange 校验、未批准不可运行；修改版本仍经过原服务 |
| M23 | 快捷/阈值压缩、摘要/删除/恢复及来源查看 | 保留 | 任务菜单「整理上下文」和 gate 面板；compression-panel、quick-preview/quick-apply、summaries/resume | 原文可恢复；范围保护、预算和协议错误真实展示；不截断替代压缩 |
| M24 | Session Patrol 文档来源、拖拽、JSON 编辑、撤销、保存冲突 | 保留 | 任务高级操作及全图「装备会话 Patrol」打开已有工作台；命名与 Portfolio Patrol 区分 | 来源目录、虚拟列表、IME、未完成 JSON 保存、冲突本地保留、不丢草稿 |
| M25 | Session Patrol preview/deploy/restart/continue/resume/definition audit | 保留 | 现有 workbench/branches/review；draft preview token 与 durable deploy 原接口 | 模拟调用不变真实证据；编译失败不能投放；continue 固定 checkpoint；旧定义审计可读 |
| M26 | 单 Lane Context Curator 的跟踪、暂停/恢复/停止、版本审计 | 合并 | 任务代理检查器及受管 Context 检查；context-curation/state、history/retry 原接口 | 保留 root/managed 关系、发布/准备差别、分页、失败重试；Loop ownership 边界不改变 |
| M27 | Teammate/Worker/Swarm 查询、历史、retry/continue/cancel | 合并 | 任务检查器「代理与运行」；复用已有 agents 和 run 接口 | 旧协作历史及工具结果可查；Loop-owned 执行仍由后端拒绝裸自主启动 |
| M28 | 材料上传、路径登记、预览、图片 Blob/放大、插件文档查看 | 调整 | 任务页右材料检查器、无工作区模式相同控件；原材料接口/MaterialContentLoader/plugin opener | 真文件内容、加载失败、Blob 回收；PDF/DOCX/空间查看器仍可打开和返回 |
| M29 | 材料读写/指令模式、编辑、清空、删除、版本恢复 | 保留 | 材料详情/更多操作；PUT /materials、clear/delete/versions/restore | 保存和回滚基于真实版本；失败不显示已保存；既有破坏性确认保留 |
| M30 | 材料自动/手工分组、组 CRUD、拖拽与排序 | 保留 | 材料检查器分组列表；material-grouping 及 material-groups/order/members 原接口 | 文件/分类两种视图；跨组排序持久一致；失败恢复旧顺序 |
| M31 | 每 Run 选材、备注、必看图片、使用历史 | 保留 | composer 选材按钮及右侧材料标记；run-material-picker/image-material-picker/material-history | 仅本轮选材与历史选材分开；备注快照；必看依赖仍受真实模型能力与预算约束 |
| M32 | 全图工作区/根/派生层次、折叠、卡片、键盘、选择任务 | 调整 | 全图保留层次浏览，增加本地检索及关系图查看/缩放；复用已有树与 Portfolio 图，不新建图引擎 | 跨工作区定位；多来源不当树唯一真相；缩放不等于改变领域拓扑；键盘可用 |
| M33 | Context archive/unarchive/delete/batch/cascade，祖先墓碑 | 保留 | 全图及任务更多菜单；归档列表仍在设置数据页 | 既有确认、cascade、不可删除原因；保留被后代引用祖先；不操作用户真实数据做测试 |
| M34 | 全图「装备小兵」与会话 Patrol 可拖拽头像 | 调整 | 全图节点「装备会话 Patrol」与任务中的相同显式操作；旧拖拽入口迁至次级操作，能力不删 | 点击和键盘替代入口可完成同用例；拖拽如保留可用；头像与真实会话位置对应 |
| M35 | 插件状态、依赖、注入冲突、接口、轨迹、重载与前端 assets | 调整 | 插件页列表/详情、真实状态筛选和检索；/plugins、/traces、/reload；原 assets 注册与文件 opener | active/unavailable/rejected/empty；真实 reload 结果与资源可达；不把 UI preview 当启用 |
| M36 | 无工作区模式隐藏 assembly task、独立输入/材料 | 调整 | 独立主导航页面；复用 GET /assembly/task 和已有 Main/材料链，隐藏宿主工作目录不当用户工作区 | 空任务库也能进入；其草稿/材料与任意普通任务及 Patrol 隔离；只按实际权限运行 |
| M37 | 记忆 CRUD、全文/分段、会话/消息/文字采集、合并和再压缩 | 调整 | 记忆库检索/列表/编辑面；memory.js 与 /memory、/summarize | 真新增/保存/删除；来源引用和分段保留；压缩失败不替换正文；任务引用仍可选择 |
| M38 | 模型 CRUD/default/curation_default、密钥轮换、连接测试 | 保留 | 全局设置「模型」；原 model_settings API、配置冲突确认 | 成功基于持久设置；错误字段可修正；密钥不进 URL/截图/日志；不另建配置状态 |
| M39 | 访问模式、单次审批、后台 accessPending、Windows sandbox 状态 | 保留 | 全局受限提醒与任务装备；Patrol 折叠态在输入区提供待授权入口，详情处理 | 用户拒绝/过期/单次许可不扩权；折叠状态不漏需处理门禁；安全状态不由视觉开关更改 |
| M40 | 中文/英文、整页缩放、导航/检查器调宽、窗口与快捷键 | 保留 | 新 shell/token 样式；原 i18n/preload zoom，页面布局偏好与领域状态分开 | 两语言、键盘、Esc/焦点返回、窄屏、200% 缩放、减少动画；旧布局偏好合理读取/钳制 |
| M41 | 模型输出安全 Markdown、文件/外链、session header 与资源路由 | 保留 | 现有 decoder/renderer、shell openExternal、CSP 与 Blob 载入 | 内容转义、危险链接拒绝、受保护资源访问；不为重构关闭隔离 |
| M42 | 概念升级后的「查看 Context」与「给 Context 发言」分离 | 调整 | 检查 target 使用 inspected_context_id，本轮普通消息目标始终 workspace_id；明确进入任务才用 activeTaskId | 抽屉/图/版本/事实四种检查后分别发言，网络断言均为 workspace intake |
| M43 | 旧 UI 中重复 Portfolio/Inspector 大块渲染与多层 CSS 覆盖 | 合并 | 迁移到单一现有能力组件和新版 tokens；同职责实现完成接入后删旧调用与样式 | 搜索全部 callers/imports/selectors 和插件依赖；迁移清单与测试证明可达后才删 |
| M44 | 原型固定节点、4/7、模拟 Run/进度/连接/成功、离线测试 | 拟废弃 | 只保留为隔离视觉 fixture；生产模块禁止运行模拟状态和硬编码业务结果 | 审批确认后，生产网络与数据库状态对照；没有事实时明确空/unknown，不产生 4/7 |
| M45 | 原型插件「预览开关」，当前宿主无插件 enable/disable 写接口 | 拟废弃 | 生产使用真实状态与重载；若原包证明是纯视觉预览，可作为明确标注的非业务预览保留，不承诺启停插件 | 审批时确认范围；禁止成功样式伪造插件生效；持久插件启停属于另行确认的后端能力 |

## 能力缺口与最小新增范围

### G01：Observation 的用户读面

已有：LoopObservation.envelope、LoopDecisionInputs.payload、不可变 previous_progress/task_delta/committed_lineage、Live round.observation_id、TaskProgressQuery 的 refs/hash/readiness。缺失的是公开、安全、精确的 Observation 读接口，不是 Observation 冻结或 Round 机制。

方案：在既有 query_routes 增加 `GET /agent-loops/{loop_id}/observations/{observation_id}`，独立只读 query 读取原有实体，校验归属，并沿用当前会话保护及 Live access/redaction 规则。返回必要的 round/Observation/前序 Progress/冻结来源/Lineage 身份与摘要；大型来源和前序条目按稳定身份有界分页。当前 Progress 从原 head 读取，必须与冻结 previous_progress 分栏并各自标明版本；不把最新 head 回填历史。

范围不含新表、事件、writer、调度器、模型调用或通用查询框架；不返回原始 Worker prompt、推理、凭据及未授权证据。不把完整模型 envelope 直接序列化到浏览器。实现需经本次 artifacts 审批。

### G02：全图检索与图缩放

当前完整任务/树列表、已有 Portfolio SVG 和偏好已足够承接。检索是对已加载数据的本地筛选并明确范围；图缩放/平移只保存视图偏好。没有全量资料时不得称全局全文检索。无需新增索引后端、图数据库或图依赖；不以截图平面替代真实拓扑。

### G03：原型文件和插件开关

原图/源包已由用户提供，逐页映射见 OpenSpec reference-map.md。M44/M45 随用户调用 apply 获得确认：生产不采用模拟数据和假插件启停，使用原有真实状态、详情及重载接口；未新增配置 writer。

## 旧实现清理和兼容范围

- 主导航中的上下文、代理、Agent Loop 合并入口，不删除 Context 编辑、协作、task Loop 或诊断能力。
- 调整 workspace-patrol-* 原模块，不在旁边长期保留新版第二套 Patrol 控制器/输入 Store。
- 复用 Live reducer/connection、conversation、材料、Session Patrol 与插件接口；移出 app.js 的实现须删除原副本和对应 handler。
- legacy Live connector 属于既有部署开关回退，不因导航迁移直接删除。先查配置、测试与旧历史消费者，再决定本次是否已无引用；不得在没有证据时称其废弃。
- 原 task-based Loop 和 workspace Patrol 的交互模式是两个合法业务身份，兼容入口必须保留；同一领域数据的 UI 呈现可以合并，共享组件不代表混用 owner。
- 任何删除以已完成迁移行、caller 证据、受影响检查通过为前提，不能以「图里没画」或「目前没挂载」为理由。

## 验收与交付记录

逐项实施结果、验证范围及未实测边界见 [迁移验收记录](focus-frontend-acceptance.md)。本表仍是能力与新入口的对应依据，验收记录区分真实接口、受控故障/视觉 fixture 和外部 Provider 未实测范围。

必须同时具备：针对性的纯状态/协议回归、真实 FastAPI/隔离 PostgreSQL 请求链、浏览器交互与原图视觉核对。Mock 只用于可控异常及独立组件，不能替代关键路径的真实业务接入。测试数据不使用用户工作区/正在运行的 Loop。

至少覆盖 Patrol 首输入与连续输入、失败幂等重试、折叠与检查后发送不改目标、Observation 精确版本、等待/预算恢复、Live 重连、任务 Run/SSE 与草稿隔离、材料/图片、Context 编辑/压缩、Session Patrol、全图生命周期、插件、无工作区模式、记忆和设置。

交付分别统计业务代码与测试代码新增/删除/净增行数；纯搬迁、样式、Observation 读面和必要 UI 状态为主要来源说明。禁止压缩格式或删除必要测试/文件头来降低表面行数。只有跨执行脊柱或共享领域的实际改动/失败证据要求时才跑全量测试。

## 源码核对位置

- desktop/index.html；desktop/app.js 的 bootstrap/render、Patrol/Loop、任务、材料、Context、Session Patrol、全图、记忆、设置与事件入口。
- desktop/workspace-patrol-{picker,inputs,controller,view}.js；desktop/loop-api.js；desktop/loop-live-{schema,reducer,selectors,store,connection}.js。
- desktop/portfolio-map-view.js；desktop/context-conversation-view.js；desktop/loop-{console-controller,console-store,facts-view,wait-request-view,mission-editor}.js。
- desktop/conversation-{render,events,reconciler,view}.js；desktop/run-material-picker.js；desktop/material-content-loader.js；desktop/material-grouping.js。
- desktop/patrol-workbench.js 和 authoring/document/source/assembly/review/branches；desktop/plugin-view.js；desktop/memory.js；desktop/preload.cjs/main.cjs。
- backend/app/desktop/routes.py、plugins_routes.py、memory_routes.py、model_settings_routes.py；agent_loop 的 routes/query_routes/live_routes/task_progress/query/live_access；session_patrol/deployment 与 run_orchestration。

本清单建立完成后，才创建本次 OpenSpec proposal/design/specs/tasks；清单属于它们共同的业务保留依据。
