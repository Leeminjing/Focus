# 2026-10-17 机器人与面板的原生层级恢复

用户再次报告只看到输入框。实际机器人窗口可枚举且无障碍树包含机器人图片，但未激活时原生捕获为空白；因此仅捕获输入框不能证明两者同时显示。尚不能据此确定哪个系统操作丢失了原生置顶状态。

- 在真实 Electron 回归中模拟两个既有浮窗丢失置顶状态，再走生产 `restore()`。修复前出现 `false !== true`，对应机器人未恢复到桌面浮层。
- 启动、恢复和展开输入框时，宿主统一重设两个窗口的原生置顶状态并使用 `moveTop()` 提升可见窗口，不抢走输入框焦点。
- 增加 `--show-patrol-panel` 启动入口，同时处理单实例唤回；显式展示时展开实际面板，不再仅证明后台进程存在。
- Windows 页面回归改用与实际应用一致的 GPU 配置。宿主和真实 Electron 共 12 项定向检查通过。
- 实际应用已以 `--show-patrol-panel` 重启为 PID 5064，后端端口 50946。未调用额外的窗口激活操作，分别捕获到正常显示的输入框和机器人；原生窗口位置相邻，机器人位于面板右侧。原始证据为 `C:/Users/brubing/.codex/visualizations/2026/10/17/01a14936-6b0f-76f3-bdad-6d9908be7a0e/actual-desktop/panel-restored.png` 和同目录 `robot-restored.png`。

下方 PID 和原生截图属于前一轮记录，不作为本轮实际启动成功的证据。

## 本地 main 整合验证

与本地 main 的新增功能整合时，保留了持久草稿及未确认发送恢复、预算预设和隐藏面板暂停刷新/Revision 缓存。未绑定时隐藏预算区以保持紧凑初始尺寸，绑定后预算控制照常提供。冲突整合后的 54 项 Patrol、原生 UI、恢复、输入和预算测试全部通过；启动生命周期、运行时准备和启动 UI 检查也通过。测试夹具同步加载文件引用模块，并等待刷新后的新 SSE 连接；发送状态断言读取状态节点，避免误匹配用户正文。

---

# 2026-10-17 Windows 原生灰框修复

用户的桌面截图指出灰色矩形外框、双层圆角和机器人底板。上轮 `capturePage` 只验证 Chromium 内容，没有覆盖 Windows 合成后的非客户区；上轮对原生外观的 passed 结论不充分。本节取代下方关于 Acrylic、硬裁剪区域与原生边缘的结论。

## 复现与修复

- 使用 computer-use / `@oai/sky` 实际捕获 Windows 窗口，复现 288×109 内容外还有约 302×117 的系统外框。宿主配置回归先因缺少 `thickFrame:false` 失败，再修复通过。
- 面板和机器人明确关闭 Windows 厚边框、系统阴影与系统圆角；两个窗口均使用真正的 alpha 透明，整窗 `backgroundMaterial` 改为 `none`。不再叠加 Windows Acrylic 灰底。
- 删除上一轮按 DIP 扫描行设置 `setShape` 的方案和对应 IPC，由 Chromium 绘制平滑圆角与透明边缘，避免 150% 缩放时的硬裁剪台阶。
- 可见面板保持 288×109，机器人大小保持不变。面板原生画布为 304×125，其中四周 8 DIP 为透明阴影空间；定位抵消该空间，保持可见卡片与机器人的原有间距。原生宽高允许最多一个 DIP 的缩放取整，可见卡片宽度仍精确断言 288。
- 卡片采用稳定的冷白半透明表面与轻微 CSS 阴影，去掉叠加的背景模糊滤镜。当前方案不声称使用系统 Acrylic 模糊桌面。
- 键盘焦点边框收为 1px；输入框使用插入光标和卡片边界提示焦点，搜索框由搜索容器提示焦点，避免粗蓝框。机器人悬浮提示缩至 8px，并显式按内容宽度排版，不再省略截断。

## 原生验证

- 测试页面的默认软件渲染检查继续通过；额外增加手工原生模式，启用真实 GPU，在统一浅色 / 深色底色下检查 Windows 合成后的面板、机器人、菜单和边缘。底色是隔离 fixture 的所有者窗口，不是生产业务内容。
- 深色验收曾被测试底色窗口遮挡；检查到页面 opacity=1、动画已结束，修正验证窗口所有者关系后，原生画面正常。该问题归属于验证夹具的覆盖关系，未将其误报为生产透明材质故障。
- `C:/Users/brubing/.codex/visualizations/2026/10/17/01a14936-6b0f-76f3-bdad-6d9908be7a0e/native-frame-audit/panel-windows.png` 是浅色背景上的 Windows 原生面板截图；`scene-dark.png`、`panel-dark.png`、`robot-dark.png` 为深色底色的原始 Windows 合成截图。没有用 `capturePage` 冒充这些原生证据，也没有重绘或覆盖灰框。
- `native-frame-audit/renderer/` 的 13 张生产 renderer 截图与尺寸 JSON 补充检查布局；它们不替代上述 Windows 原生取证。
- 本轮宿主 11 项 + 真实 Electron 页面 1 项，共 12 项定向检查通过，覆盖透明窗体配置、可见尺寸、菜单、长内容、跨工作区草稿、重试、窗口过渡与拖动。语法和 diff 检查通过。
- 临时原生验证实例已全部退出；实际应用已更新重启为 PID 21140，保留原 userData 的位置和工作区身份。正式实例不启用测试底色或任务栏验证模式。

final result: passed

---

# 历史：2026-10-17 自查修复与原稿对照

本节取代下方历史阶段的结论。用户要求继续贴近提供的 ZIP / GIF / MP4，并自查缺陷；保留已确认的小尺寸：输入面板 288×109 DIP，机器人画布 96×120、原图显示槽 88×88。这里的 passed 是本轮功能回归和组件视觉验收通过，不是 27 张示意图与 58 秒视频逐帧、逐像素一致的声明。

## 本轮修复与证据

| 缺陷 | 原因及修复 | 验证 |
|---|---|---|
| 输入时缩略图反复重建、引用丢失键盘焦点 | 材料、版本引用、历史记录每次草稿更新都 replaceChildren；改为仅在对应内容变化时更新 | 原生 Electron 保留图片节点与引用/重试按钮焦点的断言，先失败后通过 |
| `@` 候选不能用键盘选择 | 搜索与正文缺少候选导航；补充方向键、Home/End，保留原生 Enter 按钮行为与 IME 保护 | 方向键焦点断言先失败后通过；浏览器实际 Down → Enter 加入引用 |
| 临时读取错误后，同工作区无法恢复 | 激活判断只看工作区 ID，没有区分是否成功加载 | 注入一次 503，再从机器人重新打开恢复；新增重新读取工作区入口；草稿保持 |
| 长记录滚动卷走发送区 | 输入表单和历史/结果使用同一个滚动容器 | 拆分固定输入区和反馈滚动区；20 条历史 + 20 行输入的可见性回归先失败后通过 |
| 多引用叠加候选列表挤掉发送区 | 固定弹层上限未预留输入区高度 | 限制引用列表高度并计算弹层剩余空间；32 个版本引用 + 两个候选的压力回归先失败后通过 |
| 系统重新定位后，机器人拖动跳回旧坐标 | 一直使用 ready 时缓存的位置 | 用本次指针的屏幕坐标和客户区坐标计算窗口原点；真实 Electron 输入事件回归先失败后通过 |
| 卡片之间的空白仍属于原生窗口的绘制/点击区域 | Acrylic 覆盖整个窗口背景，CSS 透明不等于原生窗口洞 | 增加仅面板可调用的有界 shape IPC；按可见圆角卡片设置原生区域；区域、圆角、越权及真实 Electron 调用检查通过 |

另修正了重复发送错误提示、迟到的其他工作区发送错误污染当前错误区、快速收起再打开时旧动画隐藏新窗口，以及启动时重复读取材料限制失败导致桥接入口未注册的问题。发送回执直接使用其 Loop 身份建立 Live，不再因一次额外读取失败把已受理输入显示成全局错误。重试仍保留原 submission_id，且不覆盖后续草稿。

## 视觉复核

- 原稿：ZIP 内 02-unbound、07-version-picker、08-version-detail、17-menu；通过 render.cjs 核对几何、颜色和 0.38 秒过渡。组件采用原始机器人、Noto Sans SC 和 Lucide 资产。
- 本轮全新 Electron 截图与尺寸 JSON：`C:/Users/brubing/.codex/visualizations/2026/10/17/01a14936-6b0f-76f3-bdad-6d9908be7a0e/repair-audit/`。包含空态、菜单、绑定前后材料、固定版本、失效、发送未确认和压力状态，共 13 张。均为生产 host / preload / renderer，后台为隔离 HTTP/SSE fixture；不是生产业务结果。
- 已打开同一输入中的 `source-comparison.png`，左为原稿组件按 80% 显示，右为本轮 Electron 截图按实际 1.5 DPR 归一；没有把实际界面拉伸到参考尺寸。空态、菜单、候选与详情分别比较同一种组件状态。动态工作区、代码正文、hash 与失效标记使用 fixture 数据，不伪装成原稿业务数据。
- 空面板 288px 宽，系统取整下约 109 DIP 高；菜单 152×107，候选 288×151，详情 286×262。对应原稿按 80% 的 288×109、151×104、288×149、286×262。输入纵向位置、顶部留白、菜单比例、详情标题和版本身份已重新校准。
- 字体/层级：保留套件字体；正文和可编辑区保持小尺寸下的可读性，不将所有辅助文字机械缩为 7px。文件标题显示 basename、完整路径保留在 title；hash 使用 8 位与已提交来源标签。
- 布局/动效：面板高度按原稿 380ms 三次缓出变化；关闭也使用 380ms。显示器变化会取消旧高度动画；尊重减少动态效果设置。候选位于面板上方，宽屏详情位于左侧，窄屏退回堆叠布局。
- 颜色/资产：沿用 #2b3749 / #68768a / #8994a5 / #547da8 和原始透明机器人；Diff 增删行有克制的红绿标记；菜单状态点随真实状态更新。
- 浏览器复核：实际用 Down / Enter 选择版本，打开详情并切换 Diff；保存 `browser-detail.png`。预览使用本地示例数据，未向用户工作区发送演示输入。

P0/P1/P2 本轮已发现项均修复。保留的 P3/平台差异：Windows 系统材质、字体栅格化、原生目录/退出/托盘样式无法保证与绘制视频逐像素一致；系统 DWM 合成效果不包含在 capturePage 截图中。原生区域的几何与调用已验证，桌面真实背景下的光学效果仍以运行版为准。图 27 是来源工作台示意，现有真实工作台继续按 Context/Revision 定位，不用示意数据替换业务内容。

## 验证与运行

- 本轮定向检查累计 29 项通过：宿主 11、原生页面 1、共享 intake / inputs 15、完整工作台浏览器/原生入口 2。覆盖失效引用、跨工作区草稿、错误恢复、同身份重试、长内容、快速开合、原生输入拖动和窗口区域。
- 工作台测试曾在调整窗口后立即读到旧视口，导致布局断言失败；补上视口到达和两帧布局等待后通过，未放宽布局断言。
- 修改脚本语法、git diff --check 通过；没有遗留 `[DEBUG-...]` 探针。
- 修改后的实际应用 PID 48528，加载本工作树，Gateway `http://127.0.0.1:53431/health` 返回 200，日志确认 CSS、字体与 renderer 请求成功。用户明确确认旧安装版没有草稿后，已关闭旧 PID 21756；当前只保留本次修复版，避免机器人重叠。

final result: passed

---

# 历史记录：会话 Patrol 小兵视觉验收

## 同一视觉比较输入

| 参考设计 | 真实 Electron 实现（1280×840） |
| --- | --- |
| ![冷脸萌小兵参考设计](C:/Users/brubing/Downloads/冷脸萌小兵.png) | ![会话 Patrol 小兵实现](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/add-session-patrol-avatar/qa/session-patrol-avatar-1280x840.png) |

## 核对结果

- 角色主体：白色圆润机体、深色面罩、青蓝眼睛、顶部晶体、悬浮光环和阴影均保留。
- 会话呈现：小兵位于会话画布而非右侧检查器；气泡朝向小兵，且不改变原会话布局。
- 交互状态：七个状态资源与五个移动资源使用统一比例、锚点和透明画布。
- P1 修复：重新计算品红色键透明度，移除绿色边缘；为状态气泡补充对话尾巴。
- P0：无。
- P1：无未解决项。
- P2：无未解决项。

## 常驻待命 Patrol 验收

| 确定性 Electron 状态 | 真实网页零 Agent 会话 |
| --- | --- |
| ![待命 Patrol Electron 实现](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/add-session-patrol-avatar/qa/standby-patrol-1280x840.png) | ![待命 Patrol 真实网页](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/add-session-patrol-avatar/qa/standby-patrol-web-actual.png) |

- 常驻：真实网页的当前任务没有已投放 PatrolAgent，画布仍呈现一个待命小兵。
- 语义：气泡明确显示“Patrol 小兵 / 待命 / 尚未布置任务 / 布置任务”，没有伪装成运行中 Agent。
- 布局：小兵和气泡位于会话画布，不占用右侧检查器，不阻断消息与 Composer。
- 视觉：继续使用既有白色机体、深色面罩、青蓝发光、顶部晶体、光环与阴影；P0/P1/P2 无未解决项。

final result: passed

---

# Patrol 工作区选择页设计验收（历史目录方案，已撤销）

本节保留修改记录。用户随后明确工作区必须来自电脑文件系统；该方案的数据源和下方旧验收结论已失效，以文末“文件系统选择修正”验收为准。

用户选择本轮显示的第 2 张设计，并明确要求直接改造现有代码、不新开 OpenSpec change。基线为 8bd8537；本次只改工作区选择入口及其壳层生命周期，复用原文件夹绑定、Patrol 输入和 Live，未增加后端机制或依赖。

## 视觉依据与证据

- source visual truth：C:/Users/brubing/.codex/generated_images/01a11eb8-bbed-77d0-a00a-35cdad56696a/exec-32a4bc06-7eae-4ecf-8568-128379a8930a.png。
- implementation screenshot：C:/Users/brubing/.codex/visualizations/2026/10/09/01a11eb8-bbed-77d0-a00a-35cdad56696a/comparison-final.jpg 的右侧实际页面。
- full-view comparison：上述 comparison-final.jpg 同时包含左侧源设计和右侧运行中的真实 index.html；不是将两张单独截图当作并排检查。
- source pixels：1487 × 1058。实际页面 CSS viewport：1487 × 1058，在固定尺寸 iframe 内加载生产页面；两侧统一以 50% 展示为 743.5 × 529，保持比例。组合截图像素为 2000 × 1125；浏览器截图密度曾随窗口变化，因此验收使用同时渲染的等尺寸源图与实际页面，不以未规范化的独立截图断言像素误差。
- state：桌面浅色、选择入口、Focus 选中；先展示设计中相同的项目名称，再追加重复名称和长路径，合计 112 个离线工作区，以检查列表滚动。
- preview：http://127.0.0.1:3817/；比较页 /qa.html。预览读取当前工作树的生产资源，只注入既有离线测试 fixture，不连接用户数据库。
- focused regions：组合视图中已检查搜索/项目行及右侧图标、名称、路径、进入按钮；这页无密集图表或自定义图像资产，主要文字和图标在该视图可辨识，无需另外生成局部比较图片。

## 比较历史与 findings

1. [P1，已修复] 刷新进入 Patrol 时，默认普通会话检查器仍出现。初次浏览器 AX/截图确认其占用右侧，与工作区详情并列；共享 renderInspector 改为依据当前入口决定实际显示，分隔条同步实际隐藏状态。最终组合截图中仅保留选定的双栏，真实 Electron 重载断言检查器和分隔条均隐藏。
2. [P2，已修复] 初次等画幅比较中，“进入 Patrol”比参考图明显偏窄，路径/说明层级偏弱。已将主操作设为至少 220 × 52，路径/说明/提示字号分别调整为 15/16/14，并将列表列宽设为 424、详情内边距收敛到同一布局。comparison-final.jpg 为修改后重新加载和比较的证据。
3. 无未解决 P0/P1/P2。原生标题栏窗口按钮属于 Electron 外壳；浏览器预览保留生产全局“新增任务”按钮，绑定操作放在选择页标题旁，未照搬生成图中改变全局按钮职责的细节。

## 五项必查视觉面

- 字体：沿用 Segoe UI / Microsoft YaHei UI，页标题 26、详情标题 36、项目名 15、次要路径 12；名称单行截断，右侧完整路径可换行，不把长路径与名称同等突出。
- 间距和布局：左侧搜索固定，项目列表独立滚动；右侧用一个分隔线和开放页面承载详情，图标、路径、说明、主操作有明确顺序。720px 窗口切换为上下布局，无横向溢出。
- 颜色：沿用宿主 surface/text/border/accent 语义令牌；白底控件、浅蓝选中、蓝色主操作。没有引入另一套颜色系统或卡片网格。
- 图像/图标：品牌使用现有 Focus 图片，文件夹、搜索、加号和箭头使用已有离线 Lucide 资产；未新增手绘 SVG、emoji 或模拟图片。
- 文案：选择、进入与执行启动的含义分开；仅首条输入启动工作，详情不虚构任务状态。项目名称/路径使用真实 API 字段与共享 HTML 转义。

## 交互和回归

改代码前调用 Context7 /mdn/content 核对原生滚动、grid minmax 与键盘焦点：[overflow](https://developer.mozilla.org/en-US/docs/Web/CSS/overflow)、[focus-visible](https://developer.mozilla.org/en-US/docs/Web/CSS/:focus-visible)。

- CUA 浏览器实际完成搜索 focusfront → 详情更新 → 点击进入 → 原统一输入页；单独新开生产预览页检查 console，无 error/warn。比较页 iframe 切换过程中出现过未定位的 MutationObserver 工具日志；独立页面复查及隔离 Electron 回归未复现，未将其掩盖成应用已修复的问题。
- Node/Electron 定向批次 85 项通过，其中完整真实页面回归覆盖 104 条长路径/同名项、原文转义、大小写和路径分隔符搜索、焦点与壳层重绘、空/失败列表、取消/失败/成功绑定、迟到响应、720px 布局、四类首输入、普通会话、独立观测、连续草稿与重载。
- JS 语法和 git diff --check 通过；未运行无关后端或全量测试。屏幕阅读器专项、正式数据库和外部 Provider 不属于本次前端验收。

## 清理与代码量

删除 app.js 内原始按钮堆砌、逐项绑定和重复列表读取；同职责只保留一个 workspace-patrol-picker 模块，app.js 只负责原生绑定与进入宿主。选择页搜索在壳层刷新时保留，显式切换/离开才解除监听并拒绝迟到响应。没有第二份输入/执行链。

相对 8bd8537：业务新增 272、删除 30、净增 242 行；测试新增 82、删除 7、净增 75 行。主要增长为必要的分栏布局、搜索/详情选择与定向 UI 回归；预览脚本和图片位于独立可视化目录，未混入业务代码量。

## Implementation checklist

- [x] 保留选定双栏结构、既有令牌和资产。
- [x] 搜索、同名识别、完整路径、显式进入和原生绑定可用。
- [x] 页面刷新/切换不污染普通会话，不被旧异步结果接管。
- [x] 删除旧列表实现、核对文件头、通过定向回归。
- [x] 修复 P1/P2 后重新进行同画幅视觉比较。

Follow-up polish：生成图与系统字体的抗锯齿、原生外壳高度存在少量预期差异，不影响内容层级和核心流程。

final result: passed

## 文件系统选择修正：当前验收

有效需求来源为用户最新明确指令“工作区是从电脑文件系统选”，及 C:/Users/brubing/AppData/Local/Temp/codex-clipboard-caacf9b7-9826-41a5-b6f4-d014e017b3e7.png（2159 × 1222）。该截图标示错误入口，不再按历史目录列表图稿实施。

[P1，已修复] 数据源错误：删除历史工作区列表、搜索、选中首项、全部目录查询 API/服务及对应夹具。页面复用 preload.selectWorkspace → focus:select-workspace → dialog.showOpenDialog(openDirectory)；浏览器兼容入口继续沿原 /workspaces/select。选择只保存本页返回路径，点击“进入 Patrol”才复用原 create_workspace 登记绑定，首条信息才启动执行。

改代码前已调用 Context7 /electron/electron，核对原生目录对话框返回的 canceled/filePaths 及现有 invoke/handle 桥：[Electron dialog](https://www.electronjs.org/docs/latest/api/dialog)。没有另建文件浏览器、数据模型或执行链。原导航偏好改为保存已确认工作区的 metadata，重载直接恢复该工作区；此前仅存数据库 UUID 的旧值不再触发历史目录选择。

当前实现截图：C:/Users/brubing/.codex/visualizations/2026/10/09/01a11eb8-bbed-77d0-a00a-35cdad56696a/filesystem-picker-selected.jpg，1600 × 900。全视图比较为同目录 filesystem-correction-comparison.jpg：左侧用户标示的错误页，右侧当前实际页面。源图等比显示为 800 × 453，实际页在 1600 × 900 iframe 中以 50% 显示为 800 × 450；两者比例未被拉伸。比较以已纠正的数据源和确认流程为准，不将来源错误的旧布局继续当作像素目标。

五项视觉面已复查：保留宿主 Segoe UI/微软雅黑字号层级、语义颜色和真实离线图标；删除列表后的页面只表达本次路径及两个明确操作；完整路径换行、720px 无横向溢出；文案说明系统选择、显式确认与首输入执行边界。主要控件和路径在全尺寸截图可读，无需另生成局部裁图。

验证：85 项 Node/Electron 定向回归及 4 项后端合同测试通过，相关 Python Ruff F、JS 语法与 git diff --check 通过。回归验证取消选择不登记、重选取消保留旧路径、重复选择不双开、绑定失败重试、迟到选择/绑定不接管已离开的页面、已确认工作区重载、四类型首次输入、普通会话和历史记录；旧历史列表 HTTP 请求次数必须为零。只替换了原错误搜索场景测试，必要执行与草稿回归保留。

CUA 实际检查空选择页 → 点击选择 → 精确路径确认，独立页面 console 无 error/warn。预览与自动测试在原生桥边界使用离线返回路径，没有操作用户真实系统对话框或正式目录；现有 Electron 生产 IPC 和 openDirectory 配置已核对。该限制不被表述为真实文件系统端到端测试。

相对 e538494：业务新增 67、删除 201、净减少 134 行；测试新增 59、删除 55、净增 4 行。增长为路径确认、原导航偏好恢复和异步边界断言，主要删除来自误加的历史列表/查询及其样式。无新 OpenSpec change、运行依赖、提交或推送。

Implementation checklist：原生目录来源、选择与登记分离、取消/异步收口、删除失效路径、定向测试、实际页面截图均完成。无未解决 P0/P1/P2；普通会话检查器和原输入机制保持其既有职责。

final result: passed

---

# Patrol 关系工作台：2026-10-10 本轮视觉核对

本节对应 `refactor-patrol-graph-workbench`。保留上文历史记录；本轮只评价新增关系工作台，既有原生 Acrylic 运行环境限制单列在实施记录中。

## 比较证据

源图：`openspec/changes/refactor-patrol-graph-workbench/references/target-workbench.png`，1748×904 px。

生产页面实际截图：`openspec/changes/refactor-patrol-graph-workbench/evidence/after/reference-1748.png`，1750×905 px。请求视口1748×904，Windows/Electron尺寸取整为1750×905 CSS px，JS devicePixelRatio=1；capturePage 原始2625×1358，使用 Electron nativeImage.resize 还原CSS密度。两个完整图像已在同一次工具结果中以 original 尺寸一起打开比较。

状态：详情展开、选中桌宠交互、进度/事实/来源面板可见；实际32节点属于隔离fixture，数据量和来源数不同于参考，未将9/14、阶段分区或示例进度写入生产。

| 参考 | 生产页面截图（隔离数据） |
|---|---|
| ![目标](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/refactor-patrol-graph-workbench/references/target-workbench.png) | ![实现](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/refactor-patrol-graph-workbench/evidence/after/reference-1748.png) |

几何：实际左栏231px、右栏280px、中央1159px；图区域718px高、输入89px高。参考左栏约226px、右栏约276px、输入约79px。顶部保留原生56px安全区及退出/工作控制，输入保留真实回执和历史，因此比参考略高。这是已确认业务/原生约束，不压小正文来完全贴合像素。

## 比较历史与修正

1. [P1，已修复] 原四卡布局把关系图限制在左上。改为中央连续图、两侧浮动读面及底部输入；右侧所选工作线接入原ContextInspector。
2. [P2，已修复] 第一版输入框过高、重点卡聚集，单节点靠左。收紧输入区、复用既有脑图标；重点卡在真实一跳范围内按几何间距筛选，坐标不随选择重排；单节点居中。
3. [P2，已修复] 中等窗口点节点没有切到所选工作线面，进度尾部和观察摘要挤压。900–1199px选择时显式切换右面板；增加进度占比、列表独立滚动、固定页脚，缩减观察重复文案。
4. [P1，已修复] 128节点下不相关事实也重新对账图/进度，导致秒级停顿。使用原投影结构共享判断更新区域；新增事实修订不触碰图DOM/进度焦点断言。修复后的同一压力用例见 performance-comparison.json。

修复后证据为上表最新截图，以及 after/ 下 empty、error、responsive-1440-1、responsive-900-1、responsive-390-1、responsive-1280-2、forced-colors、prefers-reduced-motion、prefers-reduced-transparency、global-approval 截图。控制台捕获无Uncaught错误。

## 五类视觉表面

- 字体与层级：沿用系统Segoe UI / 微软雅黑；面板标题14px、正文12–14px、重点节点17–20px、进度数44px，短元数据10px。已在原尺寸图中逐项核对左侧进度、选中节点、右侧来源和底部输入；没有用整页缩放掩盖比例问题。
- 间距与排版：中央大画布、轻量点标、最多5张重点卡、两侧独立滚动、细边和双层高光均落地。元数据更长时保留滚动与完整检查入口。图位置由真实拓扑决定，不硬编码成示例的每一个坐标。
- 颜色与材质：灰白连续底、低遮盖白色玻璃、灰蓝普通边/强调边、柔和阴影。去掉原白色大图卡与点阵；没有窗光图片或生成壁纸。普通浏览器回退已核对；原生后景透出不在本次视觉通过结论内。
- 图像/资产：参考主体为数据图与控件，没有新增装饰性位图。SVG用于真实可交互来源边，图标复用既有离线Lucide资产；未把界面烘焙成图片。
- 文案/内容：保留真实“已受理”“当前Live”“只读检查”及版本/权限说明。缺少权威阶段字段时不画假阶段；空数据不画虚构节点、百分比或运行状态。

局部核对使用同组原尺寸图中进度区域、选中节点和来源区域、composer区域逐项查看（源图约x4–231/y35–585、x783–987/y223–325、x1431–1708/y500–874、x269–1392/y788–868；实现对应区域见 geometry.json）。1×图已可清晰读取字号、来源版本与控件，不另做放大插值图作为“更清晰”证据。

## 状态与范围

真实指针/键盘验证关系线命中、重叠版本选择、拖拽、来源检查、面板切换和输入。390/900/1440/约1748、200%缩放下持久输入可见，无页面横向溢出。全局审批保留可达层级。

渲染层没有剩余P0/P1/P2视觉问题。P3差异为系统字体光学细节、原生安全区和业务文字导致的少量间距差异；没有伪造像素级一致或还原度百分比。

尚未完成的整体验收：Windows Acrylic 两个受控后景未出现预期变化，且未修改的基线在同一环境同样失败；真实Windows输入法候选窗未覆盖。OpenSpec 5.1/5.2 保留未勾选，不据此宣称整个change验收完成。

final result: passed

---

# 全图玻璃关系区：2026-10-11

本节对应 `refactor-global-map-glass-graph`，实施范围是用户标注的全图关系区域。红框外的导航、模式和业务入口保持原职责。

## 同尺寸比较

源图：`openspec/changes/refactor-global-map-glass-graph/references/target-graph.png`，1258×826 px。

结果：`openspec/changes/refactor-global-map-glass-graph/evidence/after/selected-reference.png`，1258×826 px。生产页面的 CSS 视口1990×1163，图区域1258×825.531，DPR=1；Electron capturePage 输出按 CSS 密度归一，未对布局做截图专用修改。通过实际窗口尺寸与“适应画布”得到比较区域。

状态：选中一条真实页面内的工作线，原检查器在画布旁。截图中的32节点为隔离视觉 fixture，选中的 R3、5来源/4去向与参考数据不同；真实 PostgreSQL 单节点检查和任务打开证据单独保存在 evidence/real/。图坐标来自既有拓扑布局，不把参考数据或阶段分组写进生产。

| 参考区域 | 实际区域（隔离数据） |
|---|---|
| ![参考](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/refactor-global-map-glass-graph/references/target-graph.png) | ![实现](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/refactor-global-map-glass-graph/evidence/after/selected-reference.png) |

两张原尺寸图片已在同一次工具结果中并列核对。完整页面证据是 after/reference-full.png 与 selected.png；额外核对了390、900、1440、200%、高对比及减少透明截图。

## 修正历史

1. [P1，已修复] 旧全图仍使用不透明大白卡、点阵及所有节点等重卡片。接入原 workbench 图呈现，保留唯一布局；使用连续灰白底、轻节点及有限重点卡。
2. [P1，已修复] 原固定检查抽屉覆盖右上“仅看所选关联”。同一检查器在全图宽屏占据旁栏，窄屏位于画布下方；真实鼠标能继续操作过滤/刷新。
3. [P2，已修复] 隐藏状态行让 Grid 自动排位错位，窄屏底部图例被裁切。显式指定 header/status/graph/footer 行，图层 min-height:0，缩放保持在图内；修复证据 responsive-390-1.png。
4. [采集差异，已排除] Electron 初始窗口受屏幕高度限制，造成首批截图缩小。setContentSize 获取实际视口并记录 geometry，再以1258×826区域比较；不把该差异当生产字号问题。截图等待有限动画完成，避免记录检查面过渡中半透明帧。

## 五类表面核对

- 字体：沿用系统 Segoe UI/微软雅黑；点节点13px、元信息10px、重点卡17px、选中标题20px。目标图的字形光学细节不是同字体来源，系统抗锯齿属于 P3；未整体压小界面以伪造匹配。
- 间距与布局：参考与结果相同区域尺寸。灰底贯通、重点卡圆角/双层亮边、细曲线、轻标签与图内缩放已呈现。实际拓扑和选择一跳关系决定卡片位置，结构差异是已确认数据约束；外层三种模式保留。
- 颜色：普通边灰蓝，选中直接边加深，卡片低遮盖白色渐层及柔和阴影；空状态和错误有明确文字。forced-colors 改用系统色和描边；减少透明使用原阅读底。没有新增窗光背景或原生材质机制。
- 资产：该参考区域为功能性图谱，没有待生成的照片/插画。沿用已有图标，SVG 仅呈现真实来源边和命中区域；未烘焙节点或用静态图充当交互。
- 内容：节点名、Revision、来源数、运行状态均来自 API/测试响应；“已提交来源”“只读检查”和显式任务打开保留。刷新受理不当作状态生效，缺少权威阶段字段时不画参考分区文字。

局部检查使用同组1×区域图：选中卡（参考约x574–795/y229–335，结果约x439–640/y304–400）、标签点/连线（参考上半区域，结果x95–420/y120–310）及底部图例/缩放。文字和边界在原尺寸可读，未用插值放大图片代替清晰度证据。

## 交互与限制

真实指针命中精确边、重叠版本分别检查、拖拽不误选；Enter 打开草稿/历史版本，Esc 关闭归焦；搜索和刷新保留视口；批选取消、删除409、确认级联及成功清空；0/1/32/128、失败/加载/恢复和越工作区响应隔离已通过。控制台捕获 errors=[]。

真实 PostgreSQL/HTTP 使用原 Bootstrap 发布版本，UI 检查同一 Revision，再显式打开对应任务；原任务材料、Run/SSE、记忆及独立会话继续通过。没有把多节点 fixture 截图当成真实业务完成证据。

本次关系区域无未解决 P0/P1/P2 视觉问题。已有 Windows Acrylic 后景变化限制、原生输入法候选窗未覆盖情况沿用上轮记录；本轮不宣称原生透明环境修复。

Implementation checklist：共享图/样式收敛、真实接口承接、旧入口保留、响应式/系统偏好、键鼠/错误恢复、同尺寸对照已完成。迁移清单、逐项证据与代码增减见本 change 的 implementation.md。

final result: passed

---

# Desktop Patrol UI 验收

final result: passed

## 目标与范围

以用户第二张截图为视觉方向，修改现有桌面输入面板及机器人下方提示。复用原有机器人图片、材料上传、工作区绑定、输入历史和 Live 回答流程。

参考图为 1034 × 320 px。面板区域裁取 (40, 20)–(830, 300)，归一化到 840 × 303 px，与 Electron 空面板截图直接并排比较。原生窗口为 560 × 202 CSS px，截图密度为 1.5。预览页只用于展示布局，原生文件夹选择及发送由 Electron 测试验证。

## 检查记录

- 首轮 P2：新增图标的 CSS 变量在共享图标样式表中解析相对路径，导致箭头、下拉和更多图标缺失。已将资源定义移入共享 icons.css，并重新截图确认全部显示。
- 复查：面板比例、单层圆角玻璃表面、顶部工作区入口、输入占位、圆形添加及发送按钮符合参考方向；菜单完整可见，材料、错误及回答出现时窗口有界展开。
- 图标、文字和按钮在整张对照图中可清楚辨认，无须额外局部放大。机器人保留现有资产，默认隐藏底部提示，悬停、键盘聚焦或工作状态时显示。
- 400 px 浏览器窗口下无横向溢出。浏览器请求 320 px 时实际最小视口为 400 px，未声称验证更小尺寸。
- 浏览器验证了输入后发送启用、清空后禁用、菜单打开及 Escape 关闭，无控制台错误。
- 24 项 Node/Electron 检查通过：原生窗口边界及 resize IPC 鉴权、拖放、截图粘贴、明确发送、Ctrl+Enter/IME、跨工作区草稿、隐藏恢复、受理回执、已提交回答。图标修正后重新运行 Electron UI 验收通过。

## 证据

- [归一化参考对照](C:/Users/brubing/.codex/visualizations/2026/10/17/01a148eb-f3d2-7aa0-aa6d-2204bc8ba7b5/patrol-ui/reference-comparison.png)
- [原生空面板](C:/Users/brubing/.codex/visualizations/2026/10/17/01a148eb-f3d2-7aa0-aa6d-2204bc8ba7b5/patrol-ui/desktop-empty.png)
- [操作菜单](C:/Users/brubing/.codex/visualizations/2026/10/17/01a148eb-f3d2-7aa0-aa6d-2204bc8ba7b5/patrol-ui/desktop-menu.png)
- [回答展开](C:/Users/brubing/.codex/visualizations/2026/10/17/01a148eb-f3d2-7aa0-aa6d-2204bc8ba7b5/patrol-ui/desktop-committed-answer.png)

## 剩余差异

P3：玻璃折射层次会随桌面背景、Windows 版本及合成设置变化。已配置系统 Acrylic 和 CSS 半透明表面；本次隔离 Electron 测试禁用 GPU，验证了布局与交互，未验证真实桌面背景下的动态模糊效果。

---

# 2026-10-17：按用户桌面 UI 套件还原（本轮有效验收）

本节以用户本轮提供的 focus-desktop-ui-kit.zip、focus-desktop-demo.mp4、focus-desktop-demo.gif 为依据，覆盖之前 560×202 的大面板方案。演示里的编辑器属于桌面背景，未把它新增为 Focus 内的编辑器页面。附件中的说明和示例作为设计材料，未作为执行命令、真实任务结果或测试证据。

## 视觉依据与证据

- source visual truth：C:/Users/brubing/Downloads/focus-desktop-ui-kit.zip；解压资料位于 C:/Users/brubing/AppData/Local/Temp/focus-ui-reference-20261017/focus-desktop-ui-kit/。主要核对 ui/02-unbound.png、04-text-reference.png、07-version-picker.png、08-version-detail.png、09-ready.png、13-history.png、16-results.png、17-menu.png、21-reference-boundaries.png；同时读取 27 状态清单、绘图尺寸及 58 秒时间线。
- 用户 MP4 与 ZIP 中 MP4 的 SHA256 相同。已抽取时间联系图：C:/Users/brubing/.codex/visualizations/2026/10/17/01a14936-6b0f-76f3-bdad-6d9908be7a0e/patrol-ui/video-contact.png。
- implementation screenshot / full-view comparison：同目录 browser-empty-comparison.png、browser-material-comparison.png。两张图都在同一浏览器截图内包含左侧原稿和右侧生产 desktop-patrol.html，而非分别查看后凭记忆判断。
- focused region comparison：同目录 comparison-final.png（776×188），来自上述浏览器全视图的原稿和实际空面板等尺寸裁切；菜单、版本详情、历史另以原生截图核对。
- viewport：浏览器 1389×900 CSS px，截图 1389×900 px，deviceScaleFactor 1。资料原图 1440×900 px，空面板源区域 (956,612)–(1316,748)，360×136 px；对照页以原尺寸显示该区域，未拉伸截图。
- state：浅色、未绑定、空输入、发送禁用；材料对照为 Focus 已绑定、单项参考文字、空指令。材料示例内容属于隔离夹具。
- 原生 Electron 运行在 Windows 150% 缩放下。空面板请求尺寸为 360×136 DIP；原生边界可能取整到 137 DIP，截图约 540×205 px。源码级几何断言和渲染逻辑分别核对，不把 DPI 取整当作额外内容高度。
- 其他原生截图：同目录 desktop-menu.png、desktop-bound-preview.png、desktop-committed-answer.png、desktop-version-picker.png、desktop-version-detail.png、desktop-file-reference.png、desktop-version-unavailable.png。
- 可交互隔离预览：http://127.0.0.1:3818/。该页读取当前工作树的生产 JS/CSS；绑定、Live、版本和发送使用明确标注的本地夹具，不连接用户数据库或模型。

## 比较历史与修复

1. [P1，已修复] 默认面板仍为 560×202，字号、按钮与留白均大于套件。改为 360×136，移除顶栏分隔线，使用 40px 顶栏、15px 输入、28px 发送按钮和紧凑工具栏；机器人改为 128px 原图画布，对应约 70px 可见主体。原生窗口保留显示器边界约束。
2. [P2，已修复] 初轮比较底部工具栏比源稿偏上约 5px，参考文字仍显示技术文件名和 KB 信息。调整工具栏和底部间距；只在桌面 compact 模式显示“参考文字”、内容摘要和图片类型，普通工作台材料卡保留原呈现。最终 browser-empty-comparison.png / browser-material-comparison.png 为修复后的重新截图。
3. [P1，已修复] 桌面没有文件版本引用完整路径。复用既有 facts / factDetail API、inputFileReference 和 file_refs store，补上搜索、读取中、空候选、读取重试、固定版本筹码、详情、移除及分页；详情始终按原 Fact Revision 查询并核对 result_key/hash。失效保留原要求和引用；切换工作区中止读取并拒绝迟到候选。
4. [P2，已修复] 版本详情初轮纵向堆叠并出现不必要的纵向滚动。宽屏按源稿在紧凑面板左侧呈现 358px 详情，切为内容 / Diff / 写入前内容三个键盘可用标签；收紧段落与标签间距。空间不足时回退上方展开，主体仍保持 360px。
5. [P1，已修复] 工作台原生交接配置未启用，固定版本按钮无法到达唯一机器人草稿。宿主挂载 onCompose，传递有界、按工作区限定的 file_refs；工作台不再持有第二份可编辑输入。原生工作台页面测试真实触发版本按钮，断言只打开同一机器人并保留 Fact / Revision / result_key / slot / path / hash。
6. [P2，已修复] 发送未确认时重试记录被折叠隐藏。首次失败自动展开记录，后续编辑不会反复强制展开；引用本身也参与退出时的未发送提醒。
7. 开合增加 380ms 展开与 200ms 收起过渡，尊重 reduced-motion。重新唤起会取消旧的收起动作，隐藏后草稿仍由原 store 持有。
8. [P2，已修复] 最终原生截图发现空面板的菜单底部状态行被裁切。菜单打开时让玻璃壳层占满已申请的展开高度，并加入菜单 bottom 不超过 innerHeight 的真实页面断言。定向回归重新通过；最新 desktop-menu.png 中三项操作、Esc 和底部状态行完整可见。

## 五项视觉面核对

- 字体：直接采用套件 NotoSansSC-Regular.ttf，随附 OFL 和来源信息；绑定 12px、正文 15px/22px、引用标题 10px、次要文字 9px、状态 11px。名称截断、正文换行和长文件路径均保留；系统抗锯齿与 Canvas 原稿存在 P3 差异。
- 间距与布局：360×136 空面板、18px 圆角、单层玻璃表面、无顶栏横线；材料为 43px 紧凑条目。记录和结果按需增加高度，宿主上限 620px 并约束显示器边界；详情打开后输入主体仍为 360px。
- 颜色与令牌：源稿 ink #2b3749、secondary #68768a、muted #8994a5、accent #547da8；透明冷白/蓝表面、白色边缘、蓝灰按钮；工作完成与异常使用克制的绿/琥珀提示。
- 图像质量：机器人与套件素材 SHA256 一致，继续使用原始透明 PNG；文件、文件夹、搜索、关闭、添加、发送使用现有离线 Lucide 图标。文字参考以同套文件图标标识，和稿中的排版引号有 P3 细节差异。图片缩略图读取真实附件字节，不以示例图片替换用户材料。
- 文案与数据：参考材料与要求分开；发送中、已受理和工作完成来自原有 store / Live 状态。演示中的文件名、耗时、进度和完成内容未写入生产数据或硬编码为成功反馈。工作区仍调用系统目录选择器。

## 交互和回归

- Node / Electron 共 27 项定向检查通过，覆盖宿主 IPC 归属及边界、拖放/截图预览、明确发送、IME / Ctrl+Enter、工作区草稿隔离、收起后恢复、受理和完成分离、固定回答来源、版本搜索与标签键盘操作、按原身份提交 file_refs、版本失效保留及迟到请求隔离。
- 新增收起过渡结束后原生窗口确实隐藏的断言单独重跑通过；不是仅检查动画 class。
- 普通浏览器工作台与原生工作台的真实页面回归均通过。前者保留已有输入流程，后者核对单一机器人交接，不模拟一个额外可发送草稿。
- CUA 浏览器实际完成输入、版本筛选、加入引用、打开详情和切换 Diff、明确发送、受理/完成、展开历史并继续编辑；输入区未被记录锁住。
- 已检查浏览器日志。IAB 对照页记录了一条没有来源 URL 的 MutationObserver.observe 日志；本页面及所载生产脚本未使用 MutationObserver，隔离 Electron 的应用 console/preload 错误列表为空。该工具侧疑似注入日志保留为核验记录，未声称浏览器日志完全为零。
- JS 语法与 git diff --check 通过。Electron API 行为通过 Context7 官方文档核对：https://www.electronjs.org/docs/latest/api/browser-window 。
- 未运行真实模型任务，也未声称验证真实 Explorer 拖放、系统输入法或实际授权执行；本轮修改的是桌面 UI 与已有输入交接。

## Findings / Follow-up polish

无未解决的 P0/P1/P2。P3：Windows Acrylic 的折射与背景模糊受系统合成、减少透明设置及显卡状态影响；隔离原生截图禁用 GPU，不代表视频中的完整光学效果。保留原机器人各状态 PNG，未宣称新增角色骨骼动画。真实数据的标题、hash、回答长度及缺失 Diff 按服务端事实显示，不照抄演示内容。

## Implementation checklist

- [x] 使用用户指定的紧凑尺寸、字体、配色和原始角色素材。
- [x] 原稿与实际界面同画幅比较，修正 P1/P2 后重新截图。
- [x] 参考材料、文件版本和指令分开呈现，版本身份保持固定。
- [x] 工作台回到唯一机器人草稿，读操作不会触发发送。
- [x] 状态、记录、结果、来源与既有后端语义一致。
- [x] 定向原生回归、浏览器交互、语法和 diff 检查完成。

final result: passed

---

# 再复核与缩小版（2026-10-17，本轮有效结果）

**原稿完整还原结论：尚未达到完全、逐帧复刻。** 上一节的 passed 指当轮实现的界面与功能检查通过，不代表 27 张状态稿及 58 秒视频全部一致。本轮重新读取用户套件总览、尺寸与当前生产页面，重新截图，未将旧截图当成本轮审查证据。

用户新要求是机器人和输入框更小、更安静。本轮明确以此覆盖原稿的绝对尺寸，保留配色、字体、素材和交互层级；没有继续将 360×136 DIP 当作用户想要的桌面占位。Windows 150% 缩放下，旧逻辑尺寸会占约 540×204 个屏幕像素，这也是原先观感偏大的原因之一。

## 重新取证与尺寸

- source visual truth：用户 ZIP 内 ui/02-unbound.png、04-text-reference.png、08-version-detail.png 以及 overview-02.png / overview-03.png；源图 1440×900，空面板 360×136，机器人原始显示画布 128×128，可见主体约 70px。
- 本轮 evidence：C:/Users/brubing/.codex/visualizations/2026/10/17/01a14936-6b0f-76f3-bdad-6d9908be7a0e/compact-audit/。
- 01-before.png 为本轮修改前的浏览器截图，779×1483；02-compact-empty.png、03-compact-reference.png、04-compact-detail.png 为修改后生产 renderer 与源稿的同页截图。浏览器面板捕获尺寸有宿主缩放变化，分别为 1623×3573、779×1483、771×1468，未据此声称 CSS 像素误差。
- *-focused.png 为上述相同截图的顶部裁切，移除空白桌面，未替换实际界面。已打开检查引用卡截图。
- 原生 Electron 截图 desktop-empty.png 为 432×165 px，Windows deviceScaleFactor 1.5，对应 288px 宽、约 109–110 DIP 高；宿主请求高度 109 DIP，原生边界容许一个 DIP 的系统取整。desktop-robot.png 为 147×180，机器人 CSS 画布 96×120，其中原图显示槽为 88×88，可见主体约 48 DIP。
- 同一比较输入 compact-comparison.png（752×342）：左为用户提供的源面板 360×136 与 128px 机器人素材，右为本轮原生截图按 1.5 密度归一化为 288×109 与 96×120。保留大小差异用于解释用户要求；不是把两者拉成相同大小后宣称尺寸一致。
- 缩小后的空面板面积减少约 36%；机器人原图显示槽 128→88，可见主体约 70→48，减少约 31%。输入字体 15→13px，正文保留至少 11px、辅助字 9px，不通过整页缩放压小所有文字。

## 复核发现与本轮修正

1. [已修复] 绝对尺寸偏大：原生空面板改为 288×109、机器人窗口 96×120；标题、输入、工具栏、材料卡、圆角和弹层随之收紧。原生邻接间隙 8→6px，展开高度上限 620→500px，版本详情总宽上限 738→590px。
2. [已修复] 文字引用使用普通文件图标。桌面文字卡改用同一 Lucide 图标族的 quote 标识，区分文字参考和文件版本；保留真实材料名称及字节。图标来自官方 https://github.com/lucide-icons/lucide/blob/main/icons/quote.svg，遵循本地同族许可。
3. [已修复] 已提交结果缺少原稿中的“工作区更新”标题层级。仅实际存在回答、进度或产物时显示该标题，Context 标题降为次级，未写入演示结果。
4. [已修复] 更窄尺寸下较长状态可能挤压控件。状态文字单行省略并提供完整 title，重连入口保持可用。菜单、引用卡、侧向详情和纵向滚动经本轮原生截图检查，不存在操作区被窗口裁切的遗留问题。

## 仍与演示不同的部分

- 材质：沿用 Windows Acrylic 与 CSS 半透明表面，不能保证任意系统背景下得到视频中绘制的相同折射、反光和光学效果。
- 动效：有开合淡入/淡出、加载旋转及状态 PNG 切换；没有视频中全部连续高度插值与角色过渡，不能称为 58 秒逐帧复刻。本轮未为了补充动画而增加持续漂浮、闪烁等桌面干扰。
- 展开方式：空面板更多菜单会为避免原生窗口裁切而申请额外高度；窄屏版本详情移到上方，和宽屏示例的绝对坐标不同。
- 来源工作台：保留现有真实工作台和 Context/Revision 定位；图 27 的示意图布局没有替换整套工作台。系统目录选择、退出确认和托盘保留 Windows 原生实现，外观不等于绘制稿。
- 最新缩小尺寸是用户本轮明确要求的设计调整，字体并未机械缩到原稿的 80%，以保持输入及结果可读。因此不再把它表述为“所有像素都和原稿相同”。

## 五项视觉面与交互

- 字体：继续使用套件 Noto Sans SC；输入 13/18px，绑定 11px，结果 11/18px，辅助文字 9px；长路径换行或截断，版本 hash 和移除入口保留。
- 布局：同一玻璃表面、安静顶栏与底部工具栏；材料卡 43→36px、发送按钮 28→24px，详情左侧宽 286px，窄屏回退上方；原图宽高比不变。
- 颜色：保留 #2b3749 / #68768a / #8994a5 / #547da8 与冷白透明表面，未用高饱和或大阴影补偿尺寸变化。
- 图片：直接缩放原有透明机器人，未重绘或裁切机体；附件缩略图仍来自用户材料。quote 为官方图标资产。
- 内容：指令、材料和固定文件版本保持区分；标题、状态和结果来自真实 store / Live，未把设计场景当作实际运行结果。
- 本轮 20 项 Node/Electron 定向检查通过，覆盖尺寸及 IPC 边界、菜单不裁切、预览、IME / Ctrl+Enter、跨工作区草稿、开合、固定版本、版本失效及迟到响应；共享材料标识调整后重新跑过原生页面。
- CUA 本轮实际打开版本候选、选入引用、查看详情并切换 Diff。源稿和实际组件在同一捕获中比较；未用构建成功代替视觉验证。
- git diff --check 与修改脚本语法检查通过。
- 用户确认没有未发送草稿后，已重启此次修改版，实际 Electron PID 66564，Gateway http://127.0.0.1:54934/health 返回 200，日志确认加载本工作树的 CSS、字体和 renderer；旧安装版仍独立运行，没有结束其进程。

本轮缩小与功能验证完成；以上原稿差异作为明确限制保留。通过的是缩小版的有界布局与交互验证，不是全套原稿逐帧复刻的声明。

final result: passed
