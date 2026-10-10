# Focus 前端迁移验收记录

基线：250875a6686603d559ee0e694d24639d3559b2aa。2026-10-10，在用户提供源包并调用 apply 后实施。原有 docs/synthesis-support-repair.md 修改保留；没有 commit、部署、安装版重启或用户业务数据测试。

## 实现边界

页面仍采用原生 HTML/CSS/JavaScript 与浏览器 DOM，没有引入前端框架。下文的 Node 指运行 JavaScript 回归测试的工具；现有 Electron 主进程也使用 Node，未新增产品 Node 依赖。

六项导航、安静 Patrol、显式工作详情、Context 三页签、传统任务三栏、全图、插件、独立会话、记忆与设置使用原业务入口。新增业务模块只有冻结 Observation 查询，以及共用的只读 Context/Observation 用户视图。没有新领域表、调度器、Round 状态机、执行链、图引擎或前端框架。

TaskProgress 精确历史参数、事实 outcome_status 过滤和统一当前脱敏，是对既有读合同的修正。授权表单、收窄解析和等待表单解析公开复用；任务整理上下文复用 quick-apply，并保留既有 gate 的 resume。生产不加载原型 model.js；插件开关由真实状态/重载承接。

## 证据索引

| 编号 | 数据与方法 | 已验证范围 |
| --- | --- | --- |
| B1 | test_observation_inspection、workspace_patrol_contracts、round_task_progress、materialized_facts（排除旧迁移 fixture） | 21 passed / 1 deselected；135 项冻结分页、精确身份、跨 Loop 拒绝、当前权限变化、零写副作用、事实业务结果与验证状态分开 |
| B2 | run_material_inputs、memory_library、desktop_run_stream_lifecycle、model_settings_api、context_revision_lifecycle | 30 passed；材料快照、记忆 CRUD/注入、流终态、模型持久配置、引用祖先保留 |
| B3 | desktop_poc 的 Main 端到端、resume、commitment 恢复/放弃、PostgreSQL draft/material | 4 passed；真实 HTTP/图/RunManager/SSE/checkpoint，受控模型采样 |
| B4 | loop_wait_authority_convergence、loop_control_admission_race、plugins | 58 passed；预算响应重放、冲突/撤权/晚到/停止、插件解析/拒绝/依赖 |
| B5 | session_patrol_workbench_api 实际 graph 与 Electron API 用例 | 2 passed；真实编译/历史/投放、来源与网络失败恢复，受控模型采样 |
| B6 | context_projection、revision_dag、evolution_graph、curation_contracts、semantic_lifecycle | 21 passed；多来源、引用历史、approval_required、Tool Exchange 与阶段合同 |
| B7 | context_patrol_service 的投放/暂停恢复/配置修复重试 | 3 passed；真实控制/持久 revision 与失败恢复，采样受控 |
| B8 | test_frontend_task_real 与 loop_curation_ownership（排除旧降级迁移 fixture） | 12 passed / 1 deselected；真实材料分组排序/归属/非空删除拒绝、Loop 与旧 Curator ownership 排他，传统任务链同时复测 |
| U1 | workspace-patrol-page.test，完整生产页面与明确隔离 HTTP fixture | 四类首输入、目录确认/取消/乱序、草稿/幂等重试、IME、默认折叠、跨 Round 不覆盖检查、预算目标隔离、等待草稿与 text 合同、直接消息回执、六页空库、插件/记忆失败重试、设置、Esc/焦点、390px/200% |
| U2 | test_frontend_migration_real | 真实 PostgreSQL/FastAPI/HTTP/SSE：四类受理、生产 Bootstrap/冻结读取、Context 完整会话、暂停后第五条表达；数据库 5 条意图、0 条虚假 Run |
| U3 | test_frontend_task_real | 真实生产 HTTP 页面/插件资产/磁盘材料预览与选材/Main Run/SSE/数据库终态/使用记录、独立会话身份；900px/390px 截图。外部模型采样替换，其他链路真实 |
| R1 | desktop targeted Node 45 passed | commitment 四类回归、必看图片/材料 loader、模型设置、会话小兵、Live schema/reducer/单连接/重连/缺口/取消/迟到、材料 UI |
| R2 | composer、wait、context-editor、compression、loop/facts、patrol authoring/assembly 的针对性回归 | 任务草稿按归属、页面重绘与语言切换、原有门禁/压缩/派生/事实/UI 生命周期，详见执行命令结果 |
| U4 | context-ui.e2e（生产页面、受控网络） | 全图根/派生/多父、受阻投影、编辑来源、Pointer/键盘重排、滚动/撤销/减少动画、900px 检查器和 Esc 焦点返回；调用适配为任务次级入口，原业务断言保留 |
| S1 | f20 architecture guard、node --check、git diff --check、OpenSpec strict | 原同源/CSP/preload 安全边界、零新增依赖、无产品 CORS/代理更改、语法与 artifacts |

## M01—M45 迁移闭环

“协议回归”表示旧领域机制保持原 API，并有相关回归证据；不代表外部 Provider 或每个高级按钮都已逐一进行生产环境操作。

| 项 | 承接及状态 | 验收证据与范围 |
| --- | --- | --- |
| M01 | 六导航/默认 Patrol，保留 | U1 / S1，空任务库独立可达 |
| M02 | 启动/session/宿主，保留 | U3 生产 HTTP 静态与插件资产，S1 |
| M03 | composer 工作区选择，调整 | U1 原生目录回调确认/取消/迟到，无执行 |
| M04 | 折叠输入与待处理提醒，调整 | U1 / U2，输入不展开、后端暂停仍保持 |
| M05 | 四类输入/指定回答/幂等，调整 | U1 / U2 / B1 |
| M06 | 历史浮层/更早，调整 | U1，失败正文与稳定 submission_id |
| M07 | Live 单连接与恢复，保留 | R1 / U2，UI 离开不控制后端 Run |
| M08 | 提交 Progress/版本/沉淀重试，调整 | B1 / U2；精确版本与当前 head 分开，blocked retry 走原路由 |
| M09 | 冻结 Observation，调整 | B1 / U2，白名单/分页/legacy/隐私/零写入 |
| M10 | 已提交 DAG，调整 | R1 / U2，发布指针变化读取关系，复用现图坐标 |
| M11 | Context 三页签/来源/任务打开，调整 | U1 / U2，检查不变发送身份；显式任务回调使用原 switchTask |
| M12 | facts/filter/分页/详情，调整 | B1 / U2，验证状态与业务结果分别查询，unknown 不计成功 |
| M13 | 当前工作区控制/授权/等待，合并 | U1 / U2 / B4，共用原 grant/wait/recovery API |
| M14 | 任务工具 → task Loop，保留 | 原 activation/Mission 控件与服务调用保留；R1 / B4；全生命周期高级 UI 未逐项外部实操 |
| M15 | 三类 task Loop intervention，保留 | 原控制台模式/明确目标仍在，R1 的接受与交付语义 |
| M16 | audit/adoption/压缩/用量，保留 | 原控制台高级页及查询保留，R1；没有新增审计或采用机制 |
| M17 | 左任务列表/新建/切换，调整 | U1 / U3 / composer 回归，多任务草稿与旧 ui-state |
| M18 | Main/材料/技能/幂等/SSE，调整 | U1 受理不造 Run，U3 真 Run/使用记录，B3 |
| M19 | 消息/Markdown/tool/更早，保留 | R1 / conversation 既有回归 / U3 实际终态会话 |
| M20 | 中断/resume/必看恢复，保留 | B3 / R1，保留原待决面板和接口 |
| M21 | /commit 九阶段与 handoff，保留 | B3 / R1，原确认/修订/放弃与恢复未被新布局替代 |
| M22 | Context 编辑/多来源/投影，合并 | task 检查器与原 context-editor/derive/decision；context-editor 回归/B2 |
| M23 | 手工/关键词/阈值整理，保留 | task 工具显式入口/原门禁/quick-apply，compression 回归/B3/U3 取消无写入 |
| M24 | Session Patrol 编辑，保留 | task 工具/代理/原工作台，B5/authoring 回归 |
| M25 | preview/deploy/continue/resume，保留 | B5 实际图/投放，原 definition audit 入口保留 |
| M26 | 单 Lane Curator，合并 | 代理检查器/原 managed Context 状态与历史，B7 / B8 与既有 curator/presence 回归；全套生产控制未逐项实操 |
| M27 | Teammate/Worker/Swarm，合并 | 代理检查器/原详情、历史、retry/continue/cancel；协议入口保留，R1；外部协作 Provider 未实测 |
| M28 | 上传/登记/Blob/插件预览，调整 | U3 实际磁盘与插件打开/关闭，B3 / R1 loader/图片回收 |
| M29 | 材料规则/清空/删除/版本，保留 | 原 material-action/API，B3 / R1；破坏性操作保留原确认 |
| M30 | 分组/排序，保留 | 原 material-grouping/group APIs；针对性 material-grouping 回归，B8 实际排序/组归属/非空组删除拒绝，U3 材料检查器 |
| M31 | 本轮选材/备注/必看/历史，保留 | B2 / R1 / U3，真实材料使用记录绑定同一 Run |
| M32 | 全图/树/卡片/检索，调整 | U1 与原 map tree/cards，关系图复用 PortfolioMapView；本地检索明确范围 |
| M33 | archive/restore/delete/cascade，保留 | 原全图卡片与设置归档页，B2；未对用户数据执行破坏性验收 |
| M34 | 装备 Session Patrol/头像，调整 | 全图装备与 task 工具/代理入口，R1 / B5；默认隐藏，仅显式唤出 |
| M35 | 插件状态/依赖/冲突/接口/轨迹/重载，调整 | U1 / B4 / U3 实际 assets；补接旧 reload 按钮的实际 writer |
| M36 | 独立会话与材料，调整 | U1 / U3，新 assembly identity 与普通任务输入分离；内部目录不作为绑定的用户工作区，访问模式显示实际作用范围 |
| M37 | 记忆检索/编辑/采集/分段/合并，调整 | U1 / B2，原整理器与正文失败保留路径继续使用 |
| M38 | 模型/密钥/连接测试，保留 | B2 / R1 / U1 设置，不读取或展示用户密钥做截图 |
| M39 | 访问/审批/sandbox，保留 | access-approval / B3 / R1 / U1，原真实权限与门禁 |
| M40 | 中英文/缩放/导航/检查器/键盘，保留 | composer language 回归、U1 Esc/390px/200%、S1；未声称全部历史高级文案已完整英译 |
| M41 | Markdown/链接/资源/插件，保留 | S1 / R1 / U3，同源静态与实际插件文件打开 |
| M42 | 查看身份和发送身份分离，调整 | U1 / U2，Context 检查后的第五条输入仍为工作区 intake |
| M43 | 重复 UI/失效入口，合并 | 删除旧主导航、旧检查重复函数、app grant/answer 解析副本；公共样式原位改造，未知旧业务路径保留 |
| M44 | 模拟 Run/固定进度/假成功，按批准废弃 | 产品只读 API/Live，无原型模型依赖；U2 为 0 虚假 Run，U3 为持久实际 Run |
| M45 | 插件预览开关，按批准废弃 | 真实状态/重载，不新增或承诺插件 enable/disable writer |

## 尚未实测与已知限制

外部 Provider 的整轮自主推进、真实 Windows 系统文件夹对话框的人工点击、用户已安装版、外部 OnlyOffice 服务与全部协作/Curator 高级操作未在用户生产环境实测。相关原业务接口和模块保留，不能把测试采样或视觉 fixture 称作这些外部能力已通过。

两个既有历史迁移测试失败：test_loop_materialized_facts 的 migration_round_trips，以及 test_loop_curation_ownership 的 migration_retires_only_proven_terminal_owners_and_downgrade_keeps_them_safe。两者降级到 f9a0b1c2d3e4 后仍使用含 interaction_mode 的当前 ORM，发生 UndefinedColumn，早于本次查询逻辑。未修改这两个旧 fixture；其余物化事实与 ownership 用例通过，降级失败单独复测并记录，不称为全绿。

## 视觉与代码量

参考映射见 OpenSpec reference-map.md。截图数据分为 .tmp-focus-frontend-evidence 的真实业务页面，以及 Temp/focus-refactor-qa 的受控失败/空/视觉页；图中节点数量与进度来自实际数据，不复刻原型 8 节点/4-7。

隐藏 Electron 截图曾滞后一帧；依据 Context7 的官方 Electron 文档改为 offscreen 软件绘制与 invalidate 后重新生成。原图的雾白/银灰/克制蓝、卡片/抽屉/三栏关系已核对；保留仓库原 Focus 图标。已核对实际 quiet/details/Observation/Progress/facts/Context、任务材料/终态/独立会话，以及受控插件/记忆/全图/设置/窄屏/缩放图。900px/390px 的独立会话关闭检查器后占满主内容，不预留空任务列；紧凑导航的品牌不越栏覆盖页面标题。空 Progress 不声称有独立证据；实际阶段与输入回执分开。

| 代码类别 | 新增行 | 删除行 | 净增行 |
| --- | ---: | ---: | ---: |
| 业务代码（含样式与必要文件头） | 1313 | 331 | 982 |
| 测试代码（含独立 fixture/bridge/真实 UI） | 929 | 39 | 890 |

统计为本次修改的 desktop/backend 文件 Git numstat 加 11 个未跟踪新文件；排除规划/验收文档、截图、缓存和用户原有文档修改。新增业务文件合计 278 行：Observation read 152 行、共用 Context inspector 81 行、Observation view 45 行。其余增长来自原控制器的按需加载/失败恢复/授权衔接、任务三栏与真实回执、现有图交互及样式；原有同职责实现已替换，未新增调度或领域状态。相对初次交付统计，修复后的业务净量增加 29 行、测试净量增加 120 行，主要用于类型安全、完整性提示和缺失的回归；本轮没有新增模块或依赖。

测试增长用于以前没有公开接口的冻结分页与隐私、两条真实浏览器业务链、受控乱序/断线/失败及旧入口迁移。没有删除必要断言来减少行数；旧导航视觉断言改为六导航，Context 业务断言保持。没有全量测试；没有修改构建型编辑器源，未重建既有 CodeMirror bundle。

## 最终复核与复现入口

2026-10-10 核验修复复核：C1/W1—W5 全部关闭，证据见 OpenSpec verification.md 与 verification-evidence.json。对应 M05 的指定请求可见身份/晚到响应、M09 的数值隐私/冻结覆盖、M10 的精确 Revision 对账、M32/M34 的全图装备/选择均重新验收。原五个旧 Node 断言按批准的语义与布局迁移，实质业务断言保留。

最新针对性 Node 批次为 14 个文件、90 passed，包含隐藏 Electron 的选择/Space/Enter/focus、真实 DOM 边对账、多请求/迟到回执、available-incomplete 且页已读完。任务 Loop Electron 长流程另行通过。后端最终顺序批次为三个真实查询/UI 用例 3 passed（37.53s），验证合法来源合同下对象/数组/异常状态/字符串不能带出私有指标，真实 Patrol/传统任务/磁盘材料/Main/SSE/持久结果保持可用。并行复测曾有传统任务 UI 首次启动超时，单独重跑 1 passed（21.50s），之后上述完整顺序批次通过；偶发启动超时根因未确定，记录保留，不称所有运行均无失败。

最新 Patrol 截图位于 .tmp-focus-repair-final-20261010/test_real_patrol_frontend_inta0/evidence；传统任务截图仍输出到 .tmp-focus-frontend-evidence；受控六页/窄屏/缩放截图在 Temp/focus-refactor-qa。已人工核对新增 Observation 覆盖提示与原卡片/分页、全图操作工具栏；外部 Provider/安装版/人工系统对话框等未实测范围仍按上节保留。

初次实施的传统任务真实 UI 单文件复测：1 passed（17.24s）。与 ownership 合并的独立批次：12 passed / 1 deselected。多次复测与交叉批次不累加成独立覆盖数量。

初次实施的 Node 批次分别为 composer/access/material/UI/security 的 13 passed，以及 i18n/material-grouping/curator/successor/authorization 的 11 passed。修改或新增的 JavaScript/CJS 文件 node --check、git diff --check 和 OpenSpec strict 通过。两个历史降级 fixture 的失败按上节保留。

在仓库根目录使用 Python 3.12、已有 Node/Electron 与测试 PostgreSQL；PYTHONPATH 指向 backend/packages/harness，pytest 禁用 cacheprovider 并指定新的隔离 basetemp。核心入口：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend/packages/harness'
$env:PYTHONIOENCODING = 'utf-8'
python -m pytest backend/tests/test_observation_inspection.py backend/tests/test_frontend_migration_real.py backend/tests/test_frontend_task_real.py -q -p no:cacheprovider --basetemp .tmp-focus-frontend-review
node --test desktop/composer-draft.test.cjs desktop/access-approval.test.cjs desktop/run-material-ui.test.cjs desktop/workspace-patrol-page.test.cjs desktop/f20-architecture-guard.test.cjs
node --test desktop/i18n.test.cjs desktop/material-grouping.test.cjs desktop/context-curator-presentation.test.cjs desktop/loop-successor-view.test.cjs desktop/loop-authorization-error.test.cjs
openspec validate refactor-focus-patrol-first-frontend --strict
```

实际浏览器截图在 C:/Users/brubing/Desktop/ag-project/focus/.tmp-focus-frontend-evidence：real-patrol-quiet、real-patrol-details、real-observation、real-progress、real-facts、real-fact-detail、real-context、real-task-material、real-task-complete、real-standalone、real-standalone-900、real-standalone-390。对应 real-patrol-result.json 与 real-task-result.json 保存本次测试业务身份。它们均为隔离验收数据；较早的 real-task-failure.png 是调试保留，不列为通过证据。

## 兼容范围

| 保留路径 | 原因与退出条件 |
| --- | --- |
| task-based Loop / workspace Patrol | 两种合法 owner 与交互身份，各在任务工具和 Patrol 输入；不作为重复机制清理。若未来取消一种身份，需独立业务审批与数据迁移 |
| legacy Live flag / 旧历史读取 | 部署开关及存量消费者仍存在；直到部署配置和历史数据确认不再使用前保留，本次不扩大兼容范围 |
| 树/卡片/Context 编辑/Session Patrol/代理/材料高级页 | 原图没有画出的真实能力仍需承接，通过明确次级入口复用原模块；不能以本次导航收敛为删除条件 |
