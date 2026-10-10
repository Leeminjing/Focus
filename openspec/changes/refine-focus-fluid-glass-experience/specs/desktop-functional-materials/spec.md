## Purpose

为 Focus Windows 桌面工作台建立清楚的内容层与浮动控制层，使控件通过背景参与、边缘厚度、光感和交互响应表达层次；在正常与降级环境中保持正文清晰、操作可见和业务信息完整，不把普通磨砂或窗口材质等同于本能力。

## ADDED Requirements

### Requirement: Material follows functional hierarchy
The frontend SHALL 将玻璃限定于适合的导航、输入外壳、浮动操作条与浮层控制；会话、图节点、Task Progress、Observation、事实表格及长内容 SHALL 优先保持清晰稳定的内容表面。系统 MUST NOT 在同一视觉层叠加多重玻璃，或添加无业务用途的彩色背景来展示透光。

#### Scenario: Details are opened over real work content
- **WHEN** 用户展开已有 Patrol 工作详情
- **THEN** 输入控制、必要工具条和检查控制与正文形成可辨的浮动层次，图／进度／冻结／事实正文仍可清楚阅读，玻璃内部按钮不再各自嵌入一层背景滤镜

#### Scenario: The work area has no content yet
- **WHEN** 用户处于空工作区或安静输入态
- **THEN** 主内容只保留输入区，材质通过自身边缘和交互响应呈现，不创建装饰数据、彩色背景、工作卡片或虚构仪表盘

### Requirement: Glass belongs to the control surface
The frontend SHALL 在控件自身协调背景透光、顺形边缘明暗、厚度／层次及局部高光，并在交互时提供适度材料反馈；模态遮罩模糊、均匀描边、普通阴影或静态 blur 单独 MUST NOT 被认定为完成 Liquid Glass。

#### Scenario: Content scrolls beneath a floating control
- **WHEN** 用户在工作详情或长内容中滚动，真实内容经过浮动控制下方
- **THEN** 控制的透光与边缘可感知地参与该背景，文字保持清晰，变化发生在控制表面而非只有整屏遮罩

#### Scenario: A glass control is pressed then released
- **WHEN** 用户按压后释放输入控制或浮层控制
- **THEN** 形状／边缘／高光给予立即而克制的响应并回到安静态，文字与 caret 不被整体缩放，反馈不暗示网络请求已成功

### Requirement: Material remains legible and quiet
The frontend SHALL 使用一致的 hover、focus、press、selected、disabled 和 pending 反馈语言，保留普通文字至少4.5:1及关键非文字边界至少3:1的实际合成对比度。系统 SHALL 在无人交互且没有真实状态变化时保持安静，MUST NOT 增加呼吸、漂浮、粒子、循环扫描或持续光效。

#### Scenario: Dense evidence lies behind the control
- **WHEN** 工具条或浮层位于深浅文字、图边或事实背景之上
- **THEN** 标签／焦点与边缘仍可识别，必要的文字区域遮蔽不使整个内容层变成玻璃，也不只依靠颜色或动效传达状态

#### Scenario: A surface is idle
- **WHEN** 用户停止操作且展示状态稳定
- **THEN** 新材质效果停止活动，业务状态仍由文字、图标、轮廓或选中状态表达，不持续播放装饰动画

### Requirement: Accessibility and transparency fallback preserve information
The frontend SHALL 尊重减少动画、支持范围内的减少透明／高对比／forced-colors 偏好，并在透明渲染不支持时使用清晰的实色降级。降级 MUST NOT 删除业务内容、隐藏必要控制或取消焦点反馈。

#### Scenario: Reduced motion is enabled during a transition
- **WHEN** 系统减少动画偏好生效或在转换中改变
- **THEN** 位移、形变和光感扩散停止，界面立即收敛到最后合法显示意图，焦点、状态文字与可用控件保持

#### Scenario: Transparency is unavailable or reduced
- **WHEN** 当前平台不支持控制材质，或实际支持的减少透明／高对比偏好要求降级
- **THEN** 控制呈现实色和清楚轮廓，相同信息与业务操作仍可用，验收单独记录正常效果和降级状态

### Requirement: Platform claims are evidence bounded
The delivery SHALL 明确区分 Apple 原生光学特性、Skill 作者建议和 Focus 的 Web 实现选择；不得声称未经验证的原生等价。近似材质 SHALL 仍满足控制自身的背景参与、边缘厚度和交互响应，不能用平台限制接受普通磨砂替代。

#### Scenario: The first closed loop still looks like frosted cards
- **WHEN** 实际页面仅显示白底或静态模糊加阴影，没有可辨的控制自身厚度和交互光感
- **THEN** 该材质验收为未完成，即使业务测试通过、参考已读取或 CSS 存在 backdrop-filter
