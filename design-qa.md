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
