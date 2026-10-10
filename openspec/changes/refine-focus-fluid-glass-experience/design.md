## Context

动机见 proposal。此文件是待审阅设计，**尚未批准，产品代码尚未修改**。采用现有 `spec-driven` schema，保留 proposal／design／specs／tasks 结构；证据仅附在本 change 的 evidence 目录，不另建规划体系。

### 源码与工作区基线

| 项 | 实际结果 |
| --- | --- |
| 本地分支／HEAD | `main` / `628c42d9993700b34ed08c6804bd4b6e3e7ebfa5` |
| 远程核对 | `git ls-remote origin HEAD refs/heads/main` 返回同一 SHA；未 fetch、pull、切换或 reset |
| 既有未提交内容 | `docs/synthesis-support-repair.md` 有既有修改；backup bundle、staging 和多组临时证据目录未跟踪；均不作为此次清理对象 |
| 规格环境 | `openspec/config.yaml` 为 `schema: spec-driven`；主规格四项，均为图片／多模态；旧前端 change 仍未归档 |
| 实际前端 | Electron **43.2.0**、Chromium **150.0.7871.129**、内嵌 Node **24.18.0**；终端 Node **24.14.0**、Python **3.12.10**；原生 DOM／CSS、已有 CodeMirror 6 |
| 运行环境 | Windows 11 家庭中文版 `10.0.26200`；i9-13900HX，32 logical processors；可见内存 16,463,352 KiB；Intel UHD 与 RTX 4060 Laptop GPU，实际渲染器选择须由 trace 核实 |
| 安全边界 | 唯一 Gateway、同源 API/SSE、session／CSP／preload 白名单；不修改权限、后端模型或安装版 |

本轮阅读：`desktop/package.json` 与已安装 Electron package；`index.html` 的 CSS/脚本装配；`styles.css` 和 `styles/{tokens,base,components,shell,workspace-patrol,views,patrol-avatar}.css`；`workspace-patrol-{view,controller,inputs,picker}.js`；`context-inspector.js`；`portfolio-map-view.js`；`app.js` 的 render、Patrol 挂载、shell、全图检查与已有局部动画调用；`conversation-reconciler.js`；`loop-live-{store,reducer,connection}.js`（前一轮已读，本轮核对调用）；旧前端迁移 design／迁移表／acceptance；真实前端测试、preload、隔离 fixture 与 server。没有逐一执行所有高级能力，没有把文件打包等同于全部逐行阅读。

### 资料工具与来源分层

2026-10-10 实际完成的查询：

| 来源类别 | 实际读取／调用 | 本次使用的结论 |
| --- | --- | --- |
| 主 Skill | `C:/Users/brubing/.agents/skills/apple-design/SKILL.md`，Liquid Glass 与 Design improvement mode | 先审现状、明确产品辨识点、紧凑 token、按关键流改造和验证 |
| Skill 作者的跨平台建议 | `references/cross-platform.md`；`references/hig/liquid-glass.md` 的 curated guide／Cross-platform translation | Web 材质是近似；建议的 blur／alpha／saturation 不是 Apple 数值标准；系统窗口材质不能证明 Web 控件材质 |
| Apple 原文的本地参考 | `hig/{materials,motion,layout,accessibility,typography,color,designing-for-macos,sidebars,text-fields,popovers,focus-and-selection}.md` 的相关章节 | 控制／内容分层、少量材质、可取消反馈、渐进披露、焦点与可读性。按路由逐批读取，未加载整个目录 |
| Apple 官方在线 | [WWDC25/219 Meet Liquid Glass](https://developer.apple.com/videos/play/wwdc2025/219/) 的 transcript／chapters；打开 [Materials](https://developer.apple.com/design/human-interface-guidelines/materials)、[Motion](https://developer.apple.com/design/human-interface-guidelines/motion)；已打开 [Skill 仓库](https://github.com/dickwu/apple-design-skill) 核对来源 | Dynamics 1:29、Adaptivity 6:00、Principles 10:31：材质外观与反馈共同设计，控件之间有空间连续性，避免 glass-on-glass。**阅读了官方转录，没有声称完整观看视频** |
| Context7 查询 1 | resolve `Electron` → `/electron/electron`；query renderer responsiveness、contentTracing／debugger、同步 renderer 工作及 GPU 测量限制；成功 | 可用 [contentTracing](https://github.com/electron/electron/blob/main/docs/api/content-tracing.md)、[debugger](https://github.com/electron/electron/blob/main/docs/api/debugger.md) 测量；[performance](https://github.com/electron/electron/blob/main/docs/tutorial/performance.md) 提醒避免阻塞 UI 的同步 IPC；软件 offscreen 截图不能替代硬件性能证明 |
| Context7 查询 2 | resolve `MDN Web Docs` → `/mdn/content`；query translucent backdrop-filter、backdrop root、嵌套滤镜与降级；成功 | [backdrop-filter](https://github.com/mdn/content/blob/main/files/en-us/web/css/reference/properties/backdrop-filter/index.md) 需要透光填充，祖先 opacity／will-change 等可能改变 backdrop root；不可全局永久 will-change |
| Context7 查询 3 | `/mdn/content`；query interruptible Animation lifecycle、finished／commitStyles、dialog close 与 reduced motion；成功 | [finished](https://github.com/mdn/content/blob/main/files/en-us/web/api/animation/finished/index.md) 完成 promise 会随生命周期更新；取消不能运行旧终态回调；[requestClose](https://github.com/mdn/content/blob/main/files/en-us/web/api/htmldialogelement/requestclose/index.md) 有 cancel 语义；保留 native dialog 的焦点和模态边界 |

两次 resolve、三次 query 均真实返回资料；并非用搜索或记忆冒充 Context7。第一次产品修改前重新核对实际运行版本与这些资料适用性；如必要查询不可完成，按用户要求停止。查询返回不覆盖本次所有浏览器行为，仍须在 Electron 43.2.0 实测。

Apple 官方原则只用于材料、层级、反馈和连续性。Skill 的“每个桌面命令进入 macOS menu bar”“暗色模式”“跨平台优先 native vibrancy”等建议不扩大本任务：保留 Windows 窗口、原快捷键和设置入口，不新增暗色／移动模式／菜单系统，也不启用 Mica/Acrylic 来替代控件证明。CSS 颜色、时长、边缘绘制及 helper 是下面明确标注的 **Focus 实现选择**。

### 已运行／已查看的现状证据

1. 运行现有 `backend/tests/test_frontend_migration_real.py`，**1 passed / 14.31s**。fixture 创建、迁移、销毁随机独立 PostgreSQL；真实 workspace intake／Live/SSE／冻结查询／pause 路由，未调用外部 Provider、未启动真实 Agent Run、未接用户业务库。非关键 shell 接口用现有 fixture。
2. 使用 Temp 中的只读诊断 harness 扩展同一测试流，**1 passed / 16.13s**。产品源码未改；新增采样逻辑只在临时 harness 中。20 次实际 DOM click 切换，保存 geometry／draft／selection／样式及下一 rAF 延迟；不把这些 click 当作真实鼠标硬件延迟。
3. 已逐张查看 [安静输入](evidence/real-patrol-quiet.png)、[展开详情](evidence/real-patrol-details.png)、[Context 会话检查](evidence/real-context.png) 和探针的 [有草稿安静态](evidence/probe-quiet.png)、[收回终态](evidence/probe-collapsed.png)。其余冻结／事实截图留档，未以留档声称逐张视觉复核；这些终态图不冒充连续过渡证据。
4. 诊断 [baseline-metrics.json](evidence/baseline-metrics.json)：CSS viewport **1600×913**、DPR 1、reduced motion=false；软件 offscreen、hardwareAccelerationEnabled=false。20 次操作→下一 rAF：中位 16.50ms，nearest-rank P95 17.60ms；61 个空闲 rAF 间隔，中位 16.70ms、P95 16.80ms；采样窗口无 longtask。样本小且软件绘制，**不是 actual presentation latency、GPU frame rate 或 Fast 通过证明**。
5. 20 次切换保留同一 form 和 `基线草稿：未发送`，选区 `[2,5]`；详情与安静态的几何分别直接落在 `(234,735.95,1326,177.05)` 与 `(517,339.97,770,241.05)`，同步与下一帧没有中间状态，animations=0。click handler 明确把焦点给触发按钮，不能把探针中的 focus=false 误报为无故丢焦点。
6. 取样 computed style：composer、Context drawer 为白色且 backdrop-filter=none；navigation 为 gradient 背景、backdrop-filter=none。源码只有 Patrol dialog 的 **backdrop** blur(4px)，dialog 本身实色且只有入场 keyframes。
7. 当前十几毫秒的软件小样本不能说明大数据／真实 GPU 路径已流畅。滚动、事件突发、物理输入、GPU trace、真实录屏、Narrator、系统透明偏好映射均尚未测量。旧原型 ZIP／设计图片本轮未取得，未声称对齐。

## Goals / Non-Goals

**Goals:**

- 产品辨识点是“同一个可持续表达的输入面，从安静入口延展为可检查的工作局面”。玻璃和动效围绕这个关系，正文安静而稳定。
- 在现有职责上修正显示层生命周期，保持草稿、IME、焦点意图、选区、滚动、revision／request identity 和所有已存在能力。
- 以可观察行为、真实截图／连续帧、可复现性能记录分别证明 Fluid／Fast／Alive／Liquid Glass；先验证核心闭环再推广。

**Non-Goals:**

- 不改 Agent 架构、Loop 控制、数据库、权限、接口、SSE 合同；不合并执行历史，不新增状态推断或模拟进度。
- 不添加主题系统、暗色模式、移动产品、macOS 仿制窗口、字体依赖、动画库、图引擎、Canvas/WebGL 光学引擎或逐帧背景采样服务。
- 不把没有记录的测量写成已通过，不把“看起来接近 Apple”当原生等价证明，不部署用户安装版。

## Decisions

### D1. 四项体验的具体差距与落点

| 目标 | 已确认当前表现／差距 | 预期行为 | 责任位置／证据 |
| --- | --- | --- | --- |
| Fluid | toggleDetails 改 class 与 hidden；quiet 改 flex 对齐、宽度、padding 和 textarea 高度，跳变无过渡；drawer.close 立即 hidden；dialog 仅统一上浮入场 | 同一输入面移动、调整形状并展出内容；退出沿来源回收；反向从当前视觉位置继续，最终只保留最后意图 | controller 的 toggle/mount/leave、inspector 的 select/close/dispose、picker、共用 visibility helper；闭环连续帧和快速反向用例 |
| Fast | 已有按需读取、请求 generation、稳定 DOM、图 layout cache；软件小样本响应快，但 GPU／大数据未测 | pointer／键盘触发本地反馈先出现，不因动画或网络锁住输入；实际提交仍表达 pending／accepted／failed | 原 inputs/API/Live 保留；硬件 trace、事件→呈现延迟、longtasks、布局／绘制记录 |
| Alive | 所有 button 全局 scale(.985)；导航、输入、浮层没有材料共同反馈；部分旧 avatar/trace 有循环效果 | hover／focus／press／selection 采用同一层次与光感；只在交互或真实状态变化时响应，静态停止；正文不重复入场 | components/token、shell、composer 与浮层控制；交互前后帧、静止窗口无新效果循环 |
| Liquid Glass | 内容卡片与输入框都是白色矩形，边框／阴影主导；玻璃仅在遮罩处模糊 | 控制自身透光，形状有边缘明暗／内外厚度／局部高光；展开、按压改变材料反馈；内容容器保持清楚 | 原位 components 的表面 primitive；composer/shell/toolbars/dialog shell 应用；真实背景滚动与反向转换截图／帧 |

### D2. 材质分层及可实现边界

**Focus 的实现选择**：一个 regular-like 控制材质加实色降级，优先原生 CSS backdrop、渐变／内阴影／边框和短促 native 动画，不引入滤镜引擎。

| 表面 | 处理 | 限制 |
| --- | --- | --- |
| 导航主控制面 | 单一略透光的窄导航面，保留 drag/resizer 区域；选中项是此面上的薄填充／边缘反馈 | nav 子按钮不再加第二层 backdrop；背景只能是原中性工作面，不制造装饰彩色背景 |
| Patrol composer | 唯一持续输入的玻璃外壳，文字和 caret 为清晰 foreground；quiet 时靠边缘厚度与光感区分，details 时参与真实下方内容 | 不对 textarea 字体／caret 做缩放，不复制输入节点；不把玻璃 blur 数值当验收 |
| 图缩放工具条、任务浮动操作条 | 局部薄功能层；真实滚动边缘有小范围遮蔽／过渡区保证标签读得清 | 不覆盖整个画布滤镜；滚动边缘装饰 pointer-events:none，不能挡内容点击或造成文字缺失 |
| Context 抽屉 | 外侧边缘、header／tabs 控制区表达玻璃，正文区域为清晰实色／高密度表面 | 主体会话和来源不透杂乱图文；不能整张玻璃再嵌 header 玻璃 |
| menu／dialog | 浮层外壳与边缘有自身透光和高光；正文用足够遮蔽填充。遮罩只表达模态焦点，减少或取消 backdrop blur | 保留 native dialog top layer、关闭和焦点；非模态 Context 不增加全屏模态遮罩 |
| Graph nodes、会话、Progress、Observation、LoopFact、材料／插件／记忆正文 | 内容层，实色、分隔、字体层级；允许静态低阴影 | 无 backdrop、无每次事件入场、无玻璃嵌玻璃 |

控制自身至少协调四种视觉成分：背景透光与适度散射；顺形的上侧亮缘／下侧暗缘与内外分离；局部非均匀高光而非均匀白描边；随 focus／press／展开变化的厚度和光感。高光绘在壳层伪元素并退出命中，不另建跟踪光源的常驻 loop。长表面高光保持克制，小按钮的按压不推动正文。

blur、fill 的起点可在 **12–24px、白色 84–92%** 中校准；不是规格定值。标签区域的有效背景须满足 D3 下限，边缘可更透光。不得通过把整体 alpha 降低到难读来“证明玻璃”，也不得为 glass reveal 添测试外的彩色背景。详情态使用真实图／事实／文字背景验证；quiet 本身就是中性空白，不能要求其透出不存在的内容。

CSS 可以实现透光散射、分层、边缘厚度线索、阴影和交互形变；本方案的非均匀边缘是 **光学近似**，不实现 Apple 原生的实时折射／lensing／背景亮度自动映射。Mica/Acrylic/NSVisualEffectView 是窗口材质，不是这项控件能力。验收至少应看到控制自身的边缘、厚度、背景参与和交互响应共同工作；如果结果仍只是磨砂卡片，核心闭环不得通过，也不得以跨平台限制宣布完成。需要额外 SVG displacement／WebGL／依赖来改变已批边界时，返回 artifacts 再审批。

### D3. 紧凑 token 和可读性

从当前语义 token 修改，不在 styles.css 尾部另覆写一套。基础六角色色为 canvas `#f6f7f9`、surface `#ffffff`、primary text `#202631`、secondary text **`#4d596b`**、accent **`#2468d7`**、separator `#c9d1dc`；现有成功／警告／错误角色保留含义，不再增加任意彩色层。继续兼容公开 `--focus-plugin-*` 和 `--bg/--panel/...` 变量。

以下为 sRGB 相对亮度公式的离线计算，不是截屏估算：

| 组合 | 当前／拟定对比度 | 处理 |
| --- | --- | --- |
| 当前 secondary #718096 / white | 4.02:1 | 小文字改为更深 secondary，并核对各调用方 |
| 当前 placeholder #91a1b9 / white | 2.62:1 | 输入用途提示改用可读的 secondary，不能依赖放大字体绕过 |
| 当前 white / accent #377cec | 3.99:1 | 主操作填充改 #2468d7，white 对比度 5.20:1 |
| primary #202631 / canvas #f6f7f9 | 14.17:1 | 保留 |
| 拟 secondary #4d596b / 最暗标签区域 #d6d6d6 | 4.89:1 | 壳层文字区域通过足够填充保障这个下限；边缘可更透明 |
| accent #2468d7 / #edf0f4 | 4.55:1 | 链接／状态需在实际合成背景上复检 |

最终仍用真实合成像素、滚动背景和 forced-colors 核验普通文字≥4.5:1、关键非文字边界≥3:1；对比值是本项目验收选择，非 Apple 为 Electron 提供的硬性数字。文字保持现有 Segoe UI／Microsoft YaHei UI／system-ui，body 14、控件 13–14、必要 meta 至少12，quiet 输入20、details 输入14–16、标题20–28；保留缩放，不引入 SF Pro 或品牌展示字体。

保留已有 spacing、radius 和 motion token，补少量材料角色变量（fill／edge／highlight／shadow／backdrop／solid），按几何间距做同心嵌套。原 motion 80／120／180／240ms 只作起点，最终以响应和实际连续帧确认。高光、blur 和 opacity 不每帧从 JS 修改；不要在玻璃祖先挂永久 will-change:opacity 或无必要的 filter。正常／hover／focus-visible／pressed／selected／disabled／pending 都有相同材料语言，focus outline 不用透明发光替代。

### D4. 保留节点的连续展开与回收

`detailsOpen` 仍是 controller 唯一显示意图，输入 store 和业务 projection 不变。Form 与 textarea 在 mount 后持续存在；点击展开先更新显示意图和 aria 状态、启动本地反馈，网络读取单独进行。

展开前读取一次 composer／内容区域几何和阅读状态；目标布局仍是左图、右 Progress/Observation、下方事实和可用输入。采用一段可中断的 native 过渡连接 composer 的位置及外壳形状，详情沿输入面展开，正文以轻度显现连接空间关系；不能给每张卡分别安排登场。优先平移／opacity 与独立壳层形状，**不缩放可编辑文字**。确需单个 composer 尺寸插值时限定在这个节点、无逐帧 JS 布局读取，并用 trace 判断其成本，不能扩成整页 height 动画。

收回先将退出详情设为 inert，若焦点位于将消失的区域则在输入区的详情控制上安置焦点；保留内容 DOM、图缩放和滚动直到退场完成，最后 hidden。点击按钮造成的合法焦点转移保留；没有用户动作时动画／Live 不夺走输入焦点。Composer 选区、IME 和 textarea 内部滚动不因几何变化重置。

快速反向时以当前视觉位置／进度重定目标，取消旧动画且使旧 completion 失效；不重新从静态起点播放，不排队。缩回途中再展开立即解除相应 inert，只有最新退出完成能 hidden；页面离开／resize／减少动画偏好改变时，取消并同步到最新合法静态布局，不留下 fixed overlay、transform 或 pointer blocker。布局尺寸在切换、真正 resize 和打断点读取，不在每帧循环读写。

### D5. 抽屉、menu 与 dialog 保留原交互语义

| 入口 | 连续性和最终状态 | 数据／焦点边界 |
| --- | --- | --- |
| Context 节点 → 非模态抽屉 | 从右侧检查平面进入，边缘材质先形成、正文清晰跟随；退出沿同一路径。检查 A→B 时保留壳层，只更新标题／加载／正文，不让抽屉再次离场入场 | inspector 保留 active identity／generation／abort；B 立即可见加载提示，A 迟到不写 B；`read(older)` 恢复阅读锚点，不能把换页动效当新对象 |
| 抽屉关闭 | 关闭 button／Esc 使用同一路径；退出即停止接受新读回调，完成后 hidden/inert；再次打开从当前帧接续 | 焦点回到仍连接的触发节点；触发节点已消失则到本页明确 fallback。非模态不全局 trap focus，Patrol 输入持续可用 |
| 输入记录、待处理、Observation、Progress／事实检查 | 单个现有 dialog；来源按钮的局部反馈立即出现，外壳从锚点附近建立关系，不把大文本放大缩小；更换检查种类保留壳层，不重复通用上浮 | showDialog 记录当前 trigger；close button／cancel(Esc)／程序关闭统一；movedPanel 恢复原位置，不复制带 draft 的表单 |
| 工作控制菜单 | 从当前 summary 位置展开，关闭回到该位置，保留 details／键盘语义与现有动作 | 不把点击空白、关闭或动画取消解释为授权撤销／停止任务 |
| 文件夹确认 | 仅对自有 picker dialog 应用材质和过渡，系统 chooser 保留 Windows 行为 | chooseFolder 返回之前不创建绑定；确认成功仍由原 onEnter；取消不丢旧路径 |
| 传统任务检查器／已有通用 dialogs | 共用表面和可见性原语；保留现有列宽、焦点返回、确认和按需读 | 不把任务检查器改成 Patrol 目标，不修改主会话草稿或运行身份 |

native dialog 在退出期间保留真实 top-layer 模态性，退出完才 close；减少动画时立即 close。退出过程中若用户把焦点移到别的合法控件，旧回调不得再强行 focus。不能把 dialog 移到普通 div 来方便动画，也不能取消 native select 的 Windows 键盘行为。独立 content 的检查来源不进入 transition helper。

### D6. 最小工程边界与替代清单

已检索 `.animate/getAnimations/commitStyles/startViewTransition/transitionend/animationend`：现有动画主要是 context 编辑行重排／删除和 drag preview，没有可复用的 surface 可见性生命周期。它们不是“能力缺失”的业务问题，而是显示层衔接不足。

| 类型 | 范围 | 理由／退出条件 |
| --- | --- | --- |
| 复用 | inputs store、API、Live store/reducer/connection、generation／AbortController、PortfolioMapView layout/reconcile、conversation 对账、原 native dialog/details、系统文件夹桥 | 当前业务和身份机制足够，不新建控制器或状态源 |
| 修改 | token/base/components；shell、workspace-patrol 和涉及的 views 原规则；controller toggle/showDialog/mount/leave；inspector select/render/read/close/dispose；picker dialog；必要 app 挂载／任务检查器衔接 | 原职责内修正材料、退出语义和节点保留。主体 click 业务分支不整体改写 |
| 新增 | **最多一个** `desktop/surface-transition.js`，提供 element-scoped visibility transition／cancel-cleanup | 多入口共享 hidden/inert、取消 generation、finish cleanup，CSS 入场无法完成这些约束；纯 visibility helper 不持有 Loop、Context、draft、网络或权限。仅内部 WeakMap 记录当前动画／settlement，选取曲线／方向由调用者传入，不成为通用动画引擎 |
| 装配新增 | `index.html` 增加这一个脚本的明确顺序 | 不新增依赖、bundler 或 build script；CSS primitive 置于已有 components，不另起第二套 CSS 文件 |
| 替代／删除 | 覆盖本次 surface 的 dialog-enter／backdrop-enter、inspector-enter 及直接 hidden/close 的重复显示实现；相关 legacy styles.css 中已被新职责替代的冲突规则；全局 button scale 改为适用控件反馈 | 在 callers／selectors／tests 核对后同一变更删除，不留旧新效果同时作用 |
| 必要兼容 | 旧公开 CSS aliases／plugin token、native selects／dialogs、legacy Live、两种 task/workspace owner、原 Context 编辑和 Session Patrol 高级入口 | 有真实消费者或独立业务语义。本次不删除；只有后续明确迁移消费者／业务审批后退出，不为此添加新兼容分支 |

视图 helper 用闭包／函数，不为形式建类；helpers 默认模块内部。旧控制器持有最终显示意图，新 helper 只执行当前 element 的过渡，不成为第二显示状态源。实现文件头必须说明公开能力、输入含义、输出、具体工作流和真实例子；内部仅解释必要约束，不逐行注释。实质修改后核对文件头；必要安全说明／许可保留。

CSS 顺序目前为 legacy styles.css→tokens→base→components→shell→views→其他专用样式→workspace-patrol。按职责原位改规则，核对 computed style 胜出者，合并同文件重复 `.observation-identities` 等本次触及声明；不能在最后追加覆盖块掩盖层级。`[hidden]`、visually-hidden、reduce-motion 的必要 !important 保留；不增加布局／颜色专用 !important。主界面静止时不新增 rAF、呼吸或漂浮；旧默认关闭的 Session Patrol 头像功能保留，针对本次实际挂载检查其装饰循环，若开启时存在无意义 hover/pulse 则用静态真实状态与交互反馈替代，不扩成头像业务重写或 splash 重构。

### D7. 能力迁移与保留清单

下表引用旧迁移 M01–M45 以明确覆盖范围，不以旧文档的“已通过”代替本次验证。“调整”只指显示和动效；**无拟废弃业务能力**。

| 旧项／已有能力 | 处理 | 新界面承接位置 | 接入方式 | 本次验收 |
| --- | --- | --- | --- | --- |
| M01–M02 六导航、设置、bootstrap／session／连接重试 | 调整视觉、保留合同 | 原 shell | 原 actions／bootstrap/preload/CSP | 六入口、空库、连接失败恢复、窗口缩放；不新增菜单体系 |
| M03 工作区系统选择／确认／取消 | 调整 | 输入内 workspace button／原 picker | selectWorkspace、POST workspaces | 选择无执行、迟到／取消不污染、确认等待反馈 |
| M04–M06 四类表达、指定回答、稳定 submission、历史／更早／失败重试 | 调整 | 同一个 composer、原历史 dialog | workspace inputs store／intake/history/restart | 发送后 quiet；草稿、request id/revision、同 ID retry；accepted≠生效 |
| M07 Live/SSE 连接／断线／重同步 | 保留 | 原状态与按需详情 | 原 Store/Reducer/Connection | 展开收回不重订阅；gap 重同步、旧 workspace 无污染 |
| M08 Task Progress／版本／blocked retry | 调整 | 右侧内容卡与原检查 dialog | taskProgress／retry 原 API | pending 不覆盖 head，指定版本／失败／retry 无假成功 |
| M09 冻结 Observation／分页／前序 | 调整 | 右侧内容卡与原冻结检查 | observation_id 白名单查询 | 跨轮/历史不混读，加载／错误保留固定身份和隐私 |
| M10 集合关系／多父 Revision DAG | 保留、调整工具条 | 左关系图，明确全图入口 | committedLineage＋PortfolioMapView | 布局、zoom、scroll、revision edges 稳定；候选不冒充发布 |
| M11／M42 Context 概要／会话／来源与版本／任务打开 | 调整 | 原共享非模态 inspector | conversation/revisions/revision、onOpenTask | 切 A/B／分页／Esc、焦点返回；抽屉检查后的发送仍 workspace |
| M12 LoopFact／filters／分页／来源详情 | 调整视觉 | 下方事实、原 dialog | 原 facts/factDetail | unknown 不算成功，历史 tool 排除、筛选与阅读位置保持 |
| M13 工作区暂停／继续／停止／授权预算／等待恢复 | 调整浮层 | 原控制菜单／grant/wait dialog | 原 control/grant/wait/recovery API | pending 与实际状态分离，错误可读、close 不产生控制动作 |
| M14–M16 task Loop/Mission/三类 intervention/audit/adoption/压缩用量 | 保留 | 原任务持续推进／高级控制台 | 原 controller/API/明确目标 | 按现有针对性合同回归和可达性检查，不伪造外部执行完成 |
| M17–M19 任务列表、新建／切换、Main／SSE／会话与工具详情 | 调整共用材质、保留 | 任务三栏 | TaskRunOperations、composer draft、conversation reconciler | 任务草稿／selection／滚动不丢，真实 Run/stream／accepted 分离 |
| M20–M21 中断/resume/Must-view/commit 九阶段/handoff | 保留 | 原会话门禁和高级面板 | 原 gate/resume/abandon 链 | 受影响 DOM 调用回归；审批语义不因 dialog 退出改变 |
| M22–M25 手工派生／编辑／projection/压缩/Session Patrol 编排投放 | 保留 | 原任务检查器、全图和工作台 | 原 editor／compile／preview／deploy API | 入口可达、草稿不因共用 style 覆盖、实际合同回归；无新执行链 |
| M26–M27 Curator/Teammate/Worker/Swarm 查询与控制 | 保留 | 原代理检查器／详情 | 原 agents/history/control APIs | 入口／冻结身份及受影响回归，不另测整轮外部 Provider |
| M28–M31 材料预览、插件 viewer、读写规则、版本、分组／排序、本轮选材／备注／必看／使用历史 | 调整控制外壳、保留 | 原右材料检查器／文件面板／选材控件 | 原 loader/Blob、material/group/run-material APIs | 真实临时磁盘材料＋选材；focus／draft／Blob cleanup；不改写用户文件 |
| M32–M34 全图树／卡片／缩放／归档删除／装备小兵 | 保留、调整操作层 | 原全图／高级入口 | 原 map／lifecycle／Session Patrol tools | 图卡键盘、选择、zoom、明确装备；原确认保留、无破坏性用户验收 |
| M35 插件状态、接口冲突、依赖、轨迹／reload | 调整 | 原插件页 | 原 plugin API/assets/opener | 状态真实、reload pending/error、插件样式边界，无伪 enable |
| M36 无工作区模式／独立草稿与材料 | 调整共用操作层 | 原独立会话 | 原 assembly identity/Main/材料 | 空库可达，和任务／Patrol 身份隔离；正文不玻璃化 |
| M37 记忆 CRUD／采集／分段／合并／再压缩 | 调整 | 原记忆库及 dialog | 原 memory service/API | 保存失败不丢正文，历史来源／分段保留 |
| M38–M41 模型设置／密钥／访问模式／sandbox／语言／缩放／Markdown/资源 | 保留、调整共用控件 | 原设置／gate／shell | 原 settings、access、i18n、preload、renderer | 键盘、200% zoom、必要窄窗；不暴露密钥、不弱化 CSP |
| M43 重复 UI/CSS | 合并本次触及的显示职责 | 原组件／专用 CSS | 搜索全部 callers／selectors／tests 后替换 | 无双重动画／token 来源；旧业务入口可达 |
| M44–M45 旧原型模拟结果／插件预览开关已按旧审批移除 | 保持现状 | 不重新引入 | 生产 API/Live | 不用固定节点、百分比或假成功；视觉 fixture 明确隔离 |

### D8. 性能验收：先补有效基线，再比较

软件 probe 只说明当前少量数据的 DOM 响应与立即跳变。正式 Fast 验收必须在**同一 Windows 机器、同一窗口／缩放／电源／GPU模式、同一数据快照**，在产品修改之前保存硬件基线，修改之后重放；若环境不可复现则注明不可比较，不设脱离场景的绝对 fps。

| 场景／数据规模 | 测量方式 | 拟定通过标准（Focus 选择，不是 Apple 标准） |
| --- | --- | --- |
| 本地 press/select/展开/关闭 | 每场景30次，预热5次，3组；可信 pointer/keyboard 的 event→首个可见 feedback，CDP/Chromium input/render trace 与连续帧；网络不计入本地反馈 | 基准硬件无 throttling 时 P95≤50ms；相对硬件基线恶化≤10ms；两项都满足。rAF proxy 单独列，不能代替 presented-frame |
| 关键闭环／反向／对象切换 | 20组，反向间隔40／80／120ms，实际窗口连续录屏或采样帧；记录 time origin、实际时间、有效输入与最终 identity | 不锁操作、无排队旧动画、无起点闪回；正文可编辑；最终 hidden/inert 无透明拦截，退出状态与最后意图一致 |
| 图／事实 Live | 32 Context／64 revision edges／200 facts 窗口，突发100事件／5s，并同时编辑输入；测试 fixture 与真实路由证据分开 | 图 topology 未变时坐标不重算，焦点／scroll/zoom 保持；无整页入场、无新订阅。UI事件→可见更新P95≤100ms（不含网络到达前时间），按旧 sequence 仍确定归约 |
| 大图／长会话／滚动 | 128 Context／256边；1000消息历史分页、200可见事实，20s滚动＋输入；受控合成 fixture 明确标注，业务闭环仍走真实接口 | 相同负载下长任务总时长／p95 frame interval 相对基线增长≤10%；若基线本身差，单独定位本次新增工作，不能用相对标准遮掩核心闭环卡顿；不得有本次引入的>100ms renderer longtask |
| 静止与反复开关 | 10s静止、50次开关后再10s；MutationObserver、trace、listeners／节点／未完成 animation 计数 | 无新增常驻 rAF／光效，隐藏 surface 不绘制；无增长中的 overlay/handler、未收口动画或跨页写入；真实后台事件仍允许局部更新 |

保存 Chromium trace 中 Script／Style／Layout／Paint／Composite 与 longtask／输入呈现数据，而非只记 duration token。不得在基线与改造版本一个软件一个硬件比较。若 trace 无法给出可信 presentation identity，报告 proxy 和 **未验证**，不能仅凭没超时过关。轻量数据与压力数据单独报告，不把 mock 压力证明真实业务结果。

### D9. 可访问性与证据分层

- 尊重已有 `prefers-reduced-motion`、`update: slow`；JS 原生动画也必须读取相同偏好，偏好改变立即收敛。减少动画时取消位移／形变／光感扩散，仍有明确选中、焦点、pending／error／accepted 文字与边缘反馈。
- 检查 `prefers-reduced-transparency`、`prefers-contrast: more`、`forced-colors` 与 `@supports`，透明不支持时实色降级。Skill 对 Chromium 偏好映射的说法不当作实测；分别记录媒体查询模拟与真实 Windows 设置检测，映射未知时标未验证。高对比标签／轮廓按系统色保留。
- Keyboard-only 的 Tab、Shift-Tab、Enter／Space、Esc、focus-visible、IME、native select，非模态不做全局 trap；必要窄窗900／640／390 CSS px 与200%缩放，折叠顺序图→进度→冻结→事实不变，输入可达。390只是现有布局回归，不新增移动产品。
- 业务证据：接口／状态／id／revision／草稿／权限断言。视觉证据：同尺寸、同数据、同模式截图，并查看实际材质边缘和真实背景。动态证据：闭环＋快速反向＋对象切换录屏或实际时刻连续帧，人工查看，不能拼终态图冒充动画。性能证据：独立 trace 和指标。四类逐项独立 verdict；功能 pass 不提高其他 verdict。
- 正常光学路径和实色降级各有图；没有截图、连续帧或硬件记录就标未验证，不靠 Skill／HIG 标签或 CSS 属性自证。无法取得真实 GPU 窗口测量时，玻璃方向可由真实页面视觉判断，但 **Fast 验收仍未完成**。

## Risks / Trade-offs

- [中性工作台背景让透光不显著] → 用背景参与、非均匀边缘／厚度、交互响应及真实滚动内容联合判断；不添装饰色，不把普通磨砂认定完成。
- [全局 token 影响旧页面／插件] → 保留公开别名，核对 computed style 和调用者，按迁移表定向回归；不修改插件业务或编辑器 bundle。
- [动画结束后旧回调 hidden 新界面／夺焦点] → element generation、abort/cleanup、最后意图终态与反向测试；dispose立即清理；helper不读取业务状态。
- [composer resize 导致正文缩放／每帧 reflow] → 不缩放文字，先有限 geometry 试验与 trace，集中同一个 retained node；成本不达标回到实现选择修正，不扩动画框架。
- [大面积 backdrop 与复杂玻璃祖先拖慢渲染] → 少量、裁剪控制面，无嵌套 backdrop，无常驻 will-change；透明降级不删业务；硬件基线比较。
- [审批范围膨胀] → 三个能力、一个闭环先行，无额外主题／导航系统／依赖。需新增滤镜引擎或业务删除，修改本 change 后重新审批。

## Migration Plan

1. **当前到此仅规划**：完成 artifacts／严格校验，停止等待用户明确批准本 change。保持当前产品文件、依赖、构建和安装应用不变。
2. 获批后先重新检查基线和用户工作区变化，取得可比较的硬件渲染基线，完成实际支持探测；沿既有 OpenSpec apply 检查任务，不擅自重置或清理。
3. 在原位 token／surface 和核心显示生命周期完成 Patrol 闭环，查看真实页面、动态和性能证据，确认满足已批标准；不把完整铺页当作方向验证。
4. 推广共用控制语言；按 D7 清单逐项保留、调整、替代／删除，清理本次旧效果和声明，检查新文件头。权限、真实 API 和数据不迁移。
5. 仅运行受影响 Node／Electron 与关键真实 PostgreSQL/HTTP/SSE 用例；只有出现共享后端合同变更或定向覆盖不足的具体证据才扩大测试，并记录原因。
6. 交付保留同窗口／数据的前后截图、闭环录屏或连续帧、trace、业务证据。按已批清单报告完成／未完成／未验证；不会增加新的优化清单。
7. 分别统计业务（JS/CSS/HTML及必要头注释）和测试新增／删除／净增行数，排除规划／生成／第三方／既有用户修改；解释 helper、测试增长和旧效果删除证据。无需强求净减，不能压缩格式或删测试换数字。
8. 回退仅撤回本次显示层文件，并保留用户既有修改和证据；无数据／权限迁移或缓存清库。对安装版的发布／重启不在当前授权内。

## Open Questions

无未决产品范围或审批前设计选择。blur／高光强度、具体短过渡曲线以及 Windows 偏好映射的实际支持属于获批后以本设计标准校准和报告的实现事实，不得借校准降低四项验收或新增功能。
