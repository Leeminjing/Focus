**Comparison Target**

- Source visual truth: `C:\Users\brubing\AppData\Local\Temp\codex-clipboard-8b77ceb9-b7b3-4092-8db6-1e7412d12ba2.png`
- Implementation screenshot: unavailable; the revised local preview is open in the Codex browser panel.
- State: Page 1 with an empty task, no registered materials, and the desktop window chrome visible.

**Findings**

- [P1] Visual comparison is blocked because the in-app browser control runtime could not initialize, so a current implementation screenshot and console inspection could not be captured.
  Fix: repeat the same-state capture when browser control is available and compare it with the source in one input.

**Implementation Evidence**

- Backend uses LangGraph `messages` mode for token chunks while retaining `values` snapshots.
- Frontend batches token DOM writes with `requestAnimationFrame` and updates only the active message node.
- User messages align right in a restrained blue-gray bubble; assistant messages stay left with a blue role label.
- Completed assistant messages render escaped semantic paragraphs and lists; streaming remains low-overhead text until completion.
- Electron suppresses the native application menu on startup.
- The Page 1 header is reduced to a 54px toolbar; the transient saved status clears automatically.
- Composer actions are grouped into a compact trailing control area with a primary send button.
- The empty materials state is a single muted line instead of an empty bordered card.
- JavaScript syntax checks and served-asset assertions pass.

**Required Fidelity Surfaces**

- Fonts and typography: unchanged from the established Page 1 design.
- Spacing and layout rhythm: header, composer actions, and empty materials state are compacted; screenshot verification pending.
- Colors and visual tokens: existing neutral, blue, and muted tokens preserved.
- Image quality and assets: no image assets are used in this UI region.
- Copy and content: the empty material hint is shortened to `暂无材料`.

**Implementation Checklist**

- [x] Replace state-only streaming with token streaming.
- [x] Avoid full Page 1 rerenders for every stream event.
- [x] Give user and assistant messages distinct alignment and surface treatment.
- [x] Reduce body line-height and semantic spacing.
- [x] Remove the native Electron menu bar on desktop startup.
- [x] Compact the Page 1 header and saved-status treatment.
- [x] Compact composer actions and the empty materials state.
- [ ] Capture and compare the revised implementation at the source state.

final result: blocked

---

## Context rail implementation QA — 2026-08-15

**Comparison Target**

- Source visual truth: `C:\Users\brubing\AppData\Local\Temp\codex-clipboard-d26ef17c-fbec-42d6-88cd-a843fda5a8b4.png` (`2139 × 1355`).
- Normalized source: `C:\Users\brubing\Desktop\ag-project\focus\design-qa-context-rail-reference-normalized.png` (`2139 × 1188`), cropped from y=43 to remove Windows title chrome and match the browser capture region.
- Implementation screenshot: `C:\Users\brubing\Desktop\ag-project\focus\design-qa-context-rail.png` (`2139 × 1188`).
- Narrow implementation screenshot: `C:\Users\brubing\Desktop\ag-project\focus\design-qa-context-rail-narrow.png` (`900 × 680`).
- Browser: Codex in-app Chromium browser; CSS viewport override `2139 × 1416`, capture `2139 × 1188`, device pixel ratio `1`.
- State: existing root Context selected; nine saved Contexts visible in the right rail; composer and conversation visible.

**Findings**

- No actionable P0/P1/P2 findings remain. The rail is permanently attached to the right edge, keeps the conversation usable, presents a clear selected state, and uses compact real Context cards instead of the reference sketch's red annotations and empty placeholders.

**Required Fidelity Surfaces**

- Fonts and typography: reuses Focus's established Segoe UI/Microsoft YaHei stack, weights and truncation; card titles remain legible at desktop and 900px widths.
- Spacing and layout rhythm: the final 310px rail begins at x=1813 in the 2139px capture, matching the reference rail beginning at approximately x=1810; the rail and conversation have separate scroll regions.
- Colors and visual tokens: existing neutral surfaces, blue selected state and Focus radii are preserved; the reference's red boxes are treated as annotations, not product styling.
- Image quality and assets: this UI contains no image assets or icons requiring substitution; no CSS/SVG placeholder art was introduced.
- Copy and content: cards show real titles, root/derived identity, short Context IDs, blocked state and extra-parent labels; `新增 Context` replaces the reference's unlabeled plus with an explicit accessible action.

**Interaction and Accessibility Evidence**

- Browser test switched root → derived → root; all nine Context cards remained present and the selected `aria-current` moved to the active Context.
- Electron regression separately verifies per-Context input and conversation scroll restoration, multi-parent labels, blocked state, rail scrolling and derivation source.
- Native buttons provide keyboard focus; the rail is a labelled `aside` with labelled `nav`; selected state uses `aria-current`.
- Browser console contained no errors during initial rendering or Context switching.
- At 900px, the main area measured 648px and the independent rail 210px; composer and rail did not overlap or clip.

**Comparison History**

- Pass 1: [P1] the first implementation used a centered 1560px shell, leaving a large blank strip to the right of the rail. Fixed by making the shell fill the app width and pinning the rail to the right edge.
- Pass 2: [P2] the 280px rail was visibly narrower than the approximately 310px target region. Fixed by using a 310px desktop track while retaining the existing 210px narrow-window rule.
- Pass 3: the normalized source and final implementation were compared together at `2139 × 1188`; no actionable P0/P1/P2 mismatch remained.

**Implementation Checklist**

- [x] Keep parent, child, sibling and multi-parent Contexts visible in the Focus page.
- [x] Switch only the active independent Context and preserve other cards.
- [x] Put derivation in the rail and remove the duplicate composer action.
- [x] Keep rail and conversation scrolling independent.
- [x] Preserve Focus visual tokens and native accessibility.
- [x] Verify desktop and narrow layouts, interactions and console errors.

**Follow-up Polish**

- None required for this change.

final result: passed

---

## F22 Inspector 标题栏删除与导航隔离 QA — 2026-08-22

**Comparison Target**

- Source visual truth: `C:\Users\brubing\AppData\Local\Temp\codex-clipboard-e123aecc-b11e-4bf0-80a2-b6dd2b38ca42.png` (`2560 × 1368`)，绿框要求删除 Inspector 标题栏，红框要求左右两套导航取消联动。
- Implementation screenshot: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\after\inspector-navigation\focus-main-real.png`（正常桌面权限运行的真实 Focus，Windows 150% 输出 `2582 × 1390`，右侧“运行”状态）。
- Interaction screenshot: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\after\inspector-navigation\focus-context-isolated-real.png`（点击右侧 Contexts 后，左侧仍保持“任务”选中）。
- Combined comparison: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\after\inspector-navigation\before-after.png`；实现图裁去窗口外沿 11px 后与来源统一为 `2560 × 1368` 再并排。
- State: 已选任务、Commitment 第 2 阶段等待确认、Inspector 打开；参考与实现均以“运行”Tab 为视觉比较状态。

**Findings**

- No actionable P0/P1/P2 findings remain. Inspector 顶部重复标题行已完全删除，分段 Tab 直接成为右栏第一层；点击右侧 Contexts 后左侧仍准确标识主工作区“任务”，不再伪造主页面跳转。

**Required Fidelity Surfaces**

- Fonts and typography: 删除 `INSPECTOR` 与动态标题后没有增加替代文案；Tab、Inspector 正文和左侧导航原有字体层级保持不变。
- Spacing and layout rhythm: `.app-inspector` 从三行网格收敛为 `auto + minmax(0, 1fr)` 两行；Tab 上移到右栏顶部，未留下空白占位或负边距补偿。
- Colors and visual tokens: 分段选中面、Context 完整边框和主导航中性选中面保持既有 token，不新增颜色、阴影或装饰。
- Copy and content: 只删除重复的 kicker、动态标题和标题栏关闭按钮；四个 Tab 与所有 Inspector 内容保持完整。

**Interaction and Accessibility Evidence**

- `aside#appInspector` 以 `aria-label="任务检查器"` 自带可访问名称，不再依赖已删除的标题节点。
- 真实 Electron 守卫逐个点击 Contexts、材料、Agents、运行，四次均测得 `state.view === "focus"` 且左侧唯一 `aria-current="page"` 始终为 `data-nav-key="focus"`。
- Inspector Tab 的键盘左右/Home/End 切换仍调用同一局部 `openInspector()`；Escape 关闭与焦点归还逻辑保持不变。
- 全部 Node/插件测试与 7 组 Electron E2E 通过；全视图审计覆盖 13 类状态 × 5 组窗口/缩放，无新增横向溢出、无名按钮或 renderer 错误。
- 新版以正常桌面权限启动，页面、API、SSE 与插件仍共享动态 loopback Origin；健康页和 `/desktop/` 均为 200，响应无 CORS 头。

**Comparison History**

- Pass 1: 静态守卫确认旧 `.inspector-header`、`#inspectorTitle` 和三行网格存在；Electron 守卫复现右侧 Contexts/Agents 会让左侧分别跳转到同名项。
- Pass 2: 删除标题栏 DOM、查询、标题映射、同步和 CSS，并让 `activeNavigationKey()` 只读取 `state.view`；同状态并排图与 Contexts 隔离截图均未发现残留 P0/P1/P2。

**Implementation Checklist**

- [x] 删除 Inspector 标题栏 DOM，而不是 CSS 隐藏。
- [x] 删除动态标题查询、映射、同步和无调用的关闭 action 分支。
- [x] 将 Inspector 收敛为 Tab 与内容两行。
- [x] 左侧 `aria-current` 只由主工作区 `state.view` 派生。
- [x] 右侧四个 Tab 只更新 `state.inspector.tab`。
- [x] 验证真实桌面视觉、键盘语义、全视图、零新增依赖与动态同源。

**Follow-up Polish**

- None required for this change.

final result: passed

---

## F22 单层标题栏与品牌去重 QA — 2026-08-22

**Comparison Target**

- Main source visual truth: `C:\Users\brubing\AppData\Local\Temp\codex-clipboard-31b294cf-0a25-4187-9245-9209b1680b1b.png`；用户最终明确要求删除 Windows 原生标题栏左侧的图标/`Focus` 与应用 Logo 右侧的 `Focus` 文字，但保留应用头部橙色 Logo。
- Main implementation screenshot: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\after\branding\focus-main-real.png`；正常桌面权限启动后的真实 `BrowserWindow`，物理窗口 `1400 × 1031`、Windows device scale `1.5`，任务 / 运行 Inspector / 等待确认状态。
- Main combined evidence: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\after\branding\main-before-after.png`；源图与真实实现同状态并排。
- Splash source visual truth: `C:\Users\brubing\AppData\Local\Temp\codex-clipboard-785ba8a3-a770-4a8f-9f7a-05b39707608f.png`；红框要求删除 Logo 下的 `LOCAL AGENT WORKSPACE / Focus` 重复文案。
- Splash implementation screenshots: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\after\branding\focus-splash.png`（真实 `440 × 340` splash window）与 `focus-splash-759.png`（同源参考视口）。
- Splash combined evidence: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\after\branding\splash-before-after.png`；两侧归一到 `759 × 759` 后并排检查。

**Findings**

- No actionable P0/P1/P2 findings remain. 主窗口从“Windows 标题栏 + 应用品牌栏”两层收敛为一层 Window Controls Overlay：原生左侧图标/标题消失，橙色应用 Logo 保留且旁边不再重复 `Focus`，当前任务上下文直接跟随 Logo；最小化、最大化、关闭与新增任务互不遮挡。启动页只剩一次含字标 Logo、阶段状态、进度和本地准备说明。

**Required Fidelity Surfaces**

- Fonts and typography: 当前任务标题、workspace/短 ID、全局状态和动作字体均沿用 Focus 现有 Segoe UI/Microsoft YaHei 层级；只删除重复 `Focus`，没有用新的营销文案填补。
- Spacing and layout rhythm: 56px 头部现在是 `40px Logo / flexible task context / status / new task / native controls`；Window Controls Overlay 的 `titlebar-area-*` 环境变量负责真实系统按钮避让。Splash 将品牌图扩大到 104px，并以 15/17/12px 的 mark/status/progress/note 节奏重新居中。
- Colors and visual tokens: 标题栏覆盖色、符号色和宿主表面继续消费既有白色/深墨色与蓝白灰 token；Splash 继续使用原有橙色品牌资产与渐变进度。
- Image quality and assets: 应用头部和启动页均复用现有 `focus-icon.png`，没有重绘、拉伸、占位图、CSS 图形或新增网络资产。
- Copy and content: 删除 Windows 原生 `Focus`、应用 Logo 右侧 `Focus`、Splash 的 `LOCAL AGENT WORKSPACE` 与第二个 `Focus`；保留任务标题、工作区/短 ID、启动阶段和本地准备说明。

**Interaction and Accessibility Evidence**

- 主 `BrowserWindow` 使用 `titleBarStyle: "hidden"` + `titleBarOverlay`，真实 Window Controls Overlay API 返回可见、56px 高可用区域；新增任务按钮右边界始终位于 `titlebar-area` 内。
- `.app-header` 是拖拽区，`.app-mark` 与头部按钮是 `no-drag`；Logo 仍是带 `aria-label="返回当前任务"` 的任务入口。
- 26 个 Node/插件测试与 7 个 Electron E2E 全部通过；65 个全视图/窗口/缩放组合、900×680@150%、reduced-motion、无横向溢出和无名按钮守卫通过。
- 新 Focus 正常桌面启动：Electron PID `34452`、唯一 FastAPI 子进程 PID `35904`、动态同源 `http://127.0.0.1:49354`；`/health` 与 `/desktop/` 均为 200，无 CORS 响应头，监听数为 1。
- Codex 应用内浏览器连接仍受可信 RPC 路径限制；本次使用真实 Electron Window Controls Overlay、OS 窗口截图、computed geometry 与同状态组合图完成验收。

**Comparison History**

- Pass 1: [P1] 初版按首条指示同时删除应用 Logo 和 `Focus`，用户随后明确 Logo 必须保留。修正：用仅含图片的 `.app-mark` 恢复橙色 Logo，旧 `.brand` 和文本字标保持删除。
- Pass 2: [P1] 普通 `capturePage()` 不包含原生窗口控制按钮，无法证明系统按钮保留。修正：正常权限启动 Focus，按真实 DPI 将窗口置于屏幕内并使用 OS 窗口边界截图；最小化、最大化、关闭和避让区均可见。
- Pass 3: 主窗口和 Splash 分别与用户源图组合检查；没有残留双层标题栏、重复品牌文案、控制区遮挡、启动页失衡或 P0/P1/P2 视觉退化。

**Implementation Checklist**

- [x] 删除 Windows 原生标题栏左侧应用图标和标题，但保留原生窗口控制。
- [x] 保留应用头部橙色 Logo，删除右侧重复 `Focus` 文字和旧 `.brand` DOM/CSS。
- [x] 使用系统提供的 Window Controls Overlay 几何避让，不硬编码控制按钮占位。
- [x] 删除 Splash 重复 kicker/标题 DOM 与 CSS，重新平衡真实窗口和参考视口比例。
- [x] 保持零新增依赖、动态 loopback 同源、现有业务状态和可访问性。

**Follow-up Polish**

- None required for this change.

final result: passed

---

## F22 重复 Workspace Header 删除 QA — 2026-08-22

**Comparison Target**

- Source visual truth: `C:\Users\brubing\AppData\Local\Temp\codex-clipboard-ab2ad1aa-8162-405d-a9e1-7e7039fa910f.png` (`2560 × 1373`)，用户红框明确要求保留应用顶栏的小任务上下文并删除主内容区的大型 Workspace Header。
- Implementation screenshot: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\workspace-header-after\1440x1024-z1-01-focus.png`（CSS 窗口 `1440 × 1024`、zoom `1`；Windows 150% 输出为 `2139 × 1442` 像素）。
- Full-view comparison: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\comparisons\05-workspace-full-before-after.png` (`3071 × 934`)。
- Focused comparison: `C:\Users\brubing\Desktop\ag-project\focus\openspec\changes\f22-premium-interaction-system\qa\comparisons\04-workspace-header-before-after.png` (`2418 × 174`)。
- Normalization: source 去除顶部 32px Windows 标题栏；聚焦比较将两侧顶栏/工作区顶部裁切后统一为 1200px 宽，完整比较统一为 900px 高。测试任务标题与会话正文是确定性 QA fixture，与用户真实数据不同，不参与本次结构判断。
- State: 已选择任务的 Focus 主工作面，左侧“任务”选中，顶栏任务上下文和 Composer 可见。

**Findings**

- No actionable P0/P1/P2 findings remain. 主内容区的大型 Workspace Header 已完全消失，应用顶栏仍保留任务标题、工作区和短 ID；页面内容从顶栏下方直接开始，没有隐藏占位或 58px 空行。

**Required Fidelity Surfaces**

- Fonts and typography: 顶栏原有字体、字号、字重、截断和双行层级保持不变；删除区域没有通过新增字体或替代标题补回。
- Spacing and layout rhythm: `.app-workspace` 从 `auto + content` 两行收敛为单个 `minmax(0, 1fr)` 内容行；13 类状态 × 5 组窗口/缩放均测得页面内容顶边与工作区顶边重合。
- Colors and visual tokens: 蓝白灰 token、导航选中面、正文表面和边界保持不变；没有新增颜色或装饰。
- Image quality and asset fidelity: 本次不新增或替换图片、图标和品牌资产；现有 Focus 图标保持原样。
- Copy and content: 删除重复的 `WORKSPACE`、第二个任务标题和路径；顶栏的任务标题、工作区与短 ID 成为唯一全局任务上下文。

**Interaction and Accessibility Evidence**

- 26 个 Node/插件测试通过；Context、响应式、稳定性、F21、F22 与全视图 Electron E2E 通过。
- 65 个真实 Electron 状态组合验证不存在第二套 Header、隐藏空行、页面横向溢出、无名按钮、重复 ID、错误 `aria-controls` 或 renderer 错误。
- 左侧导航、Composer、Inspector、dialog 和文件单工作面交互保持可用；删除区域没有焦点目标，因此不改变 Tab 顺序或 ARIA 关系。
- Codex 应用内浏览器连接受可信 RPC 路径限制；本次使用与用户截图相同产品运行时的真实 Electron capture、交互和 console 守卫完成验收。

**Comparison History**

- Pass 1: [P1] 应用顶栏和 `.workspace-context` 同时维护任务标题，形成重复信息和 58px 永久占位。修正：删除第二套 DOM、`viewHeading()`、三个节点查询/同步及全部基础/响应式 CSS。
- Pass 2: 聚焦与完整并排图确认主内容直接接在应用顶栏之后；静态守卫和 65 状态几何守卫均通过，无残留 P0/P1/P2。

**Implementation Checklist**

- [x] 删除 `.workspace-context` DOM，而不是 CSS 隐藏。
- [x] 删除第二套 renderer 标题状态和 `viewHeading()`。
- [x] 删除全部基础与响应式 Workspace Header CSS。
- [x] 将主工作区收敛为单内容行。
- [x] 验证全视图、多窗口、缩放、交互、无新增依赖与同源拓扑。

**Follow-up Polish**

- None required for this change.

final result: passed

---

## Context 双栏组装器 QA — 2026-08-15

**验证场景**

- Electron 视口：`1600 × 1000`。
- 实现截图：`C:\Users\brubing\Desktop\ag-project\focus\design-qa-context-composer.png`。
- 状态：14 条长来源消息，包含完整 Tool 协议字段；右栏从空白开始，通过 Pointer 拖拽和“加入”操作自由组装，并展开一条 Tool 消息编辑完整 JSON。

**验证结果**

- 左右两栏独立滚动；删除、撤销、复制、展开和排序不重建未受影响卡片，也不让右栏跳回顶部。
- Pointer 拖拽具有浮动副本、动态占位、落位动画、边缘自动滚动和取消恢复；键盘可执行等价排序。
- 来源消息保持只读且不随组装变化；深拷贝完整保留 `tool_call_id`、`name`、`additional_kwargs` 等高级字段。
- 紧凑卡片默认只显示角色、协议摘要和内容预览；单卡片可原地展开专业 JSON 编辑。
- 删除提供短时撤销；`prefers-reduced-motion` 下功能保持完整且关闭非必要动画。
- 没有新增前端依赖，Context 编辑器不再使用 HTML5 `draggable`。

**修正记录**

- [P1] 首轮 `42% + 58% + gap` 网格造成横向溢出。已改为 `5fr / 7fr`，并为面板和卡片补充 `min-width: 0` 与边界 overflow 约束。
- 修正后同尺寸截图未发现残留 P0/P1/P2 问题。

final result: passed
