# 实施与验收记录（2026-10-10）

用户本轮明确调用 `openspec-apply-change`，作为本 change 的 apply 批准，并随后要求提交推送远程 main。实现、规划和最终证据纳入该提交；未部署、未更新安装版或归档。本记录不把全部验收标记为通过。

## 基线、范围和证据来源

- 基线为 `main` / `628c42d9993700b34ed08c6804bd4b6e3e7ebfa5`；开始 apply 时 remote main 相同。原有 `docs/synthesis-support-repair.md` 的 12 行修改保留，用户临时目录、bundle 和 staging 未清理。
- 读取范围和 Apple 官方／Skill 作者／实现选择的区分见 design D1–D8。实际 Context7 查询沿用规划中的 Electron、MDN，并在 apply 中再次查询 Animation 生命周期、capturePage 和 beginFrameSubscription。返回的 Electron 文档含 46.0 行为变化，测试实际为 43.2.0，没有把未来版本的图片尺寸规则套到当前版本。
- 只新增一个产品模块 `desktop/surface-transition.js`；无依赖、构建、后端、Agent 模型和数据合同改动。
- 前置源码用 `git archive 628c42d desktop` 写入 TEMP 隔离副本，未切换或重置当前 checkout。运行使用独立 Electron userData、临时目录和测试 PostgreSQL。
- 硬件路径：Windows 11 / i9-13900HX / 16GB，电源方案“平衡”；Electron 43.2.0 / Chromium 150.0.7871.129。`getGPUInfo('complete')` 确认 ANGLE / Intel UHD / D3D11。安装版 Focus 仍独立运行，未操作其业务数据。
- 对照窗口外尺寸 1280×900，DPR 1.5，before/after quiet PNG 均为 **1899×1256 像素**。报告末尾 viewport 是窄窗/zoom 测试后的值，不冒充初始截图尺寸。

## 已落地的职责调整

| 能力 | 承接与接入 | 验证／限制 |
|---|---|---|
| 六导航、设置、工作区选择（M01–M03） | 原 shell、picker 和原 actions；单层控制材质 | 六入口、取消、拒绝、迟到选择、设置、键盘与窄窗测试；未增加导航或设置能力 |
| 四类输入、回答、回执、历史（M04–M06） | 原 inputs store、intake 和同一 composer；历史保留 native dialog | A/B 回执不清 C 草稿；同 submission 重试；accepted 与已提交状态分开；发送不展开 |
| Live、进度、冻结、事实（M07–M12） | 原单路 Store/Connection/API；Task Progress / Observation / LoopFact 保持归属 | HTTP/SSE/冻结版本/暂停用例通过；事实未知数量仍不计成功 |
| Context 检查／传统任务打开（M13–M15） | 原 inspector 三页签与显式 onOpenTask | A→B 乱序、关闭中重开、合法焦点返回；检查不改变发送 workspace |
| 任务三栏、材料、Run、所有高级能力（M16–M45） | 原 app / TaskRunOperations / conversation reconciler / bindings；只改控制面与临时表面呈现 | 真实任务／磁盘材料／Main 与独立 Run 用例通过；纯数据与架构守卫通过；未逐一调用外部 Provider、所有插件或高级压缩执行链路，未验证部分不冒充完成 |
| 图与事实稳定更新 | 原 PortfolioMapView / LoopFactsView 的 reconcile | 未变图／事实零 DOM 写入；不反复 append 已存在节点，不搬移整张事实表；真实变化保留身份、焦点、scroll 与 zoom |
| 等待表单 | 原 patchRequests、waitUi 与 draft store | 同 revision 与 UI 呈现不变时保留原表单；Live 更新保留 textarea 节点、焦点和选区 |

没有拟废弃业务能力。保留公开插件 tokens、旧宿主 aliases、legacy Live、原 owner／权限／冻结／生命周期兼容路径。删除的是被替换的 dialog/backdrop/inspector 入场 keyframes、机械 button scale、重复 observation-identities 声明和头像装饰循环；调用与入口仍保留。

## 四个体验维度

| 维度 | 实现与实际观察 | 验收状态 |
|---|---|---|
| Fluid | 同一 form/textarea 在 quiet/details 之间改变位置与尺寸；详情退出立即 inert，完成后 hidden；drawer 从右侧可逆退出；native dialog 保留 top layer 至退出完成；resize／减少动画收敛到最后意图 | Node 原生 DOM、40/80/120ms 反向、对象乱序、Esc、草稿／选区与连续帧通过；快速反向的可重复原生输入主要采用 Space，原先“移动中的屏幕坐标鼠标点按”曾点入 textarea，失败记录保留，不伪称全范围 pointer 反向均已验证 |
| Fast | 复用布局缓存与增量对账，去除未变属性、节点、事实行及等待表单的重写；本地反馈不等网络，控制请求的 pending/error 不改业务状态 | 本地反馈与已测长任务通过；**完整 D8 未通过／未验证**，见下表 |
| Alive | 统一 hover/press/focus/selected/disabled/pending；控制材质局部光边与按压深度；真实状态文本持续可读；静止无新增循环 | 请求延迟／失败、状态不提前暂停、减少动画等测试通过；头像保持原关闭能力，移除浮动／呼吸循环 |
| Liquid Glass | 控件自身半透填充、背景滤镜、顺形上下明暗边、内外厚度、焦点光边；内容和事实表实色；菜单打开时关闭其输入父层滤镜以避免叠加 | 已查看实际 quiet/details/Context、opaque/contrast 页面；这是 Web 控制材质实现，不宣称原生 Liquid Glass 的折射、光学或系统窗口材质等价。严格视觉品质仍需用户审阅，CSS 属性存在不单独作为通过依据 |

文字角色收敛到 design D4，secondary `#4d596b`、accent `#2468d7`；图 metadata 从 9px 收敛至 12px。84%白色填充的保守黑底合成为约 `#d6d6d6`，secondary 对比约4.89:1；白字／accent约5.2:1。边缘为装饰，焦点／状态保留单独语义。透明偏好、forced-colors、reduced-motion 用 Chromium 媒体模拟实际操作；Windows 原生偏好映射与真实中文 IME 候选窗操作未验证。

## 性能记录（不降低已批准标准）

原始数据为 `evidence/final-reference/report.json`、`evidence/final-result/report.json` 和各自 renderer-trace.zip（CRC校验后压缩，内含原始renderer-trace.json）；分析为 `evidence/performance-final.json`。输入通过 `webContents.sendInputEvent` 注入可信原生事件，不把工具调用／rAF 或 CSS 时长当作呈现延迟。

远程提交包含规划阶段截图、最终 before/after 截图与连续帧回放、最终呈现帧、页面截图、JSON 测量报告及已有失败记录。大型 `renderer-trace.zip` 和中间轮次截图保留在本地 evidence 目录，未纳入 Git；报告中的绝对路径为采集时的本地路径，远程读取报告时应使用同目录的相对文件名。原始 trace 未上传，不把远程的摘要报告称为原始 trace。

| 场景 | before | after | 结论 |
|---|---:|---:|---|
| 展开／收回，3组各30样本，5次预热；trace EventLatency MOUSE_RELEASED p95 | 17.512 / 17.723 / 17.592ms | 17.659 / 17.560 / 17.762ms | 此路径满足≤50ms且增长≤10ms；未覆盖所有菜单、输入选择等操作族的30×3样本 |
| 32 Context / 64 edges 连续更新长任务 | 0 | 0 | 已测样本无新增长任务 |
| 128 Context / 256 edges / 200事实 连续更新长任务 | 12次，643ms，总体最大60ms | 0次，0ms | 消除本次测到的布局回归；没有新增>100ms长任务 |
| 128更新，Style / Layout / Paint inclusive时长 | 1034.816 / 1384.781 / 499.762ms | 320.767 / 115.272 / 204.270ms | 来源为 trace 的独立阶段；包含嵌套活动，不相加冒充CPU墙钟总时长 |
| 1000消息、80条实际挂载，约20s滚动与草稿 | 无长任务 | 无长任务 | 保留原有界渲染；未声明绝对帧率 |
| 静止10s、50次开关、再静止10s | 1730→1730节点，0动画 | 1731→1731节点，0动画，details hidden/inert | 终态收口；未测完整 listener/heap 泄漏曲线，不用节点数替代该曲线 |
| 32/128两秒 rAF间隔p95 | 8.5ms | 8.5ms | **仅proxy**，不据此宣布视觉流畅度或GPU帧率达标 |
| 100事件目标50ms间隔 | 实际5.945 / 5.874s | 实际5.736 / 5.709s | 名义20Hz；未完全覆盖严格100/5s压力条件，保留未验收状态 |
| Live更新至原生呈现像素变化，100样本、5预热 | 未用此方法建立同组前置值 | p95 **121.806ms**，max127.459ms | 单一待更新标题像素SHA256和实际帧；含IPC与frame readback，回调交付不是纯首绘时间，不能确认≤100ms。为保持关联，采样降速84个tick，不能替代上面的压力场景 |

先前 `verified-after` 的128更新34次／2333ms及 `accepted-after` 22次／1260ms均未通过，已保留。原因是未变图节点重排、事实行全部移出tbody再插入；修正在原对账职责中完成，未靠减数据或放宽阈值宣布通过。最终性能总体状态仍为 **部分通过，D8完整验收未完成**。

## 测试与实际查看

- 10个受影响 Node 文件：44项通过。额外原生用例验证无变化图／事实零DOM写入、焦点／zoom／scroll、wait textarea身份与选区、rapid reverse、A/B乱序、关闭中重开、控制失败不提前生效、六导航和设置。JS语法、diff check、OpenSpec严格校验均执行。
- 真实UI：`test_frontend_migration_real.py` 与 `test_frontend_task_real.py` 最终2项通过（29.12s）；前一轮含 `test_loop_live_acceptance.py` 3项通过（33.93s）。使用独立PostgreSQL；传统Run模型采样受控，保留实际运行图、HTTP/SSE、checkpoint、磁盘材料与持久Run。未操作外部Provider，不跑全量后端测试。
- 既有 `access-mode.test.cjs` VM DOM stub 与 `app-map-view.test.cjs` 空态预期，在原commit隔离副本也同样失败；未为本次修改业务行为以迁就这些旧断言。
- `evidence/review.html` 仅回放真实截图，不是新产品原型；按原capture时间回放，未插帧。已实际查看 before/after quiet、details、Context、expand/drawer/close/collapse 中间帧、降级与窄窗，以及 native presentation 6/55/105帧，确认对应的实际标题。其余能力截图在 `pages-after`；它们来自离线fixture，仅证明布局。
- 前期pytest第一次未设置 harness PYTHONPATH，收集失败；修正环境后运行通过。移动目标鼠标反向的选择变化和Live订阅准备失败均保留在对应failure文件，不能当作通过样本。

## 工程规模与交付边界

精确文件统计见 `evidence/code-statistics.json`。产品新增352、删除180、净增172行；测试新增286、删除5、净增281行。统计按git正常行尾处理，包含新文件，排除规划、截图、trace、第三方、生成文件、基线副本和用户既有修改。增长主要是72行的窄职责surface helper、取消/焦点/稳定DOM衔接，以及原生窗口、trace和呈现帧验证。未通过压缩格式或删除必要测试降低行数。

未完成／未验证：D8完整性能门槛、所有交互族的采样、严格100/5s、纯Live首绘≤100ms、完整listeners/heap曲线、真实Windows偏好与IME、所有外部Provider／插件／高级执行回归、用户最终材质视觉验收。tasks保留对应未勾选项；不归档、不把本记录视为全部验收完成。
