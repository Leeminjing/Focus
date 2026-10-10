## Purpose

让 Focus 的持续输入、工作详情和 Context 检查在现有真实界面中形成可追踪、可反向的空间连续性，保留输入节点、草稿、阅读位置和检查身份；动画只负责呈现用户最后的合法显示意图，不代替权限、请求或执行状态。

## ADDED Requirements

### Requirement: Patrol retains its quiet unified entry
The frontend SHALL 保留 Patrol、任务、全图、插件、无工作区模式、记忆库及已有设置入口。Patrol 未展开时主内容 SHALL 仅呈现输入区；工作区选择、输入类型、回执、待处理及详情入口 SHALL 位于该输入区。提交输入 MUST NOT 自动展开详情或铺开聊天记录。

#### Scenario: User submits successive thoughts
- **WHEN** 用户在安静态连续表达信息、目标、边界或完成检查
- **THEN** 每条输入使用原工作区受理身份，输入区显示对应发送／受理／失败回执，主内容仍保持安静输入态，没有自动展开或虚构工作进度

### Requirement: The same input surface connects quiet and detailed states
The frontend SHALL 在用户显式展开／收回时保留同一个输入控件，通过位置、形状及工作面的连续转换连接两种布局。详情 SHALL 保留左上下文关系图、右已提交进度与冻结依据、下方执行事实及持续可用的统一输入。

#### Scenario: User opens details with an unsent draft
- **WHEN** 输入中有未发送正文、选区或正在进行的输入法组合，用户显式展开详情
- **THEN** 输入面可追踪地移到详情布局，正文、选区和组合状态不因动画重建，输入持续可用，网络加载不阻止本地展开

#### Scenario: User returns from details to quiet input
- **WHEN** 用户显式收回工作详情
- **THEN** 同一个输入面回到安静布局，退出内容沿相应空间关系回收，图缩放／阅读状态保留供再次检查，终态详情退出布局和键盘访问

### Requirement: Interruptions follow the latest intent
The frontend SHALL 接受连续点击、反向操作与切换检查对象；转换 SHALL 从当前视觉状态接续，MUST NOT 排队播放过时动画、闪回起点或让旧 completion 隐藏新界面。离开页面 SHALL 清理退出效果和监听，不留下透明拦截层。

#### Scenario: User reverses before expansion completes
- **WHEN** 用户在展开尚未完成时收回，随后又展开
- **THEN** 可见表面从当前进度改变方向，最后一次意图决定终态，没有重复输入节点、残留覆盖层或必须等待的动画锁

#### Scenario: User leaves the workspace during exit
- **WHEN** 抽屉或详情退场期间用户切换工作区或页面
- **THEN** 当前页的过渡、监听与读请求被清理，旧回调不能写新页面、恢复旧焦点或挡住新输入

### Requirement: Context inspection is nonmodal and identity safe
The frontend SHALL 保留 Context 的概要、会话、来源与版本检查；检查对象 SHALL 与 Patrol 发送目标分离，只有明确“在任务中打开”才进入任务模式。对象切换 SHALL 保留检查壳层并即时呈现目标／加载，旧对象迟到结果 MUST NOT 覆盖当前对象。

#### Scenario: User inspects Context B while Context A is loading
- **WHEN** 用户在 A 读取未完成时选择 B
- **THEN** 壳层连续保留、B 的检查身份与加载反馈立即可见，A 的响应不会进入 B，后续 Patrol 输入仍提交当前工作区

#### Scenario: User closes the Context drawer
- **WHEN** 用户用关闭按钮或 Esc 关闭非模态检查
- **THEN** 抽屉沿入场关系退出，退出即不接受旧读取；完成后不参与布局、命中或 Tab 顺序，焦点返回合法来源或本页明确回退位置，不改变执行和发送目标

#### Scenario: User opens the inspected Context as a task
- **WHEN** 用户明确选择“在任务中打开”
- **THEN** 原任务入口接管指定工作线，任务草稿、材料、发送身份和运行状态保持其原归属，不和 Patrol 输入混用

### Requirement: Temporary surfaces keep their source and dismissal semantics
The frontend SHALL 让菜单、对话框和检查浮层的出现／消失与来源位置相符，保留原模态或非模态语义。关闭按钮、Esc 和程序退出 SHALL 使用一致的生命周期；更换内容不得每次重播通用入场，持久草稿不得因关闭而丢失。

#### Scenario: User opens and dismisses input history
- **WHEN** 用户从输入区打开输入记录并在入场途中关闭
- **THEN** 浮层从当前视觉状态返回来源方向，终态正确解除模态并返回焦点，输入记录与新草稿仍由原 store 保留

#### Scenario: User switches inspection type inside the dialog
- **WHEN** 用户从冻结检查转到其前序进度或事实详情
- **THEN** 现有浮层壳连续存在，内容保持明确的冻结或事实身份，原表单节点不会被复制，旧退出动作不关闭新检查

### Requirement: Reading and focus survive visual updates
The frontend SHALL 在实时更新、会话分页、展开收回和检查器内容更新中保持合法焦点、选区、阅读锚点及图谱稳定布局。视觉更新 MUST NOT 自动夺取输入焦点、让未变图谱或整页重新入场。

#### Scenario: Live events arrive while the user edits
- **WHEN** 用户在输入区打字或正在阅读事实，多个真实事件到达
- **THEN** 内容按既有身份增量更新，输入／选区与未变图坐标保持，更新不触发展开详情、全页入场或焦点跳转

#### Scenario: User loads older conversation messages
- **WHEN** 用户在 Context 会话检查中读取更早消息
- **THEN** 当前 revision 保持固定，原阅读锚点和合法焦点保留，分页不会被呈现为切换到另一个 Context

### Requirement: Existing capabilities survive adaptation
The frontend SHALL 保留设计迁移表中所有既有业务能力、高级入口、真实接口、权限和生命周期；窄窗只调整布局，不减少能力或改变信息归属。拟删除业务能力 MUST 另经用户明确确认。

#### Scenario: User resizes or zooms the desktop workbench
- **WHEN** 窗口变窄或使用既有200%整页缩放
- **THEN** 图、进度、冻结、事实和输入仍按原归属可达；导航、设置、任务三栏／检查器与高级能力保留键盘路径，隐藏区域不残留可聚焦控件
