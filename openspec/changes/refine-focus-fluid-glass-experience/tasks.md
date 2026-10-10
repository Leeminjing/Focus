## 1. 审批与实施基线

- [x] 1.1 确认用户明确批准 `refine-focus-fluid-glass-experience` 后再进入 apply；验证记录指向本 change 的批准消息，未批准时不得修改产品、依赖或构建配置。
- [x] 1.2 重新核对分支、HEAD、remote 与工作区，验证既有用户修改被保留且产品基线变化已记录；复核 design 的 Context7 查询对实际 Electron／Chromium 适用，必要查询缺失或失败时停止，不以其他资料替代。
- [ ] 1.3 按 design D8 在相同 Windows 机器、窗口、缩放、数据与电源条件下补齐正常硬件渲染的前置基线；验证 trace、GPU/viewport、采样规模、30次操作×3组及可比较数据记录齐全，software probe 不冒充硬件基线。

## 2. 核心材质与可见性原语

- [x] 2.1 在原 tokens/base/components 收敛六角色色、材料角色和控件状态，保留插件公开 aliases；验证实际合成文字／边界对比度、computed style 来源以及没有尾部追加覆盖或新布局 !important。
- [ ] 2.2 实现导航和 Patrol composer 的单层控制材质，保留内容实色与原生输入；用真实 quiet/details 页面截图验证背景参与、顺形明暗边缘、厚度、高光和 press/focus 响应，若只是磨砂卡片不得勾选完成。
- [x] 2.3 仅在既有工具不足以承接时按 design D6 实现窄职责 surface visibility/cancel helper并接入 index；通过 native DOM 用例验证反向、cancel、finish generation、hidden/inert、卸载cleanup及减少动画，不持有业务身份、不新增控制器／依赖。
- [x] 2.4 将正常／hover／focus-visible／press／selected／disabled／pending 的反馈统一到原控件规则，替换机械全局 button scale；用真实 pointer／keyboard 操作及静止trace验证局部反馈即时且无装饰循环，状态不只依赖动画。

## 3. 第一闭环：Patrol 安静输入与工作详情

- [x] 3.1 在原 controller 的 toggleDetails 与 view 语义中建立保留输入节点的展开收回，保留 detailsOpen 单一显示意图；验证同一 form/textarea、草稿、选区、IME、必要滚动、aria 和最终 hidden/inert，无输入文字缩放。
- [x] 3.2 把中途反向、resize、偏好改变和 leave/dispose 收敛到最后意图，保留原 AbortController／generation；在40/80/120ms反向和跨workspace切换中验证无起点闪回、排队动画、旧回调或透明拦截层。
- [x] 3.3 保持按需加载和当前单路Live，展开只读取原 observation／lineage，事件只增量更新；针对真实 HTTP/SSE 和既有 Node 用例验证 quiet 连续输入、不自动展开、accepted≠effective、失败正文及稳定 submission/request identity。
- [ ] 3.4 核心闭环采用正常、reduced-motion、opaque/contrast三种模式实际操作并采样；查看同窗口数据下的静态图与真实时刻连续帧，按三份 spec 验证材质／连续性／响应并分别记录 verdict，未通过不开始全页推广。

## 4. 第一闭环：Context 与临时检查面

- [x] 4.1 在现有 Context inspector 的 select/render/close/dispose 接入可逆右侧表面转换，对象切换保留壳层；验证 A→B乱序、关闭中重开、会话分页／阅读锚点、旧响应失效、Esc与触发焦点返回。
- [x] 4.2 调整现有 showDialog/close/cancel/movedPanel 与 picker 生命周期，让来源关系和退出完整；验证 native top layer、模态解除、输入／等待草稿节点回原位置、切换检查种类不重播旧入场、系统chooser取消／迟到不登记执行。
- [x] 4.3 为真实请求补足原控制器的局部pending／error反馈，保留业务状态来源；用延迟／失败注入验证展开及时、持续输入可用、pause/grant等不提前显示生效，close与动画取消不触发任何控制命令。
- [x] 4.4 走真实“quiet→details→Context drawer→close→quiet”闭环并检查后再次提交；验证检查身份与发送workspace分离、同一输入节点／draft／selection、反向和最终布局；保存并实际查看该闭环的录屏或连续帧。

## 5. 共用呈现推广与保留范围

- [x] 5.1 仅在第3–4组闭环证据通过后推广到任务三栏／材料检查器／全图工具条和现有通用浮层；验证原 TaskRunOperations、conversation reconciler、图 layout cache／reconcile、zoom/scroll与明确任务打开入口仍承接业务，无新图引擎或状态源。
- [x] 5.2 按 design D7 逐项核对六导航、设置、插件、独立会话、记忆与高级能力；验证各入口可达、原API／owner／权限／草稿归属保留，非关键外部Provider未操作范围明确标记，不用fixture冒充实际执行完成。
- [x] 5.3 核对并删除本次替代的 dialog/backdrop/inspector 入场keyframes、重复显隐与冲突视觉声明；用 callers/selectors/imports及相关测试证明替代完整，公开插件token／legacy Live／双owner等必要兼容按设计保留。
- [x] 5.4 保留当前关闭的Session Patrol头像功能，核对本次实际挂载路径；若开启时有无意义装饰循环，改成静态真实状态与交互反馈并验证原拖拽／键盘入口，不扩改splash或头像业务。
- [x] 5.5 检查每个新增／实质修改文件头，验证其公开能力、输入含义、输出、工作流和真实例子与最终实现一致；helpers保持内部职责，无大类／空转包装／碎片函数，必要许可和安全注释保留。

## 6. 定向验证与独立证据

- [x] 6.1 优先运行受影响 Node 用例：workspace-patrol-page、context-inspector、portfolio map、live reducer/store、composer draft、access、相关浮层与架构guard；验证预期业务断言保留，旧视觉断言按批准设计替换。扩大测试须记录具体影响原因。
- [x] 6.2 运行现有真实 Patrol 与传统任务UI关键用例及必要查询回归，只用新隔离PostgreSQL和临时磁盘；验证真实 intake、SSE、固定revision、pause、任务Run／材料闭环，保存失败／重跑记录，不操作用户业务数据。
- [x] 6.3 进行键盘／焦点／选区／IME、900/640/390 CSS宽度、200%zoom、减少动画、透明不可用及forced-colors检查；验证隐藏内容退出Tab／命中，原信息顺序和输入可达，媒体查询模拟与真实Windows偏好映射分别报告。
- [x] 6.4 对核心闭环、快速反向、对象切换、加载失败、Live更新提供同数据前后图、真实录屏或连续帧；实际查看并记录各视觉／动态要求的通过或未验证，不用生成效果图或终态拼图证明过渡。
- [ ] 6.5 按design D8复测本地反馈、32/128 Context压力、100事件/5s、长会话滚动和静止／50次开关，分析Script/Style/Layout/Paint/Composite及longtask；验证已批阈值和无新持续循环，不能从CSS时长或rAF proxy宣布Fast通过。

## 7. 交付与停止边界

- [x] 7.1 对照design D7迁移表，分别交付业务、材质、动态、性能与可访问性验收结果及完成／未完成／未验证清单；验证没有为了通过而降低已批标准或新增待优化需求。
- [x] 7.2 分别报告业务与测试新增、删除、净增行数，排除规划／生成／第三方／既有修改；验证主要增长与必要helper／测试能力对应、旧代码删除有caller证据、兼容有保留原因和退出条件。
- [x] 7.3 完成相关JS语法、git diff --check和OpenSpec严格校验；验证产品／依赖／构建无超范围修改，用户未提交内容保留；交付不自动部署、重启安装版或归档旧change。

实施结果与保留未验收项见 [verification.md](verification.md)。1.3 尚未覆盖所有操作族／完整采样条件；2.2 与3.4的严格材质效果待用户审阅；6.5完整D8未通过，未降低标准或归档。
