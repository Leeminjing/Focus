# 会话 Patrol 小兵视觉验收

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
